"""Generate Canonical XMI (UML 2.5 / XMI 2.5.1) from a CDIF building-block JSON Schema.

Follows the conventions of xmiModels/cdifmodels/cdifmodels.xmi (one package per
vocabulary prefix, dotted xmi:ids, "**CDIF** / Definition" comment bodies). JSON-LD
facts UML has no slot for are written as comment directives (:rdfType:, ...), which
uml_to_schema.py (in this folder) reads back with --comment-directives.
roundtrip.py runs both halves and diffs. See README.md for the mapping table.

Usage:
    python bblock_to_xmi.py <bblock dir> [-o out.xmi]
"""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

from mapping import (CHOICE_LABEL, DEFINITION_HEADER, JSONLD_KEYWORDS, PACKAGE_NAMES, UML_PRIM,
                     attr_name, is_iri_reference, rdf_type_schema)

# uuid5 namespace for this generator. cdifmodels.xmi uses a namespace we could not
# identify, so xmi:uuids here differ from it even where xmi:ids are equal.
NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://w3id.org/cdif/xmi/")

SCALARS = {("string", None): ("prim", "String"), ("integer", None): ("prim", "Integer"),
           ("boolean", None): ("prim", "Boolean"), ("number", None): ("prim", "Real"),
           ("string", "uri"): ("XMLSchemaDataTypes", "XsdAnyUri"),
           ("string", "date"): ("XMLSchemaDataTypes", "XsdDate"),
           ("string", "date-time"): ("XMLSchemaDataTypes", "XsdDateTime")}
SUPPORT_TYPES = {"XMLSchemaDataTypes.XsdAnyUri": "XML Schema primitive datatype (XsdAnyUri).",
                 "XMLSchemaDataTypes.XsdDate": "XML Schema primitive datatype (XsdDate).",
                 "XMLSchemaDataTypes.XsdDateTime": "XML Schema primitive datatype (XsdDateTime).",
                 "common.IriReference": "An IRI-valued property. JSON-LD encoding: either a plain "
                 "string, or a node reference object {\"@id\": string} with no other keys. URI-shape "
                 "values should use the {\"@id\"} form so they participate in RDF entailment."}
OBJECT_KEYWORDS = {"$schema", "description", "type", "properties", "required", "allOf", "anyOf"}


class Unmapped(Exception):
    pass


def uid(xmi_id):
    return str(uuid.uuid5(NS, xmi_id))


# Linked mode: one XMI file per building block at <out root>/<path under _sources>/<dir>.xmi,
# identified like the building-block register does it.
REGISTER_PREFIX = "cdif.bbr.metadata."                   # bblocks-config.yaml identifier-prefix
REGISTER_URI = "https://w3id.org/cdif/bbr/metadata/"
COMMON_TYPES_FILE = "cdifCommonTypes.xmi"                # XSD datatypes and IriReference, shared


def bb_id(bb_path):
    """Register identifier of a building block, e.g. cdif.bbr.metadata.schemaorgProperties.person."""
    return REGISTER_PREFIX + bb_path.replace("/", ".")


def common_uid(xmi_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://w3id.org/cdif/xmi/{xmi_id}"))


class Link:
    """Where one building block's XMI file sits in a linked tree, and how it names things."""

    def __init__(self, bb_path, out_root):
        self.bb_path, self.bb_id, self.out_root = bb_path, bb_id(bb_path), out_root
        self.file = self.path_of(bb_path)

    def path_of(self, bb_path):
        return self.out_root / bb_path / f"{Path(bb_path).name}.xmi"

    def href_to(self, bb_path):
        return os.path.relpath(self.path_of(bb_path), self.file.parent).replace(os.sep, "/")

    def href_to_common(self):
        return os.path.relpath(self.out_root / COMMON_TYPES_FILE, self.file.parent).replace(os.sep, "/")

    def uid(self, xmi_id):
        """uuid5 of the element's URI: the block's register URI + '#' + the id local to the block."""
        if xmi_id.startswith(self.bb_id + "."):
            return str(uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"{REGISTER_URI}{self.bb_path}#{xmi_id[len(self.bb_id) + 1:]}"))
        if xmi_id == self.bb_id:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{REGISTER_URI}{self.bb_path}"))
        return common_uid(xmi_id)


def directive(name, value=None):
    return f"\n:{name}:" + (f" ``{value}``" if value is not None else "")


# Building-block directory prefixes naming the vocabulary, as uml_to_schema.py's sibling
# lookup recognizes them (ddicdiX, cdifX, ...), mapped to the package they imply.
DIR_PREFIXES = {"ddicdi": "cdi", "cdif": "cdif", "skos": "skos", "dcat": "dcat", "schema": "schema",
                "prov": "prov", "xas": "xas"}


def split_dir_name(bdir):
    """Building-block directory name -> (element name, vocabulary prefix or None)."""
    name = bdir.name
    for dp, pkg in DIR_PREFIXES.items():
        if name.startswith(dp) and name[len(dp):len(dp) + 1].isupper():
            return name[len(dp):], pkg
    return name[:1].upper() + name[1:], None


def rdf_prefix(schema):
    """Vocabulary prefix of an object schema: from its @type const, else its first prefixed property."""
    props = schema.get("properties", {})
    const = props.get("@type", {}).get("contains", {}).get("const")
    first = const or next((p for p in props if ":" in p), None)
    return first.split(":")[0] if first else None


def rdf_type_parts(type_prop):
    """Split an @type property schema into (const, allowed types or None, default or None).

    Accepts the plain CDIF pattern, optionally with a `default` and with items restricted
    to an enum (bare or wrapped in a one-branch anyOf). Returns None for anything else."""
    type_prop = dict(type_prop)
    default = type_prop.pop("default", None)
    items = type_prop.get("items", {})
    if set(items) == {"anyOf"} and len(items["anyOf"]) == 1:
        items = items["anyOf"][0]
    allowed = None
    if set(items) == {"type", "enum"} and items["type"] == "string":
        allowed, items = items["enum"], {"type": "string"}
    const = type_prop.get("contains", {}).get("const")
    if const is None or {**type_prop, "items": items} != rdf_type_schema(const):
        return None
    if allowed is not None and const not in allowed:
        return None
    return const, allowed, default


def choice_groups(schema):
    """Each anyOf of pure 'required' branches -> one group: a list of alternatives (lists of names)."""
    groups = []
    for block in [schema] + schema.get("allOf", []):
        if "anyOf" in block:
            for alt in block["anyOf"]:
                if set(alt) != {"required"}:
                    raise Unmapped(f"anyOf branch is not a pure 'required' group: {alt}")
            groups.append([alt["required"] for alt in block["anyOf"]])
    return groups


def required_props(schema):
    req = list(schema.get("required", []))
    for block in schema.get("allOf", []):
        req += [r for r in block.get("required", []) if r not in req]
    return req


class ModelBuilder:
    """Collects UML elements (xmi id -> element dict) from a building block and what it references."""

    def __init__(self, defs, link=None):
        self.elements = {}
        self.warnings = []
        self.defs = defs  # the root schema's $defs, for #/$defs/ references
        # Linked mode (one file per building block): a Link for this block; element ids are
        # register-qualified, and other blocks and the shared support types are href'd.
        self.link = link

    def element_id(self, pkg, name):
        return f"{self.link.bb_id}.{name}" if self.link else f"{pkg}.{name}"

    def add(self, eid, elem):
        if eid in self.elements and self.elements[eid] != elem:
            raise Unmapped(f"two different definitions for {eid}")
        self.elements[eid] = elem
        return eid

    def support(self, eid):
        """Type ref to a support type (XSD datatype, IriReference)."""
        if self.link:
            return ("href", f"{self.link.href_to_common()}#{eid}")
        pkg, name = eid.split(".")
        return ("idref", self.add(eid, {"kind": "uml:DataType", "pkg": pkg, "name": name,
                                        "body": DEFINITION_HEADER + SUPPORT_TYPES[eid], "attrs": []}))

    def bblock_ref(self, ref, base_dir):
        """Type ref to another building block's root element, and that element's kind.

        Linked mode: an href into the block's own XMI file. Single-file mode: a stub element
        whose :buildingBlock: directive names the block."""
        target = (base_dir / ref).resolve().parent
        sources = next((p for p in target.parents if p.name == "_sources"), None)
        if sources is None:
            raise Unmapped(f"$ref {ref} does not point into a _sources tree")
        schema = yaml.safe_load((target / "schema.yaml").read_text(encoding="utf-8"))
        kind = "uml:Class" if "@id" in schema.get("properties", {}) else "uml:DataType"
        name, dir_pkg = split_dir_name(target)
        bb_path = target.relative_to(sources).as_posix()
        if self.link:
            return ("href", f"{self.link.href_to(bb_path)}#{bb_id(bb_path)}.{name}"), kind
        pkg = rdf_prefix(schema) or dir_pkg
        if pkg is None:
            raise Unmapped(f"$ref {ref}: no @type, prefixed property or directory prefix to place it in a package")
        body = DEFINITION_HEADER + directive("buildingBlock", bb_path)
        eid = self.add(f"{pkg}.{name}", {"kind": kind, "pkg": pkg, "name": name, "body": body, "attrs": []})
        return ("idref", eid), kind

    def element(self, schema, name, base_dir, where):
        """uml:Class (schema declares @id) or uml:DataType for an object schema; returns its xmi id."""
        props = schema.get("properties", {})
        extra = set(schema) - OBJECT_KEYWORDS
        if extra or schema.get("type") != "object":
            raise Unmapped(f"{where}: keywords {sorted(extra)} / type {schema.get('type')}")
        rdf_type = allowed_types = type_default = None
        if "@type" in props:
            parts = rdf_type_parts(props["@type"])
            if parts is None:
                raise Unmapped(f"{where}.@type: {json.dumps(props['@type'])[:160]}")
            rdf_type, allowed_types, type_default = parts
            if isinstance(type_default, str):
                type_default = [type_default]  # @type is an array; a bare string default is wrapped
            if type_default is not None and not (
                    isinstance(type_default, list) and all(isinstance(t, str) for t in type_default)):
                raise Unmapped(f"{where}.@type: default is not a string or list of strings: {type_default!r}")
        if "@id" in props and props["@id"] != {"type": "string"}:
            raise Unmapped(f"{where}.@id: {props['@id']}")
        if name is None:
            if not rdf_type:
                raise Unmapped(f"{where}: inline object has no @type const to name it")
            name = rdf_type.split(":")[1]
        pkg = rdf_prefix(schema)
        required = required_props(schema)
        groups = choice_groups(schema)
        for r in (r for g in groups for alt in g for r in alt):
            if r not in props:
                self.warnings.append(f"{where}: choice constraint names '{r}', which is not a declared property")

        body = DEFINITION_HEADER + schema.get("description", "")
        if rdf_type:
            body += directive("rdfType", rdf_type)
            if allowed_types:
                body += directive("allowedTypes", " | ".join(allowed_types))
            if type_default is not None:
                body += directive("typeDefault", " | ".join(type_default))
            if "@type" not in required:
                self.warnings.append(f"{where}: @type is declared but not required; the XMI assumes it is required")
        if groups:
            body += f"\n{CHOICE_LABEL}\n" + "\n".join(
                "- ``" + " | ".join(" & ".join(attr_name(r, pkg) for r in alt) for alt in g) + "``"
                for g in groups)

        attrs = []
        for pname, prop in props.items():
            if pname in JSONLD_KEYWORDS:
                continue
            type_ref, upper, directives = self.attr_type(prop, base_dir, f"{where}.{pname}")
            desc = prop.get("description")
            attrs.append({"name": attr_name(pname, pkg), "type": type_ref, "upper": upper,
                          "lower": 1 if pname in required else 0,
                          "body": None if desc is None and not directives
                          else DEFINITION_HEADER + (desc or "") + "".join(directives)})
        kind = "uml:Class" if "@id" in props else "uml:DataType"
        return self.add(self.element_id(pkg, name),
                        {"kind": kind, "pkg": pkg, "name": name, "body": body, "attrs": attrs})

    def attr_type(self, prop, base_dir, where):
        """Map a property schema to (type ref, upper bound, attribute directives)."""
        prop = {k: v for k, v in prop.items() if k != "description"}
        upper, directives = 1, []
        if prop.get("type") == "array":
            if set(prop) != {"type", "items"}:
                raise Unmapped(f"{where}: array keywords {sorted(set(prop) - {'type', 'items'})}")
            prop, upper = prop["items"], "*"
        while set(prop) == {"$ref"} and prop["$ref"].startswith("#/$defs/"):
            name = prop["$ref"][len("#/$defs/"):]
            if name not in self.defs:
                raise Unmapped(f"{where}: unresolved {prop['$ref']}")
            prop = self.defs[name]
        if is_iri_reference(prop):
            return self.support("common.IriReference"), upper, directives
        branches = prop.get("anyOf")
        if set(prop) == {"anyOf"} and len(branches) == 2 and branches[1] == {"type": "string"} \
                and set(branches[0]) == {"$ref"}:
            directives.append(directive("alsoAcceptsString"))
            prop = branches[0]
        if set(prop) == {"$ref"} and not prop["$ref"].startswith("#"):
            type_ref, kind = self.bblock_ref(prop["$ref"], base_dir)
        elif prop.get("type") == "object":
            eid = self.element(prop, None, base_dir, where)
            type_ref, kind = ("idref", eid), self.elements[eid]["kind"]
        else:
            key = (prop.get("type"), prop.get("format"))
            if set(prop) - {"type", "format"} or key not in SCALARS:
                raise Unmapped(f"{where}: {json.dumps(prop)[:160]}")
            tpkg, tname = SCALARS[key]
            if tpkg == "prim":
                return ("prim", tname), upper, directives
            return self.support(f"{tpkg}.{tname}"), upper, directives
        if kind == "uml:Class":
            # The schema embeds the node; uml_to_schema.py's default would also accept {"@id"}.
            directives.insert(0, directive("inlineOrByReference", "inline"))
        return type_ref, upper, directives


class Writer:
    def __init__(self, uid_for=None):
        self.lines = []
        self.uid = uid_for or uid

    def add(self, depth, text):
        self.lines.append("  " * depth + text)

    def comment(self, depth, owner, body):
        cid = f"{owner}.comment"
        self.add(depth, f'<ownedComment xmi:type="uml:Comment" xmi:id="{cid}" xmi:uuid="{self.uid(cid)}">')
        self.lines.append("  " * (depth + 1) + f"<body>{escape(body)}</body>")
        self.add(depth + 1, f'<annotatedElement xmi:idref="{owner}"/>')
        self.add(depth, "</ownedComment>")

    def bound(self, depth, owner, which, value):
        bid = f"{owner}.{which}"
        kind = "LiteralInteger" if which == "lower" else "LiteralUnlimitedNatural"
        self.add(depth, f'<{which}Value xmi:type="uml:{kind}" xmi:id="{bid}" xmi:uuid="{self.uid(bid)}">')
        self.add(depth + 1, f"<value>{value}</value>")
        self.add(depth, f"</{which}Value>")

    def element(self, depth, eid, elem):
        self.add(depth, f'<packagedElement xmi:type="{elem["kind"]}" xmi:id="{eid}" xmi:uuid="{self.uid(eid)}">')
        self.add(depth + 1, f"<name>{elem['name']}</name>")
        self.comment(depth + 1, eid, elem["body"])
        for a in elem["attrs"]:
            aid = f"{eid}.{a['name']}"
            self.add(depth + 1, f'<ownedAttribute xmi:type="uml:Property" xmi:id="{aid}" xmi:uuid="{self.uid(aid)}">')
            self.add(depth + 2, f"<name>{a['name']}</name>")
            kind, ref = a["type"]
            if kind == "prim":
                self.add(depth + 2, f'<type xmi:type="uml:PrimitiveType" href="{UML_PRIM}{ref}"/>')
            elif kind == "href":
                self.add(depth + 2, f'<type href="{escape(ref)}"/>')
            else:
                self.add(depth + 2, f'<type xmi:idref="{ref}"/>')
            self.bound(depth + 2, aid, "lower", a["lower"])
            self.bound(depth + 2, aid, "upper", a["upper"])
            if a["body"] is not None:
                self.comment(depth + 2, aid, a["body"])
            self.add(depth + 1, "</ownedAttribute>")
        self.add(depth, "</packagedElement>")


def build(bblock_dir):
    """Return (class name, class package, XMI text, warnings) for a building block directory."""
    bdir = Path(bblock_dir).resolve()
    meta = json.loads((bdir / "bblock.json").read_text(encoding="utf-8"))
    schema = yaml.safe_load((bdir / "schema.yaml").read_text(encoding="utf-8"))
    class_name = split_dir_name(bdir)[0]
    mb = ModelBuilder(schema.get("$defs", {}))
    root = {k: v for k, v in schema.items() if k != "$defs"}
    root_id = mb.element(root, class_name, bdir, bdir.name)

    out = Writer()
    out.add(0, '<?xml version="1.0" encoding="UTF-8"?>')
    out.add(0, '<xmi:XMI xmi:version="2.5.1" xmlns:xmi="http://www.omg.org/spec/XMI/20131001" '
               'xmlns:uml="http://www.omg.org/spec/UML/20161101">')
    model_id = f"model.CDIF{class_name}"
    out.add(1, f'<uml:Model xmi:id="{model_id}" xmi:uuid="{uid(model_id)}">')
    out.add(2, f"<name>CDIF{class_name}</name>")
    out.comment(2, model_id, f"Generated by bblock_to_xmi.py from building block '{meta['name']}' "
                             f"({bdir.parent.name}/{bdir.name}).")
    support = ["XMLSchemaDataTypes", "common"]
    pkgs = sorted({e["pkg"] for e in mb.elements.values()}, key=lambda p: (p not in support, p))
    for pkg in pkgs:
        out.add(2, f'<packagedElement xmi:type="uml:Package" xmi:id="{pkg}" xmi:uuid="{uid(pkg)}">')
        out.add(3, f"<name>{PACKAGE_NAMES.get(pkg, pkg)}</name>")
        out.add(3, f"<URI>https://w3id.org/cdif/{pkg}/xmi/</URI>")
        for eid, elem in sorted(mb.elements.items()):
            if elem["pkg"] == pkg:
                out.element(3, eid, elem)
        out.add(2, "</packagedElement>")
    out.add(1, "</uml:Model>")
    out.add(0, "</xmi:XMI>")
    return class_name, mb.elements[root_id]["pkg"], "\n".join(out.lines) + "\n", mb.warnings


XMI_HEADER = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<xmi:XMI xmi:version="2.5.1" xmlns:xmi="http://www.omg.org/spec/XMI/20131001" '
              'xmlns:uml="http://www.omg.org/spec/UML/20161101">']


def build_linked(bblock_dir, out_root):
    """Linked mode: return (XMI file path, class name, class package, XMI text, warnings).

    The file holds one uml:Package for the building block (xmi:id = its register identifier,
    URI = its register URI) with the block's root element and its inline nested elements.
    Other blocks are referenced by href into their own files."""
    bdir = Path(bblock_dir).resolve()
    sources = next((p for p in bdir.parents if p.name == "_sources"), None)
    if sources is None:
        raise Unmapped(f"{bdir} is not under a _sources tree")
    bb_path = bdir.relative_to(sources).as_posix()
    link = Link(bb_path, Path(out_root).resolve())
    meta = json.loads((bdir / "bblock.json").read_text(encoding="utf-8"))
    schema = yaml.safe_load((bdir / "schema.yaml").read_text(encoding="utf-8"))
    class_name = split_dir_name(bdir)[0]
    mb = ModelBuilder(schema.get("$defs", {}), link)
    root_id = mb.element({k: v for k, v in schema.items() if k != "$defs"}, class_name, bdir, bdir.name)

    out = Writer(link.uid)
    out.lines += XMI_HEADER
    model_id = f"{link.bb_id}.model"
    out.add(1, f'<uml:Model xmi:id="{model_id}" xmi:uuid="{link.uid(model_id)}">')
    out.add(2, f"<name>{bdir.name}</name>")
    out.comment(2, model_id, f"Building block {link.bb_id} ('{meta['name']}'), "
                             "generated by bblock_to_xmi.py --linked.")
    out.add(2, f'<packagedElement xmi:type="uml:Package" xmi:id="{link.bb_id}" xmi:uuid="{link.uid(link.bb_id)}">')
    out.add(3, f"<name>{bdir.name}</name>")
    out.add(3, f"<URI>{REGISTER_URI}{bb_path}</URI>")
    for eid in [root_id] + sorted(e for e in mb.elements if e != root_id):
        out.element(3, eid, mb.elements[eid])
    out.add(2, "</packagedElement>")
    out.add(1, "</uml:Model>")
    out.add(0, "</xmi:XMI>")
    return link.file, class_name, mb.elements[root_id]["pkg"], "\n".join(out.lines) + "\n", mb.warnings


def common_types_xmi():
    """The shared support types every linked file hrefs: XSD datatypes and IriReference."""
    out = Writer(common_uid)
    out.lines += XMI_HEADER
    out.add(1, f'<uml:Model xmi:id="cdif.commonTypes" xmi:uuid="{common_uid("cdif.commonTypes")}">')
    out.add(2, "<name>cdifCommonTypes</name>")
    for pkg in sorted({eid.split(".")[0] for eid in SUPPORT_TYPES}):
        out.add(2, f'<packagedElement xmi:type="uml:Package" xmi:id="{pkg}" xmi:uuid="{common_uid(pkg)}">')
        out.add(3, f"<name>{PACKAGE_NAMES.get(pkg, pkg)}</name>")
        out.add(3, f"<URI>https://w3id.org/cdif/{pkg}/xmi/</URI>")
        for eid in sorted(e for e in SUPPORT_TYPES if e.startswith(pkg + ".")):
            out.element(3, eid, {"kind": "uml:DataType", "name": eid.split(".")[1],
                                 "body": DEFINITION_HEADER + SUPPORT_TYPES[eid], "attrs": []})
        out.add(2, "</packagedElement>")
    out.add(1, "</uml:Model>")
    out.add(0, "</xmi:XMI>")
    return "\n".join(out.lines) + "\n"


def write_linked(bblock_dirs, out_root):
    """Write the shared types file and one XMI file per building block; return {bblock dir: result}."""
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / COMMON_TYPES_FILE).write_text(common_types_xmi(), encoding="utf-8")
    results = {}
    for d in bblock_dirs:
        path, class_name, pkg, xml, warnings = build_linked(d, out_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(xml, encoding="utf-8")
        results[d] = (path, class_name, pkg, warnings)
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bblock_dirs", nargs="+")
    ap.add_argument("-o", "--output", help="single-file mode: output file")
    ap.add_argument("--linked", metavar="OUT_DIR",
                    help="write one linked XMI file per building block under OUT_DIR")
    args = ap.parse_args()
    try:
        if args.linked:
            for path, _, _, warnings in write_linked(args.bblock_dirs, args.linked).values():
                for msg in warnings:
                    print(f"warning: {msg}", file=sys.stderr)
                print(f"wrote {path}")
            return
        if len(args.bblock_dirs) != 1:
            ap.error("single-file mode takes one building block (use --linked for several)")
        class_name, _, xml, warnings = build(args.bblock_dirs[0])
    except Unmapped as e:
        sys.exit(f"cannot map to XMI: {e}")
    for msg in warnings:
        print(f"warning: {msg}", file=sys.stderr)
    out = Path(args.output or Path(__file__).parent / f"{class_name}.xmi")
    out.write_text(xml, encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
