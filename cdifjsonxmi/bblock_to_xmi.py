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
                 "common.IdReference": "A reference to a node defined elsewhere. JSON-LD encoding: "
                 "an object {\"@id\": string} with no other keys.",
                 "common.IriReference": "An IRI-valued property. JSON-LD encoding: either a plain "
                 "string, or a node reference object {\"@id\": string} with no other keys. URI-shape "
                 "values should use the {\"@id\"} form so they participate in RDF entailment."}
OBJECT_KEYWORDS = {"$schema", "title", "description", "type", "properties", "required", "allOf", "anyOf"}
# Simple keywords carried through as a :keywords: / :arrayKeywords: directive (JSON object).
EXTRA_KEYWORDS = {"title", "minLength", "maxLength", "pattern", "minimum", "maximum",
                  "exclusiveMinimum", "exclusiveMaximum"}


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
    """A comment directive line. Names and codes go in ``...``; free text and JSON values
    (titles, descriptions, defaults) are JSON-encoded, so any text fits on one line."""
    return f"\n:{name}:" + (f" ``{value}``" if value is not None else "")


def text_directive(name, value):
    return f"\n:{name}: {json.dumps(value, ensure_ascii=False)}"


def directive_value(directives, name):
    """The JSON value of a text directive in a list built by text_directive(), else None."""
    for x in directives:
        if x.startswith(f"\n:{name}: "):
            return json.loads(x[len(f"\n:{name}: "):])
    return None


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


def type_consts(contains):
    """RDF types an @type 'contains' accepts: {const: T}, {anyOf: [{const: T}, ...]} or
    {enum: [T, ...]}."""
    if set(contains) == {"const"}:
        return [contains["const"]]
    if set(contains) == {"anyOf"} and all(set(b) == {"const"} for b in contains["anyOf"]):
        return [b["const"] for b in contains["anyOf"]]
    if set(contains) == {"enum"}:
        return list(contains["enum"])
    return None


def id_reference(prop):
    """The {"@id": string} node-reference object, as (True, description of @id or None);
    (False, None) if prop is anything else."""
    if prop.get("type") != "object" or set(prop) != {"type", "required", "additionalProperties", "properties"} \
            or prop["required"] != ["@id"] or prop["additionalProperties"] is not False \
            or set(prop["properties"]) != {"@id"}:
        return False, None
    id_prop = dict(prop["properties"]["@id"])
    description = id_prop.pop("description", None)
    return id_prop == {"type": "string"}, description


def rdf_prefix(schema):
    """Vocabulary prefix of an object schema: from its @type, else its first prefixed property."""
    props = schema.get("properties", {})
    parts = rdf_type_parts(props["@type"]) if "@type" in props else None
    first = parts[0][0] if parts else next((p for p in props if ":" in p), None)
    return first.split(":")[0] if first else None


def rdf_type_parts(type_prop):
    """Split an @type property schema into (RDF types, " | " or " & ", allowed types or None,
    default or None, description or None).

    @type is an array of strings that must contain one of several types ( | ):
    contains {const} / {anyOf: [{const}]} / {enum}, or anyOf [{contains: {const}}, ...];
    or all of several types ( & ): allOf [{contains: {const}}, ...] with minItems = their
    number. Optional: default, description, items restricted to an enum (bare or in a
    one-branch anyOf). Returns None for anything else."""
    t = dict(type_prop)
    default = t.pop("default", None)
    description = t.pop("description", None)
    items = t.pop("items", None)
    if isinstance(items, dict) and set(items) == {"anyOf"} and len(items["anyOf"]) == 1:
        items = items["anyOf"][0]
    allowed = None
    if isinstance(items, dict) and set(items) == {"type", "enum"} and items["type"] == "string":
        allowed, items = items["enum"], {"type": "string"}
    if t.pop("type", None) != "array" or items != {"type": "string"}:
        return None
    min_items = t.pop("minItems", None)
    if set(t) == {"contains"}:
        consts, sep = type_consts(t["contains"]), " | "
    elif set(t) == {"anyOf"} and all(set(b) == {"contains"} for b in t["anyOf"]):
        consts, sep = sum((type_consts(b["contains"]) or [None] for b in t["anyOf"]), []), " | "
    elif set(t) == {"allOf"} and all(set(b) == {"contains"} for b in t["allOf"]):
        consts, sep = sum((type_consts(b["contains"]) or [None] for b in t["allOf"]), []), " & "
    else:
        return None
    if not consts or None in consts or min_items != (len(consts) if sep == " & " else 1):
        return None
    if allowed is not None and not set(consts) <= set(allowed):
        return None
    return consts, sep, allowed, default, description


def simple_block(block):
    """An allOf member that is only 'required' and/or an anyOf of pure 'required' branches."""
    return set(block) <= {"required", "anyOf"} and all(
        isinstance(b, dict) and set(b) == {"required"} for b in block.get("anyOf", []))


def other_constraints(schema):
    """allOf members that are not simple (e.g. if/then/else), carried verbatim."""
    return [b for b in schema.get("allOf", []) if not simple_block(b)]


def choice_groups(schema):
    """Each anyOf of pure 'required' branches -> one group: a list of alternatives (lists of names)."""
    groups = []
    for block in [schema] + [b for b in schema.get("allOf", []) if simple_block(b)]:
        if "anyOf" in block:
            for alt in block["anyOf"]:
                if set(alt) != {"required"}:
                    raise Unmapped(f"anyOf branch is not a pure 'required' group: {alt}")
            groups.append([alt["required"] for alt in block["anyOf"]])
    return groups


def required_props(schema):
    req = list(schema.get("required", []))
    for block in [b for b in schema.get("allOf", []) if simple_block(b)]:
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

    def association_id(self, triple):
        return f"{self.link.bb_id}.assoc.{triple}" if self.link else f"assoc.{triple}"

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
        pkg = rdf_prefix(schema) or dir_pkg or split_dir_name(target.parent)[1]
        if pkg is None:
            raise Unmapped(f"$ref {ref}: no @type, prefixed property or directory prefix to place it in a package")
        body = DEFINITION_HEADER + directive("buildingBlock", bb_path)
        eid = self.add(f"{pkg}.{name}", {"kind": kind, "pkg": pkg, "name": name, "body": body, "attrs": []})
        return ("idref", eid), kind

    def add_unique(self, pkg, name, elem):
        """Add elem under name, or name2, name3, ... if name is taken by a different element."""
        candidate, n = name, 1
        while True:
            eid = self.element_id(pkg, candidate)
            if eid not in self.elements or self.elements[eid] == {**elem, "name": candidate}:
                return self.add(eid, {**elem, "name": candidate})
            n += 1
            candidate = f"{name}{n}"

    def attribute(self, role, prop, required, owner, base_dir, where, json_name=None):
        """The UML attribute (dict) for property schema prop of element owner = (name, pkg)."""
        type_ref, upper, directives, target_class, items_desc = self.attr_type(
            prop, base_dir, where, required=required, owner=owner, role=role)
        # minItems that the lower bound already implies (1 on a required array) is dropped.
        if required and directive_value(directives, "minItems") == 1:
            directives = [x for x in directives if not x.startswith("\n:minItems:")]
        desc = prop.get("description")
        if items_desc is not None:
            if desc is not None:
                directives.append(text_directive("itemsDescription", items_desc))
            else:
                desc = items_desc
                directives.append(directive("descriptionOnItems"))
        if json_name:
            directives.append(text_directive("jsonName", json_name))
        return {"name": role, "type": type_ref, "upper": upper, "lower": 1 if required else 0,
                # A reference to a uml:Class is the navigable end of an association,
                # named <Owner>_<role>_<Target> as in cdifmodels.xmi.
                "assoc": target_class and self.association_id(f"{owner[0]}_{role}_{target_class}"),
                "body": None if desc is None and not directives
                else DEFINITION_HEADER + (desc or "") + "".join(directives)}

    def alternative_name(self, branch, index):
        """Attribute name for one alternative of a union, from what the alternative is."""
        b = {k: v for k, v in branch.items() if k not in ("description", "default", *EXTRA_KEYWORDS)}
        if b.get("type") == "array":
            return self.alternative_name(b.get("items", {}), index) + "List"
        if is_iri_reference(b):
            return "iri"
        if id_reference(b)[0]:
            return "idReference"
        ref = b.get("$ref", "") if set(b) == {"$ref"} else ""
        if ref.startswith("#/$defs/"):
            name = ref[len("#/$defs/"):]
        elif ref:
            name = split_dir_name((Path("x") / ref).parent)[0]
        elif b.get("type") == "object" and "@type" in b.get("properties", {}):
            parts = rdf_type_parts(b["properties"]["@type"])
            name = parts[0][0].split(":")[1] if parts else f"option{index + 1}"
        elif "enum" in b:
            name = "enum"
        elif set(b) in ({"anyOf"}, {"oneOf"}):
            name = "choice"
        elif b.get("type") in ("string", "integer", "boolean", "number"):
            name = b["type"]
        else:
            name = f"option{index + 1}"
        return name[:1].lower() + name[1:]

    def union(self, branches, keyword, owner, role, base_dir, where, name=None, body=""):
        """A union DataType (ISO 19103 «Union» style, marked :union: ``anyOf|oneOf``) with one
        optional attribute per alternative, in order; returns its xmi id."""
        name = name or f"{owner[0]}{role[:1].upper()}{role[1:]}Choice"
        attrs, used = [], {}
        for i, branch in enumerate(branches):
            alt = self.alternative_name(branch, i)
            used[alt] = used.get(alt, 0) + 1
            if used[alt] > 1:
                alt = f"{alt}{used[alt]}"
            attrs.append(self.attribute(alt, branch, False, (name, owner[1]), base_dir, f"{where}|{i}"))
        elem = {"kind": "uml:DataType", "pkg": owner[1], "name": name,
                "body": DEFINITION_HEADER + body + directive("union", keyword), "attrs": attrs}
        return self.add_unique(owner[1], name, elem)

    def enumeration(self, literals, owner, role, where):
        """A uml:Enumeration for an inline string enum, named after the property."""
        if not all(isinstance(v, str) for v in literals):
            raise Unmapped(f"{where}: non-string enum {literals}")
        elem = {"kind": "uml:Enumeration", "pkg": owner[1], "name": role[:1].upper() + role[1:],
                "body": DEFINITION_HEADER, "attrs": [], "literals": list(literals)}
        return self.add_unique(owner[1], elem["name"], elem)

    def element(self, schema, name, base_dir, where, fallback_name=None, fallback_pkg=None):
        """uml:Class (schema declares @id) or uml:DataType for an object schema; returns its xmi id.
        A schema that is only a union (anyOf / oneOf of alternatives) becomes a union DataType."""
        for keyword in ("anyOf", "oneOf"):
            if keyword in schema and "properties" not in schema \
                    and not all(set(b) == {"required"} for b in schema[keyword]):
                extra = set(schema) - {"$schema", "title", "description", keyword}
                if extra:
                    raise Unmapped(f"{where}: union with keywords {sorted(extra)}")
                pkg = next((rdf_prefix(b) for b in schema[keyword] if isinstance(b, dict)
                            and rdf_prefix(b)), None)
                body = schema.get("description", "")
                if "title" in schema:
                    body += text_directive("title", schema["title"])
                return self.union(schema[keyword], keyword, (name, pkg), "", base_dir, where,
                                  name=name, body=body)
        props = schema.get("properties", {})
        extra = set(schema) - OBJECT_KEYWORDS
        if extra or schema.get("type") != "object":
            raise Unmapped(f"{where}: keywords {sorted(extra)} / type {schema.get('type')}")
        rdf_type = allowed_types = type_default = type_description = type_schema = None
        if "@type" in props:
            parts = rdf_type_parts(props["@type"])
            if parts is None:
                # An @type constraint in another form (JSON-LD typing): carried verbatim; a
                # const or default in it still names the element.
                type_schema = props["@type"]
                found = [v for v in (json.dumps(type_schema),) for v in
                         __import__("re").findall(r'"(?:const|default)": "([\w-]+:[\w-]+)"', v)]
                parts = ([found[0]], " | ", None, None, None) if found else None
            if parts is not None:
                rdf_types, sep, allowed_types, type_default, type_description = parts
                rdf_type = sep.join(rdf_types)
            if isinstance(type_default, str):
                type_default = [type_default]  # @type is an array; a bare string default is wrapped
            if type_default is not None and not (
                    isinstance(type_default, list) and all(isinstance(t, str) for t in type_default)):
                raise Unmapped(f"{where}.@type: default is not a string or list of strings: {type_default!r}")
        id_description = None
        if "@id" in props:
            id_prop = dict(props["@id"])
            id_description = id_prop.pop("description", None)
            if id_prop != {"type": "string"}:
                raise Unmapped(f"{where}.@id: {props['@id']}")
        named_from_type = name is None
        if name is None:
            if rdf_type:
                name = rdf_types[0].split(":")[1]
            elif fallback_name:
                name = fallback_name
            else:
                raise Unmapped(f"{where}: inline object has no @type const to name it")
        pkg = rdf_prefix(schema) or fallback_pkg
        required = required_props(schema)
        groups = choice_groups(schema)
        for r in (r for g in groups for alt in g for r in alt):
            if r not in props:
                self.warnings.append(f"{where}: choice constraint names '{r}', which is not a declared property")

        body = DEFINITION_HEADER + schema.get("description", "")
        if "title" in schema:
            body += text_directive("title", schema["title"])
        if not rdf_type and pkg:
            # the prefix of this element's property keys (otherwise that of its @type)
            body += directive("prefix", pkg)
        if id_description is not None:
            body += text_directive("idDescription", id_description)
        if type_schema is not None:
            body += text_directive("typeSchema", type_schema)
            if "@type" not in required:
                body += directive("typeOptional")
        elif rdf_type:
            body += directive("rdfType", rdf_type)
            if type_description is not None:
                body += text_directive("typeDescription", type_description)
            if allowed_types:
                body += directive("allowedTypes", " | ".join(allowed_types))
            if type_default is not None:
                body += directive("typeDefault", " | ".join(type_default))
            if "@type" not in required:
                body += directive("typeOptional")
        if "@context" in props:
            # JSON-LD context requirements are serialization, not model: carried verbatim.
            body += text_directive("contextSchema", props["@context"])
        for unknown in set(props) & JSONLD_KEYWORDS - {"@type", "@id", "@context"}:
            raise Unmapped(f"{where}.{unknown}: JSON-LD keyword property not handled")
        for constraint in other_constraints(schema):
            body += text_directive("constraint", constraint)
        if groups:
            # JSON property keys, verbatim (they may be @id or carry another prefix)
            body += f"\n{CHOICE_LABEL}\n" + "\n".join(
                "- ``" + " | ".join(" & ".join(alt) for alt in g) + "``" for g in groups)

        attrs = []
        for pname, prop in props.items():
            if pname in JSONLD_KEYWORDS:
                continue
            role = attr_name(pname, pkg)
            attrs.append(self.attribute(role, prop, pname in required, (name, pkg), base_dir,
                                        f"{where}.{pname}",
                                        json_name=pname if f"{pkg}:{role}" != pname else None))
        kind = "uml:Class" if "@id" in props else "uml:DataType"
        elem = {"kind": kind, "pkg": pkg, "name": name, "body": body, "attrs": attrs}
        if named_from_type:
            # Inline objects are named after their @type, which several may share.
            return self.add_unique(pkg, name, elem)
        return self.add(self.element_id(pkg, name), elem)

    def attr_type(self, prop, base_dir, where, required=False, owner=None, role=""):
        """Map a property schema to (type ref, upper bound, attribute directives, name of the
        target uml:Class or None, description found on array items or None)."""
        prop = {k: v for k, v in prop.items() if k != "description"}
        upper, directives, items_desc = 1, [], None
        if "default" in prop:
            directives.append(text_directive("default", prop.pop("default")))
        if prop.get("type") == "array":
            array_keywords = {k: prop.pop(k) for k in list(prop) if k in EXTRA_KEYWORDS}
            if array_keywords:
                directives.append(text_directive("arrayKeywords", array_keywords))
            if set(prop) - {"type", "items", "minItems"}:
                raise Unmapped(f"{where}: array keywords {sorted(set(prop) - {'type', 'items', 'minItems'})}")
            if "minItems" in prop:
                # uml_to_schema.py derives minItems from the lower bound; element() keeps
                # this directive only where the two disagree.
                directives.append(text_directive("minItems", prop["minItems"]))
            elif required:
                directives.append(text_directive("minItems", None))
            prop, upper = dict(prop["items"]), "*"
            items_desc = prop.pop("description", None)
            if "default" in prop:
                directives.append(text_directive("itemsDefault", prop.pop("default")))
        prop = dict(prop)
        keywords = {k: prop.pop(k) for k in list(prop) if k in EXTRA_KEYWORDS}
        if keywords:
            directives.append(text_directive("keywords", keywords))
        while set(prop) == {"$ref"} and prop["$ref"].startswith("#/$defs/"):
            name = prop["$ref"][len("#/$defs/"):]
            if name not in self.defs:
                raise Unmapped(f"{where}: unresolved {prop['$ref']}")
            prop = self.defs[name]
        if is_iri_reference(prop):
            return self.support("common.IriReference"), upper, directives, None, items_desc
        is_id_ref, id_desc = id_reference(prop)
        if is_id_ref:
            if id_desc is not None:
                directives.append(text_directive("idRefDescription", id_desc))
            return self.support("common.IdReference"), upper, directives, None, items_desc
        branches = prop.get("anyOf")
        if set(prop) == {"anyOf"} and len(branches) == 2 and {"type": "string"} in branches:
            other = branches[1 - branches.index({"type": "string"})]
            if set(other) == {"$ref"}:
                # anyOf [X, string] or anyOf [string, X]
                first = branches[0] == {"type": "string"}
                directives.append(directive("alsoAcceptsString", "first" if first else None))
                prop = other
                while set(prop) == {"$ref"} and prop["$ref"].startswith("#/$defs/"):
                    name = prop["$ref"][len("#/$defs/"):]
                    if name not in self.defs:
                        raise Unmapped(f"{where}: unresolved {prop['$ref']}")
                    prop = self.defs[name]
        for keyword in ("anyOf", "oneOf"):
            if set(prop) == {keyword}:
                eid = self.union(prop[keyword], keyword, owner, role, base_dir, where)
                return ("idref", eid), upper, directives, None, items_desc
        if prop.get("type") == "string" and set(prop) == {"type", "enum"}:
            return ("idref", self.enumeration(prop["enum"], owner, role, where)), upper, directives, None, items_desc
        if set(prop) == {"$ref"} and not prop["$ref"].startswith("#"):
            type_ref, kind = self.bblock_ref(prop["$ref"], base_dir)
        elif prop.get("type") == "object":
            eid = self.element(prop, None, base_dir, where,
                               fallback_name=f"{owner[0]}{role[:1].upper()}{role[1:]}" if owner else None,
                               fallback_pkg=owner[1] if owner else None)
            type_ref, kind = ("idref", eid), self.elements[eid]["kind"]
        else:
            key = (prop.get("type"), prop.get("format"))
            if set(prop) - {"type", "format"} or key not in SCALARS:
                raise Unmapped(f"{where}: {json.dumps(prop)[:160]}")
            tpkg, tname = SCALARS[key]
            if tpkg == "prim":
                return ("prim", tname), upper, directives, None, items_desc
            return self.support(f"{tpkg}.{tname}"), upper, directives, None, items_desc
        if kind != "uml:Class":
            return type_ref, upper, directives, None, items_desc
        # The schema embeds the node; uml_to_schema.py's default would also accept {"@id"}.
        directives.insert(0, directive("inlineOrByReference", "inline"))
        return type_ref, upper, directives, type_ref[1].rsplit(".", 1)[-1], items_desc


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
            if a.get("assoc"):
                self.add(depth + 2, f'<association xmi:idref="{a["assoc"]}"/>')
            if a["body"] is not None:
                self.comment(depth + 2, aid, a["body"])
            self.add(depth + 1, "</ownedAttribute>")
        for lit in elem.get("literals", []):
            lid = f"{eid}.{lit}"
            self.add(depth + 1, f'<ownedLiteral xmi:type="uml:EnumerationLiteral" xmi:id="{escape(lid)}" '
                                f'xmi:uuid="{self.uid(lid)}">')
            self.add(depth + 2, f"<name>{escape(lit)}</name>")
            self.add(depth + 1, "</ownedLiteral>")
        self.add(depth, "</packagedElement>")

    def associations(self, depth, eid, elem):
        """One uml:Association per class-typed attribute of elem, as in cdifmodels.xmi: the
        attribute is the navigable member end; the association owns the other end, typed by
        elem, with multiplicity 0..* (the schema says nothing about it)."""
        for a in elem["attrs"]:
            if not a.get("assoc"):
                continue
            assoc, end = a["assoc"], f'{a["assoc"]}.ownedEnd'
            self.add(depth, f'<packagedElement xmi:type="uml:Association" xmi:id="{assoc}" '
                            f'xmi:uuid="{self.uid(assoc)}">')
            self.add(depth + 1, f"<name>{assoc.rsplit('.', 1)[-1]}</name>")
            self.add(depth + 1, f'<ownedEnd xmi:type="uml:Property" xmi:id="{end}" xmi:uuid="{self.uid(end)}">')
            self.add(depth + 2, f'<type xmi:idref="{eid}"/>')
            self.bound(depth + 2, end, "lower", 0)
            self.bound(depth + 2, end, "upper", "*")
            self.add(depth + 2, f'<association xmi:idref="{assoc}"/>')
            self.add(depth + 1, "</ownedEnd>")
            self.add(depth + 1, f'<memberEnd xmi:idref="{end}"/>')
            self.add(depth + 1, f'<memberEnd xmi:idref="{eid}.{a["name"]}"/>')
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
        for eid, elem in sorted(mb.elements.items()):
            if elem["pkg"] == pkg:
                out.associations(3, eid, elem)
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
    order = [root_id] + sorted(e for e in mb.elements if e != root_id)
    for eid in order:
        out.element(3, eid, mb.elements[eid])
    for eid in order:
        out.associations(3, eid, mb.elements[eid])
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
