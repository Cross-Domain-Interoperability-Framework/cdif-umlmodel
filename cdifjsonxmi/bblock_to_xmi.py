"""Generate Canonical XMI (UML 2.5 / XMI 2.5.1) from a CDIF building-block JSON Schema.

Follows the conventions of xmiModels/cdifmodels/cdifmodels.xmi (one package per
vocabulary prefix, dotted xmi:ids, "**CDIF** / Definition" comment bodies). JSON-LD
facts UML has no slot for are written as comment directives (:rdfType:, ...), which
metadataBuildingBlocks/tools/uml_to_schema.py reads back with --comment-directives.
roundtrip.py runs both halves and diffs. See README.md for the mapping table.

Usage:
    python bblock_to_xmi.py <bblock dir> [-o out.xmi]
"""
import argparse
import json
import sys
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

from mapping import (CHOICE_LABEL, DEFINITION_HEADER, JSONLD_KEYWORDS, PACKAGE_NAMES, UML_PRIM,
                     attr_name, is_iri_reference, is_rdf_type_pattern)

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


def directive(name, value=None):
    return f"\n:{name}:" + (f" ``{value}``" if value is not None else "")


def alnum(name):
    return "".join(ch for ch in name if ch.isalnum())


def rdf_prefix(schema):
    """Vocabulary prefix of an object schema: from its @type const, else its first prefixed property."""
    props = schema.get("properties", {})
    const = props.get("@type", {}).get("contains", {}).get("const")
    return (const or next(p for p in props if ":" in p)).split(":")[0]


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

    def __init__(self):
        self.elements = {}
        self.warnings = []

    def add(self, eid, elem):
        if eid in self.elements and self.elements[eid] != elem:
            raise Unmapped(f"two different definitions for {eid}")
        self.elements[eid] = elem
        return eid

    def support(self, eid):
        pkg, name = eid.split(".")
        return self.add(eid, {"kind": "uml:DataType", "pkg": pkg, "name": name,
                              "body": DEFINITION_HEADER + SUPPORT_TYPES[eid], "attrs": []})

    def bblock_stub(self, ref, base_dir):
        """Stub element standing for another building block; its comment names the block."""
        target = (base_dir / ref).resolve().parent
        sources = next((p for p in target.parents if p.name == "_sources"), None)
        if sources is None:
            raise Unmapped(f"$ref {ref} does not point into a _sources tree")
        meta = json.loads((target / "bblock.json").read_text(encoding="utf-8"))
        schema = yaml.safe_load((target / "schema.yaml").read_text(encoding="utf-8"))
        kind = "uml:Class" if "@id" in schema.get("properties", {}) else "uml:DataType"
        pkg, name = rdf_prefix(schema), alnum(meta["name"])
        body = DEFINITION_HEADER + directive("buildingBlock", target.relative_to(sources).as_posix())
        return self.add(f"{pkg}.{name}", {"kind": kind, "pkg": pkg, "name": name, "body": body, "attrs": []})

    def element(self, schema, name, base_dir, where):
        """uml:Class (schema declares @id) or uml:DataType for an object schema; returns its xmi id."""
        props = schema.get("properties", {})
        extra = set(schema) - OBJECT_KEYWORDS
        if extra or schema.get("type") != "object":
            raise Unmapped(f"{where}: keywords {sorted(extra)} / type {schema.get('type')}")
        rdf_type = None
        if "@type" in props:
            type_prop = dict(props["@type"])
            if "default" in type_prop:
                self.warnings.append(f"{where}.@type: dropped default {type_prop.pop('default')!r} "
                                     "(annotation with no UML equivalent)")
            if not is_rdf_type_pattern(type_prop):
                raise Unmapped(f"{where}.@type: {json.dumps(props['@type'])[:160]}")
            rdf_type = type_prop["contains"]["const"]
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
        return self.add(f"{pkg}.{name}", {"kind": kind, "pkg": pkg, "name": name, "body": body, "attrs": attrs})

    def attr_type(self, prop, base_dir, where):
        """Map a property schema to (type ref, upper bound, attribute directives)."""
        prop = {k: v for k, v in prop.items() if k != "description"}
        upper, directives = 1, []
        if prop.get("type") == "array":
            if set(prop) != {"type", "items"}:
                raise Unmapped(f"{where}: array keywords {sorted(set(prop) - {'type', 'items'})}")
            prop, upper = prop["items"], "*"
        if is_iri_reference(prop):
            return ("idref", self.support("common.IriReference")), upper, directives
        branches = prop.get("anyOf")
        if set(prop) == {"anyOf"} and len(branches) == 2 and branches[1] == {"type": "string"} \
                and set(branches[0]) == {"$ref"}:
            directives.append(directive("alsoAcceptsString"))
            prop = branches[0]
        if set(prop) == {"$ref"} and not prop["$ref"].startswith("#"):
            eid = self.bblock_stub(prop["$ref"], base_dir)
        elif prop.get("type") == "object":
            eid = self.element(prop, None, base_dir, where)
        else:
            key = (prop.get("type"), prop.get("format"))
            if set(prop) - {"type", "format"} or key not in SCALARS:
                raise Unmapped(f"{where}: {json.dumps(prop)[:160]}")
            tpkg, tname = SCALARS[key]
            if tpkg == "prim":
                return ("prim", tname), upper, directives
            return ("idref", self.support(f"{tpkg}.{tname}")), upper, directives
        if self.elements[eid]["kind"] == "uml:Class":
            # The schema embeds the node; uml_to_schema.py's default would also accept {"@id"}.
            directives.insert(0, directive("inlineOrByReference", "inline"))
        return ("idref", eid), upper, directives


class Writer:
    def __init__(self):
        self.lines = []

    def add(self, depth, text):
        self.lines.append("  " * depth + text)

    def comment(self, depth, owner, body):
        cid = f"{owner}.comment"
        self.add(depth, f'<ownedComment xmi:type="uml:Comment" xmi:id="{cid}" xmi:uuid="{uid(cid)}">')
        self.lines.append("  " * (depth + 1) + f"<body>{escape(body)}</body>")
        self.add(depth + 1, f'<annotatedElement xmi:idref="{owner}"/>')
        self.add(depth, "</ownedComment>")

    def bound(self, depth, owner, which, value):
        bid = f"{owner}.{which}"
        kind = "LiteralInteger" if which == "lower" else "LiteralUnlimitedNatural"
        self.add(depth, f'<{which}Value xmi:type="uml:{kind}" xmi:id="{bid}" xmi:uuid="{uid(bid)}">')
        self.add(depth + 1, f"<value>{value}</value>")
        self.add(depth, f"</{which}Value>")

    def element(self, depth, eid, elem):
        self.add(depth, f'<packagedElement xmi:type="{elem["kind"]}" xmi:id="{eid}" xmi:uuid="{uid(eid)}">')
        self.add(depth + 1, f"<name>{elem['name']}</name>")
        self.comment(depth + 1, eid, elem["body"])
        for a in elem["attrs"]:
            aid = f"{eid}.{a['name']}"
            self.add(depth + 1, f'<ownedAttribute xmi:type="uml:Property" xmi:id="{aid}" xmi:uuid="{uid(aid)}">')
            self.add(depth + 2, f"<name>{a['name']}</name>")
            kind, ref = a["type"]
            if kind == "prim":
                self.add(depth + 2, f'<type xmi:type="uml:PrimitiveType" href="{UML_PRIM}{ref}"/>')
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
    class_name = alnum(meta["name"])
    mb = ModelBuilder()
    root_id = mb.element(schema, class_name, bdir, bdir.name)

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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bblock_dir")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()
    try:
        class_name, _, xml, warnings = build(args.bblock_dir)
    except Unmapped as e:
        sys.exit(f"cannot map to XMI: {e}")
    for msg in warnings:
        print(f"warning: {msg}", file=sys.stderr)
    out = Path(args.output or Path(__file__).parent / f"{class_name}.xmi")
    out.write_text(xml, encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
