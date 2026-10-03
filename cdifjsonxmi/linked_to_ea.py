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

from bblock_to_xmi import COMMON_TYPES_FILE, REGISTER_PREFIX, REGISTER_URI, SHARED_TYPES_FILE, common_uid

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


class Context:
    """What one file's conversion collects on the way: primitive types used, EAStubs for
    elements of other files, association ends to emit as connectors, association uuids."""

    def __init__(self, model):
        self.primitives, self.stubs, self.assoc_ends, self.generalizations = set(), {}, [], []
        self.assoc_uuids = {e.get(f"{XMI}id"): e.get(f"{XMI}uuid") for e in model.iter("packagedElement")
                            if e.get(f"{XMI}type") == "uml:Association"}


def type_ref(attr, ctx):
    """(xmi.idref, type name) for an attribute's <type>. A type in another file is also
    recorded in ctx.stubs ({EAID: (name, UML type)}) for the file's EAStub declarations."""
    t = attr.find("type")
    if t is None:
        return None, None
    if t.get(f"{XMI}idref"):
        target = t.get(f"{XMI}idref")
    else:
        href = t.get("href", "")
        file_part, _, target = href.partition("#")
        if "://" in file_part and target in UML_PRIM_NAMES:
            ctx.primitives.add(target)
            return f"eaxmiid_{target}", target
        eaid = ea_id("EAID", element_uuid(target))
        ctx.stubs[eaid] = (target.rsplit(".", 1)[-1], ELEMENT_KINDS.get(target, "Class"))
        return eaid, ctx.stubs[eaid][0]
    return ea_id("EAID", element_uuid(target)), target.rsplit(".", 1)[-1]


def multiplicity(lower, upper):
    return lower if lower == upper else f"{lower}..{upper}"


def emit_classifier(w, el, package_eaid, ctx):
    """A class, datatype or enumeration as an EA UML:Class. Attributes that are association ends
    are left out here and collected in ctx.assoc_ends, to become connectors (emit_association).
    Enumeration literals become frozen classifier-scope attributes, as in EA's own exports."""
    u = el.get(f"{XMI}uuid")
    stype = el.get(f"{XMI}type")[len("uml:"):]
    w.open("UML:Class", {"name": el.findtext("name"), "xmi.id": ea_id("EAID", u), "visibility": "public",
                         "namespace": package_eaid, "isRoot": "false", "isLeaf": "false",
                         "isAbstract": el.get("isAbstract", "false"), "isActive": "false"})
    if stype == "Enumeration":
        w.open("UML:ModelElement.stereotype")
        w.open("UML:Stereotype", {"name": "enumeration"}, close=True)
        w.close("UML:ModelElement.stereotype")
    w.tagged([("documentation", comment_body(el)), ("isSpecification", "false"), ("ea_stype", stype),
              ("ea_ntype", "0"), ("version", "1.0"), ("package", package_eaid), ("ea_guid", guid(u))])
    literals = el.findall("ownedLiteral")
    if literals:
        w.open("UML:Classifier.feature")
        for lit in literals:
            w.open("UML:Attribute", {"name": lit.findtext("name"), "changeable": "frozen", "visibility": "public",
                                     "ownerScope": "classifier", "targetScope": "instance"})
            w.open("UML:Attribute.initialValue")
            w.open("UML:Expression", close=True)
            w.close("UML:Attribute.initialValue")
            w.tagged([("ea_guid", guid(lit.get(f"{XMI}uuid")))])
            w.close("UML:Attribute")
        w.close("UML:Classifier.feature")
    for gen in el.findall("generalization"):
        ctx.generalizations.append((el, gen))
    attrs = []
    for a in el.findall("ownedAttribute"):
        if a.find("association") is not None:
            ctx.assoc_ends.append((el, a))
        else:
            attrs.append(a)
    if attrs:
        w.open("UML:Classifier.feature")
        for pos, a in enumerate(attrs):
            idref, tname = type_ref(a, ctx)
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


def emit_association(w, owner, end, ctx):
    """An association end (Canonical ownedAttribute with <association>) as an EA connector,
    written inside the owner's package:
    source end at the owner, non-navigable, 0..*; target end navigable, named after the role,
    with the attribute's multiplicity, comment and GUID. The association is unnamed."""
    assoc_id = end.find("association").get(f"{XMI}idref")
    u = ctx.assoc_uuids[assoc_id]
    owner_u = owner.get(f"{XMI}uuid")
    target_eaid, target_name = type_ref(end, ctx)
    target_kind = ctx.stubs.get(target_eaid, (target_name, ELEMENT_KINDS.get(
        end.find("type").get(f"{XMI}idref", ""), "Class")))[1]
    # unnamed in EA, so diagrams show only the role name (the Canonical XMI keeps the name)
    w.open("UML:Association", {"xmi.id": ea_id("EAID", u),
                               "visibility": "public", "isRoot": "false", "isLeaf": "false",
                               "isAbstract": "false"})
    w.tagged([("documentation", comment_body(end)), ("ea_type", "Association"),
              ("direction", "Source -> Destination"), ("ea_sourceName", owner.findtext("name")),
              ("ea_targetName", target_name),
              ("ea_sourceType", owner.get(f"{XMI}type")[len("uml:"):]),
              ("ea_targetType", target_kind), ("ea_guid", guid(u))])
    w.open("UML:Association.connection")
    w.open("UML:AssociationEnd", {"visibility": "public", "multiplicity": "0..*", "aggregation": "none",
                                  "isOrdered": "false", "targetScope": "instance", "changeable": "none",
                                  "isNavigable": "false", "type": ea_id("EAID", owner_u)})
    w.tagged([("containment", "Unspecified"), ("sourcestyle", "Union=0;Derived=0;AllowDuplicates=0;Owned=0;Navigable=Non-Navigable;"),
              ("ea_end", "source")])
    w.close("UML:AssociationEnd")
    w.open("UML:AssociationEnd", {"visibility": "public", "name": end.findtext("name"),
                                  "multiplicity": multiplicity(bound(end, "lower"), bound(end, "upper")),
                                  "aggregation": "none", "isOrdered": "false", "targetScope": "instance",
                                  "changeable": "none", "isNavigable": "true", "type": target_eaid})
    w.tagged([("description", comment_body(end)), ("containment", "Unspecified"),
              ("deststyle", "Union=0;Derived=0;AllowDuplicates=0;Owned=0;Navigable=Navigable;"), ("ea_end", "target"),
              ("ea_guid", guid(end.get(f"{XMI}uuid")))])
    w.close("UML:AssociationEnd")
    w.close("UML:Association.connection")
    w.close("UML:Association")


def emit_generalization(w, sub, gen, ctx):
    """A canonical <generalization> (general by idref or href) as an EA UML:Generalization,
    written inside the subtype's package; a supertype in another file is declared as an EAStub."""
    g = gen.find("general")
    target = g.get(f"{XMI}idref") or g.get("href", "").partition("#")[2]
    super_eaid = ea_id("EAID", element_uuid(target))
    kind = ELEMENT_KINDS.get(target, "Class")
    if not g.get(f"{XMI}idref"):
        ctx.stubs[super_eaid] = (target.rsplit(".", 1)[-1], kind)
    u = gen.get(f"{XMI}uuid")
    w.open("UML:Generalization", {"subtype": ea_id("EAID", sub.get(f"{XMI}uuid")), "supertype": super_eaid,
                                  "xmi.id": ea_id("EAID", u), "visibility": "public"})
    w.tagged([("ea_type", "Generalization"), ("direction", "Source -> Destination"),
              ("ea_sourceName", sub.findtext("name")), ("ea_targetName", target.rsplit(".", 1)[-1]),
              ("ea_sourceType", sub.get(f"{XMI}type")[len("uml:"):]), ("ea_targetType", kind),
              ("ea_guid", guid(u))])
    w.close("UML:Generalization")


def emit_package(w, pkg, ctx):
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
    first_end, first_gen = len(ctx.assoc_ends), len(ctx.generalizations)
    for el in children:
        if el.get(f"{XMI}type") in ("uml:Class", "uml:DataType", "uml:Enumeration"):
            emit_classifier(w, el, eaid, ctx)
    # Associations and generalizations go inside the package of the classes that own them, as
    # in EA's own exports; EA's package import ignores them at the model root.
    for owner, end in ctx.assoc_ends[first_end:]:
        emit_association(w, owner, end, ctx)
    for sub, gen in ctx.generalizations[first_gen:]:
        emit_generalization(w, sub, gen, ctx)
    for el in children:
        if el.get(f"{XMI}type") == "uml:Package":
            emit_package(w, el, ctx)
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
    ctx = Context(model)
    if len(packages) == 1:
        emit_package(w, packages[0], ctx)
    else:
        # Wrap several top-level packages in one, named after the model, so EA imports one package.
        wrapper = ET.Element("packagedElement", {f"{XMI}type": "uml:Package", f"{XMI}uuid": model_u})
        ET.SubElement(wrapper, "name").text = model.findtext("name")
        wrapper.extend(packages)
        emit_package(w, wrapper, ctx)
    for prim in sorted(ctx.primitives):
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
    for eaid, (name, kind) in sorted(ctx.stubs.items()):
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
    first = [root / n for n in (COMMON_TYPES_FILE, SHARED_TYPES_FILE) if (root / n).exists()]
    files = first + import_order(sorted(p for p in root.rglob("*.xmi") if p not in first))
    for f in files:
        for el in ET.parse(f).getroot().iter("packagedElement"):
            if el.get(f"{XMI}type") in ("uml:Class", "uml:DataType", "uml:Enumeration"):
                ELEMENT_KINDS[el.get(f"{XMI}id")] = el.get(f"{XMI}type")[len("uml:"):]
    for f in files:
        target = out_root / f.relative_to(root).with_suffix(".xml")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(convert(f), encoding="utf-8")
        print(f"wrote {target}")
    # The order EA needs them in (referenced files first), for import_to_ea.ps1.
    order = out_root / "import_order.txt"
    order.write_text("".join(str(f.relative_to(root).with_suffix(".xml")).replace("/", "\\") + "\n"
                             for f in files), encoding="utf-8")
    print(f"wrote {order}")


def import_order(files):
    """Files ordered so each comes after every file its hrefs (types, generalizations) point to."""
    deps = {}
    for f in files:
        targets = set()
        for e in ET.parse(f).getroot().iter():
            href = e.get("href", "") if e.tag in ("type", "general") else ""
            path = href.partition("#")[0]
            if path and "://" not in path:
                targets.add((f.parent / path).resolve())
        deps[f.resolve()] = targets
    ordered, done = [], set()

    def visit(f, stack=()):
        if f in done:
            return
        if f in stack:
            raise SystemExit(f"cyclic references: {' -> '.join(p.name for p in stack + (f,))}")
        for t in sorted(deps[f]):
            if t in deps:  # files outside the set (or the common/shared files) impose no order here
                visit(t, stack + (f,))
        done.add(f)
        ordered.append(f)

    for f in sorted(deps):
        visit(f)
    return ordered


if __name__ == "__main__":
    main()
