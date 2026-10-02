"""Convert linked Canonical XMI files (bblock_to_xmi.py --linked) to Enterprise Architect
native XMI 1.1, one file per building block, for import into EA.

EA does not resolve <type href> references between Canonical XMI files, and it replaces
their xmi:uuids with GUIDs of its own (see README.md, Linked XMI). Its native format
carries the GUIDs EA keeps, so here:

  - every element, attribute and package gets the GUID of its Canonical xmi:uuid
    (ea_guid tagged value {GUID}, xmi.id EAID_/EAPK_ + GUID), the same on every run;
  - a type in another building block is referenced by that element's GUID
    (<UML:Classifier xmi.idref="EAID_..."/>), computed from its xmi:id the way
    bblock_to_xmi.py computes xmi:uuids, plus a 'type' tagged value with its name;
  - comments are kept verbatim (newlines escaped), directives included.

Import the files into one EA package in dependency order: cdifCommonTypes.xml first,
then blocks before the blocks that reference them.

To be read as EA's own format, the files name "Enterprise Architect" 2.5 as exporter, as
EA's 1.1 exports do, and declare each element referenced from another file as an EAStub
in XMI.extensions. Imported with any other exporter name, EA assigned new GUIDs, left
cross-file types blank and refused Direct Merge ("Invalid Enterprise Architect XMI 1.1 file").

Usage:
    python linked_to_ea.py <linked root> [-o OUT_DIR]   (default OUT_DIR: <linked root>-ea)
"""
import argparse
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from bblock_to_xmi import COMMON_TYPES_FILE, REGISTER_PREFIX, REGISTER_URI, common_uid

XMI = "{http://www.omg.org/spec/XMI/20131001}"
UML_PRIM_NAMES = {"String", "Integer", "Boolean", "Real"}
ROOT_CLASS_ID = "EAID_11111111_5487_4080_A7F4_41526CB0AA00"  # EA's EARootClass placeholder
ELEMENT_KINDS = {}  # xmi:id -> "Class" / "DataType", over the whole linked set (filled by main)


def element_uuid(xmi_id):
    """xmi:uuid of an element referenced from another file, as bblock_to_xmi.py assigns it."""
    if xmi_id.startswith(REGISTER_PREFIX):
        bb_path, name = xmi_id[len(REGISTER_PREFIX):].rsplit(".", 1)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{REGISTER_URI}{bb_path.replace('.', '/')}#{name}"))
    return common_uid(xmi_id)


def ea_case(u):
    """A UUID in EA's GUID case: upper hex, third group lower (e.g. A77E33A2-0815-44c3-B370-...)."""
    parts = u.upper().split("-")
    parts[2] = parts[2].lower()
    return "-".join(parts)


def guid(u):
    return "{" + ea_case(u) + "}"


def ea_id(prefix, u):
    return f"{prefix}_{ea_case(u).replace('-', '_')}"


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
             .replace("\r", "&#xD;").replace("\n", "&#xA;").replace("\t", "&#x9;"))


def comment_body(elem):
    c = elem.find("ownedComment")
    return (c.findtext("body") or "") if c is not None else ""


def bound(elem, which):
    v = elem.find(f"{which}Value")
    return v.findtext("value") if v is not None else "1"


class Writer:
    def __init__(self):
        self.lines = ['<?xml version="1.0" encoding="UTF-8" standalone="no" ?>']
        self.depth = 0

    def open(self, tag, attrs=None, close=False):
        a = "".join(f' {k}="{esc(v)}"' for k, v in (attrs or {}).items())
        self.lines.append("\t" * self.depth + f"<{tag}{a}{'/' if close else ''}>")
        if not close:
            self.depth += 1

    def close(self, tag):
        self.depth -= 1
        self.lines.append("\t" * self.depth + f"</{tag}>")

    def tagged(self, items):
        self.open("UML:ModelElement.taggedValue")
        for tag, value in items:
            self.open("UML:TaggedValue", {"tag": tag, "value": value}, close=True)
        self.close("UML:ModelElement.taggedValue")


def type_ref(attr, primitives, stubs):
    """(xmi.idref, type name) for an attribute's <type>. A type in another file is also
    recorded in stubs ({EAID: (name, UML type)}) for the file's EAStub declarations."""
    t = attr.find("type")
    if t is None:
        return None, None
    if t.get(f"{XMI}idref"):
        target = t.get(f"{XMI}idref")
    else:
        href = t.get("href", "")
        file_part, _, target = href.partition("#")
        if "://" in file_part and target in UML_PRIM_NAMES:
            primitives.add(target)
            return f"eaxmiid_{target}", target
        eaid = ea_id("EAID", element_uuid(target))
        stubs[eaid] = (target.rsplit(".", 1)[-1], ELEMENT_KINDS.get(target, "Class"))
        return eaid, stubs[eaid][0]
    return ea_id("EAID", element_uuid(target)), target.rsplit(".", 1)[-1]


def emit_classifier(w, el, package_eaid, primitives, stubs):
    u = el.get(f"{XMI}uuid")
    stype = "Class" if el.get(f"{XMI}type") == "uml:Class" else "DataType"
    w.open("UML:Class", {"name": el.findtext("name"), "xmi.id": ea_id("EAID", u), "visibility": "public",
                         "namespace": package_eaid, "isRoot": "false", "isLeaf": "false",
                         "isAbstract": "false", "isActive": "false"})
    w.tagged([("documentation", comment_body(el)), ("isSpecification", "false"), ("ea_stype", stype),
              ("ea_ntype", "0"), ("version", "1.0"), ("package", package_eaid), ("ea_guid", guid(u))])
    attrs = el.findall("ownedAttribute")
    if attrs:
        w.open("UML:Classifier.feature")
        for pos, a in enumerate(attrs):
            idref, tname = type_ref(a, primitives, stubs)
            lower, upper = bound(a, "lower"), bound(a, "upper")
            w.open("UML:Attribute", {"name": a.findtext("name"), "changeable": "none", "visibility": "public",
                                     "ownerScope": "instance", "targetScope": "instance"})
            w.open("UML:Attribute.initialValue")
            w.open("UML:Expression", close=True)
            w.close("UML:Attribute.initialValue")
            if idref:
                w.open("UML:StructuralFeature.type")
                w.open("UML:Classifier", {"xmi.idref": idref}, close=True)
                w.close("UML:StructuralFeature.type")
            w.tagged([("description", comment_body(a)), ("type", tname or ""), ("derived", "0"),
                      ("containment", "Not Specified"), ("ordered", "0"), ("collection",
                      "true" if upper == "*" else "false"), ("position", str(pos)),
                      ("lowerBound", lower), ("upperBound", upper), ("duplicates", "0"),
                      ("ea_guid", guid(a.get(f"{XMI}uuid"))), ("styleex", "IsLiteral=0;volatile=0;union=0;")])
            w.close("UML:Attribute")
        w.close("UML:Classifier.feature")
    w.close("UML:Class")


def emit_package(w, pkg, primitives, stubs):
    """A uml:Package (and nested packages) as an EA UML:Package; classifiers before packages."""
    u = pkg.get(f"{XMI}uuid")
    eaid = ea_id("EAPK", u)
    w.open("UML:Package", {"name": pkg.findtext("name"), "xmi.id": eaid, "isRoot": "false",
                           "isLeaf": "false", "isAbstract": "false", "visibility": "public"})
    doc = comment_body(pkg) or pkg.findtext("URI") or ""
    w.tagged([("documentation", doc), ("isSpecification", "false"), ("ea_stype", "Public"),
              ("ea_eleType", "package"), ("version", "1.0"), ("ea_guid", guid(u))])
    w.open("UML:Namespace.ownedElement")
    children = pkg.findall("packagedElement")
    for el in children:
        if el.get(f"{XMI}type") in ("uml:Class", "uml:DataType"):
            emit_classifier(w, el, eaid, primitives, stubs)
    for el in children:
        if el.get(f"{XMI}type") == "uml:Package":
            emit_package(w, el, primitives, stubs)
    w.close("UML:Namespace.ownedElement")
    w.close("UML:Package")


def convert(canonical_file):
    """EA XMI 1.1 text for one linked Canonical XMI file. Its uml:Model becomes one EA package
    (a building block's model holds one package; the common-types model holds two, nested)."""
    model = ET.parse(canonical_file).getroot().find("{http://www.omg.org/spec/UML/20161101}Model")
    packages = [p for p in model.findall("packagedElement") if p.get(f"{XMI}type") == "uml:Package"]
    w = Writer()
    w.open("XMI", {"xmi.version": "1.1", "xmlns:UML": "omg.org/UML1.3"})
    w.open("XMI.header")
    w.open("XMI.documentation")
    w.lines.append("\t" * w.depth + "<XMI.exporter>Enterprise Architect</XMI.exporter>")
    w.lines.append("\t" * w.depth + "<XMI.exporterVersion>2.5</XMI.exporterVersion>")
    w.close("XMI.documentation")
    w.close("XMI.header")
    w.open("XMI.content")
    model_u = model.get(f"{XMI}uuid")
    w.open("UML:Model", {"name": model.findtext("name"), "xmi.id": ea_id("MX_EAID", model_u)})
    w.open("UML:Namespace.ownedElement")
    w.open("UML:Class", {"name": "EARootClass", "xmi.id": ROOT_CLASS_ID, "isRoot": "true",
                         "isLeaf": "false", "isAbstract": "false"}, close=True)
    primitives, stubs = set(), {}
    if len(packages) == 1:
        emit_package(w, packages[0], primitives, stubs)
    else:
        # Wrap several top-level packages in one, named after the model, so EA imports one package.
        wrapper = ET.Element("packagedElement", {f"{XMI}type": "uml:Package", f"{XMI}uuid": model_u})
        ET.SubElement(wrapper, "name").text = model.findtext("name")
        wrapper.extend(packages)
        emit_package(w, wrapper, primitives, stubs)
    for prim in sorted(primitives):
        w.open("UML:Class", {"name": prim, "xmi.id": f"eaxmiid_{prim}", "visibility": "public",
                             "isRoot": "true", "isLeaf": "false", "isAbstract": "false"})
        w.tagged([("isSpecification", "false"), ("ea_stype", "Primitive"), ("ea_ntype", "0")])
        w.close("UML:Class")
    w.close("UML:Namespace.ownedElement")
    w.close("UML:Model")
    w.close("XMI.content")
    # Elements of other building blocks this file references, declared the way EA declares
    # references out of an exported package, so EA can match them by GUID on import.
    w.open("XMI.extensions", {"xmi.extender": "Enterprise Architect 2.5"})
    for eaid, (name, kind) in sorted(stubs.items()):
        w.open("EAStub", {"xmi.id": eaid, "name": name, "UMLType": kind}, close=True)
    w.close("XMI.extensions")
    w.close("XMI")
    return "\n".join(w.lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("linked_root", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    args = ap.parse_args()
    root = args.linked_root.resolve()
    out_root = (args.output or root.with_name(root.name + "-ea")).resolve()
    files = [root / COMMON_TYPES_FILE] + sorted(p for p in root.rglob("*.xmi") if p.name != COMMON_TYPES_FILE)
    for f in files:
        for el in ET.parse(f).getroot().iter("packagedElement"):
            if el.get(f"{XMI}type") in ("uml:Class", "uml:DataType"):
                ELEMENT_KINDS[el.get(f"{XMI}id")] = el.get(f"{XMI}type")[len("uml:"):]
    for f in files:
        target = out_root / f.relative_to(root).with_suffix(".xml")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(convert(f), encoding="utf-8")
        print(f"wrote {target}")


if __name__ == "__main__":
    main()
