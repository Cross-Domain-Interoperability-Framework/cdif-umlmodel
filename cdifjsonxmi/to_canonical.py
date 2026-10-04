"""Transform an Enterprise Architect XMI export (UML 2.5 / XMI 2.5.1, see export_from_ea.ps1)
into Canonical XMI, with Joachim Wackerow's to-canonical-xmi stylesheets run by lxml.

This replaces to-canonical-xmi's run-all.cmd (Xalan, Saxon and sed) and gives the same XML:
steps 1-4 and the identifier check are his XSLT 1.0 stylesheets, unchanged on disk, which
lxml (libxslt) runs after two adjustments made as they are loaded:

  - a match pattern alternative that is a bare @name (step 2's @type, @name, ...) is written
    @*[local-name()='name' and namespace-uri()=''] with priority 0: libxslt lets @type match
    the namespaced xmi:type, which XSLT 1.0 (and Xalan) doesn't;
  - xsl:sort's default collation is the processor's; Xalan sorts with Java's locale collation
    (case-insensitive, then lowercase first), libxslt by code point. Each text sort becomes a
    case-folded sort with a case-swapped tie-break, which gives Xalan's order.

One CDIF addition to step 3: a class with several generalizations (multiple inheritance,
which DDI-CDI doesn't use) gets generalization ids naming the general
(...-generalization_VariableMeasured), where step 3 would give them all one id.

format.xslt (XSLT 2.0: trailing whitespace off comment bodies, then indenting) and the sed
namespace edits of the Eclipse variant are done in Python. Checked against Xalan on the linked
set exported from EA: every step's output is the same XML.

    python to_canonical.py roundtrip/from-ea/linkTestEA4.xmi
writes <base>_canonical.xmi, <base>_canonical-unique-names.xmi (and with --eclipse
<base>_canonical-unique-names-eclipse.xmi) and <base>_validate-ids.log beside the input.
"""
import argparse
import re
import sys
from pathlib import Path

from lxml import etree

DEFAULT_HOME = Path(__file__).resolve().parents[2] / "to-canonical-xmi"
XSL = "{http://www.w3.org/1999/XSL/Transform}"
UPPER, LOWER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
ECLIPSE = [("http://www.omg.org/spec/UML/20131001/StandardProfile",
            "http://www.eclipse.org/uml2/5.0.0/UML/Profile/Standard"),
           ("http://www.omg.org/spec/UML/20131001", "http://www.eclipse.org/uml2/5.0.0/UML")]


def stylesheet(home, name):
    """One of to-canonical-xmi's stylesheets, adjusted for libxslt (see the module doc)."""
    tree = etree.parse(str(Path(home) / f"to-canonical-xmi-{name}.xslt"))
    for t in tree.iter(XSL + "template"):
        match = t.get("match")
        if not match:
            continue
        alts = [a.strip() for a in match.split("|")]
        new = [f"@*[local-name()='{a[1:]}' and namespace-uri()='']"
               if re.fullmatch(r"@[A-Za-z_][\w.-]*", a) else a for a in alts]
        if new != alts:
            t.set("match", " | ".join(new))
            t.set("priority", t.get("priority", "0"))
    if name == "step-3":
        _name_generalizations(tree)
    for sort in list(tree.iter(XSL + "sort")):
        if sort.get("data-type", "text") != "text" or sort.get("lang"):
            continue
        select = sort.get("select", ".")
        sort.set("select", f"translate({select}, '{UPPER}', '{LOWER}')")
        sort.addnext(etree.Element(XSL + "sort", select=f"translate({select}, '{UPPER}{LOWER}', '{LOWER}{UPPER}')"))
    return etree.XSLT(tree)


def _name_generalizations(tree):
    """Step 3 names an unnamed element's identifier part after its tag, so a class with two
    generalizations gets two generalizations with one id. When there are several, add the
    general's name (generalization_VariableMeasured); a single generalization keeps its id."""
    create_id = next(t for t in tree.iter(XSL + "template") if t.get("name") == "CreateID")
    owned_end = next(w for w in create_id.iter(XSL + "when") if "local-name()='ownedEnd'" in w.get("test", ""))
    rule = etree.Element(XSL + "when", test="local-name()='generalization' and count(../generalization) > 1")
    for select in ("local-name()", "$WordSeparator", "key('id', general/@xmi:idref)/name"):
        etree.SubElement(rule, XSL + "value-of", select=select)
    owned_end.addnext(rule)  # (the xmi prefix is declared on the stylesheet element)


def formatted(doc):
    """format.xslt: comment bodies without trailing whitespace, then indented."""
    root = doc.getroot() if hasattr(doc, "getroot") else doc
    for body in root.iter("body"):
        if body.text:
            body.text = body.text.rstrip()
    for e in root.iter():
        if len(e) and e.text is not None and not e.text.strip():
            e.text = None  # whitespace-only text between elements: re-indented below
        if e.tail is not None and not e.tail.strip():
            e.tail = None
    etree.indent(root, space="  ")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True) + b"\n"


def to_canonical(xmi, home=DEFAULT_HOME, uri_root="https://w3id.org/cdif/xmi/", eclipse=False):
    """Run the pipeline on an EA XMI file; return {kind: written path}."""
    xmi = Path(xmi)
    base = xmi.with_suffix("")
    step1 = stylesheet(home, "step-1")(etree.parse(str(xmi)))
    step2 = stylesheet(home, "step-2")(step1)
    step3 = stylesheet(home, "step-3")
    step4 = stylesheet(home, "step-4")
    params = {"DDI4_XMI_URI_Root": etree.XSLT.strparam(uri_root)}
    out = {}
    for unique, suffix in (("no", "_canonical.xmi"), ("yes", "_canonical-unique-names.xmi")):
        result = step4(step3(step2, UniqueNames=etree.XSLT.strparam(unique), **params))
        path = Path(f"{base}{suffix}")
        path.write_bytes(formatted(result))
        out[suffix] = path
        if unique == "yes":
            log = stylesheet(home, "validate-ids")(result)
            out["log"] = Path(f"{base}_validate-ids.log")
            out["log"].write_text(str(log), encoding="utf-8")
    if eclipse:
        text = out["_canonical-unique-names.xmi"].read_text(encoding="utf-8")
        for old, new in ECLIPSE:
            text = text.replace(old, new)
        out["eclipse"] = Path(f"{base}_canonical-unique-names-eclipse.xmi")
        out["eclipse"].write_text(text, encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xmi", type=Path, help="EA export, UML 2.5 / XMI 2.5.1")
    ap.add_argument("--home", type=Path, default=DEFAULT_HOME, help="to-canonical-xmi checkout (default: %(default)s)")
    ap.add_argument("--uri-root", default="https://w3id.org/cdif/xmi/",
                    help="root of the xmi:uuid URIs (step 3's DDI4_XMI_URI_Root; default: %(default)s)")
    ap.add_argument("--eclipse", action="store_true", help="also write the Eclipse UML2 namespace variant")
    args = ap.parse_args()
    if not (args.home / "to-canonical-xmi-step-1.xslt").exists():
        sys.exit(f"no to-canonical-xmi stylesheets in {args.home}")
    for path in to_canonical(args.xmi, args.home, args.uri_root, args.eclipse).values():
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
