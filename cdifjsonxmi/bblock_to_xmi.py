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
import re
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

HERE = Path(__file__).resolve().parent
CLASSIFICATION_FILE = HERE / "classification.yaml"
CDIFMODELS_XMI = HERE.parent / "xmiModels" / "cdifmodels" / "cdifmodels.xmi"
_KINDS = None  # (overrides, Achim's kinds), loaded on first use


def _kinds():
    """(overrides from classification.yaml, {name: kind} from cdifmodels.xmi where unambiguous)."""
    global _KINDS
    if _KINDS is None:
        overrides = {}
        if CLASSIFICATION_FILE.exists():
            overrides = yaml.safe_load(CLASSIFICATION_FILE.read_text(encoding="utf-8")) or {}
        achim = {}
        if CDIFMODELS_XMI.exists():
            import xml.etree.ElementTree as ET
            x = "{http://www.omg.org/spec/XMI/20131001}"
            for e in ET.parse(CDIFMODELS_XMI).getroot().iter("packagedElement"):
                if e.get(x + "type") in ("uml:Class", "uml:DataType"):
                    achim.setdefault(e.findtext("name"), set()).add(e.get(x + "type")[len("uml:"):])
        _KINDS = (overrides, {n: k.pop() for n, k in achim.items() if len(k) == 1})
    return _KINDS


def classify(name, schema):
    """uml:Class or uml:DataType for the object schema of element `name` (see classification.yaml)."""
    overrides, achim = _kinds()
    kind = overrides.get(name) or achim.get(name)
    if kind is None:
        props = schema.get("properties", {})
        kind = "Class" if "@id" in props or "@type" in props else "DataType"
    return f"uml:{kind}"

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
# Array keywords carried verbatim in :arrayKeywords: (contains may hold a schema).
ARRAY_KEYWORDS = {"contains", "minContains", "maxContains", "maxItems", "uniqueItems", "allOf"}
# Object keywords UML has no form for, carried as one :constraint: (an allOf member).
CONSTRAINT_KEYWORDS = ("if", "then", "else", "not", "oneOf")


# The building block that is the {"@id"} node reference: a reference alternative like {"@id"}.
OBJECT_REFERENCE_BLOCK = "cdifDataType/objectReference"
# characters XML parsing would normalize or strip, written as character references
CHAR_REFS = {"\r": "&#13;", "\n": "&#10;", "\t": "&#9;"}


class Unmapped(Exception):
    pass


def uid(xmi_id):
    return str(uuid.uuid5(NS, xmi_id))


# Linked mode: one XMI file per building block at <out root>/<path under _sources>/<dir>.xmi,
# identified like the building-block register does it.
REGISTER_PREFIX = "cdif.bbr.metadata."                   # bblocks-config.yaml identifier-prefix
REGISTER_URI = "https://w3id.org/cdif/bbr/metadata/"
COMMON_TYPES_FILE = "cdifCommonTypes.xmi"                # XSD datatypes and IriReference, shared
SHARED_TYPES_FILE = "cdifSharedTypes.xmi"                # abstract bases of RDF types defined in several places
SHARED_PREFIX = "cdif.shared."
SHARED_UNIONS_FILE = "cdifSharedUnions.xmi"              # unions used by more than one block
UNION_NS = "cdif.union"
# Per-use annotations of a union alternative: not part of the union's identity.
ALTERNATIVE_NOTES = ("description", "default", "title", "$comment", "examples")


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

    def href_to_shared(self):
        return os.path.relpath(self.out_root / SHARED_TYPES_FILE, self.file.parent).replace(os.sep, "/")

    def href_to_unions(self):
        return os.path.relpath(self.out_root / SHARED_UNIONS_FILE, self.file.parent).replace(os.sep, "/")

    def uid(self, xmi_id):
        """uuid5 of the element's URI: the block's register URI + '#' + the id local to the block."""
        if xmi_id.startswith(self.bb_id + "."):
            return str(uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"{REGISTER_URI}{self.bb_path}#{xmi_id[len(self.bb_id) + 1:]}"))
        if xmi_id == self.bb_id:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{REGISTER_URI}{self.bb_path}"))
        return common_uid(xmi_id)


class SharedLink(Link):
    """A Link for a shared file (not a building block): ids under ns_id, uuids like the common types."""

    def __init__(self, file_name, ns_id, out_root):
        self.bb_path, self.bb_id, self.out_root = None, ns_id, out_root
        self.file = out_root / file_name

    def uid(self, xmi_id):
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


def local_role(role):
    """An attribute name without another vocabulary's prefix, for naming elements after it
    (dcterms_creator -> creator; isDefinedBy_RepresentedVariable is kept)."""
    head, sep, tail = role.partition("_")
    return tail if sep and head.islower() and head.isalpha() and tail else role


def cap(name):
    return name[:1].upper() + name[1:]


def recursive_defs(defs):
    """The keys of the $defs entries that refer back to themselves, directly or through other
    entries: each is built once, as the element named after it, wherever it's used first."""
    def local_refs(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                yield ref[len("#/$defs/"):]
            for v in node.values():
                yield from local_refs(v)
        elif isinstance(node, list):
            for v in node:
                yield from local_refs(v)
    graph = {k: set(local_refs(v)) & set(defs) for k, v in (defs or {}).items()}
    found = set()
    for start in graph:
        stack, seen = list(graph[start]), set()
        while stack:
            k = stack.pop()
            if k == start:
                found.add(start)
                break
            if k not in seen:
                seen.add(k)
                stack.extend(graph.get(k, ()))
    return found


def root_name(bdir, schema):
    """The UML name of a block's root element: from its directory, + "Profile" where one of the
    block's definitions has that name (cdifDataStructure)."""
    name = split_dir_name(bdir)[0]
    alias = schema.get("allOf", [{}])[0].get("$ref") if len(schema.get("allOf", [])) == 1 else None
    if alias == f"#/$defs/{name}":
        return name  # the root is that definition (skosConcept: allOf [$ref Concept])
    return name + "Profile" if name in schema.get("$defs", {}) else name


def block_root(bdir, _seen=()):
    """(name, kind) of a block's root element, as ModelBuilder.element builds it: a block that
    specializes others (allOf of their $refs) has their kind, a union root is a DataType."""
    bdir = Path(bdir).resolve()
    schema = yaml.safe_load((bdir / "schema.yaml").read_text(encoding="utf-8"))
    name = root_name(bdir, schema)
    members = schema.get("allOf", [])
    refs = [m["$ref"] for m in members if isinstance(m, dict) and set(m) == {"$ref"}
            and not m["$ref"].startswith("#") and "#" not in m["$ref"]]
    if refs and "properties" not in schema and bdir not in _seen:
        kinds = [block_root((bdir / r).resolve().parent, _seen + (bdir,))[1] for r in refs]
        return name, ("uml:Class" if "uml:Class" in kinds else kinds[0])
    if "properties" not in schema and ({"anyOf", "oneOf"} & set(schema)):
        return name, "uml:DataType"
    if len(members) == 1 and set(members[0]) == {"$ref"} and members[0]["$ref"].startswith("#/$defs/"):
        key = members[0]["$ref"][len("#/$defs/"):]  # a root alias of one of its definitions
        return name, classify(key, schema["$defs"][key])
    return name, classify(name, schema)


def is_partial(prop):
    """A property schema with no type of its own that only further constrains a property
    another schema defines (cdifProvenance's schema:subjectOf: {properties: {dcterms:conformsTo:
    ...}})."""
    return isinstance(prop, dict) and not {"type", "$ref", "anyOf", "oneOf", "enum", "const"} & set(prop) \
        and {"properties", "required", "contains", "not", "if"} & set(prop) \
        and set(prop) <= {"properties", "required", "description", "contains", "minItems", "maxItems",
                          "items", "not", "if", "then", "else", "allOf"}


def is_conditional(prop):
    """A schema of conditions only: if / then / else, not, required, or an allOf of those."""
    return isinstance(prop, dict) and set(prop) <= {"if", "then", "else", "not", "required", "allOf"}         and bool({"if", "not"} & set(prop) or prop.get("allOf"))         and all(is_conditional(m) for m in prop.get("allOf", []))


def is_refinement(prop):
    """Conditions and partial schemas only (cdifManifest's distribution items: allOf [{properties:
    {schema:hasPart: ...}}, {if: ..., then: ...}]), no type of its own."""
    return is_conditional(prop) or is_partial(prop) or (
        isinstance(prop, dict) and set(prop) == {"allOf"} and all(is_refinement(m) for m in prop["allOf"]))


def role_key(prop):
    """The key of a role-keyed wrapper, an object that only requires one key it doesn't declare
    (generatedBy's {required: [schema:instrument]}), else None."""
    p = {k: v for k, v in prop.items() if k != "description"}
    if p.get("type") == "object" and set(p) == {"type", "required"} and len(p["required"]) == 1:
        return p["required"][0]
    return None


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


def split_alternative(branch):
    """(alternative without its per-use annotations, those annotations). The description of an
    {"@id"} reference's @id counts as an annotation too ("idRefDescription")."""
    b = {k: v for k, v in branch.items() if k not in ALTERNATIVE_NOTES}
    notes = {k: branch[k] for k in ALTERNATIVE_NOTES if k in branch}
    at_id = b.get("properties", {}).get("@id") if b.get("type") == "object" else None
    if isinstance(at_id, dict) and "description" in at_id and set(b.get("properties", {})) == {"@id"}:
        notes["idRefDescription"] = at_id["description"]
        b = {**b, "properties": {"@id": {k: v for k, v in at_id.items() if k != "description"}}}
    return b, notes


def attr_signature(a):
    """Identity of an attribute for finding ones identical across elements; None for those
    that depend on their owner (association ends, types local to the owner's block)."""
    if a.get("assoc") or a["type"][0] == "idref":
        return None
    return json.dumps([a["name"], a["type"], a["lower"], a["upper"], a["body"]])


def required_props(schema):
    req = list(schema.get("required", []))
    for block in [b for b in schema.get("allOf", []) if simple_block(b)]:
        req += [r for r in block.get("required", []) if r not in req]
    return req


class ModelBuilder:
    """Collects UML elements (xmi id -> element dict) from a building block and what it references."""

    def __init__(self, defs, link=None, plan=None, block_name=None, union_plan=None, export_defs=None,
                 default_pkg=None):
        self.elements = {}
        self.warnings = []
        self.defs = defs  # the root schema's $defs, for #/$defs/ references
        # Linked mode (one file per building block): a Link for this block; element ids are
        # register-qualified, and other blocks and the shared support types are href'd.
        self.link = link
        # Linked mode, second pass: {(rdf type, kind): {"base_id", "common"}} for RDF types that
        # several elements define; those elements specialize a shared abstract base.
        self.plan = plan or {}
        self.block_name = block_name
        # Unions by content: {key: (type ref, annotations of the defining occurrence, alt names)};
        # union_plan holds the ones in the shared unions file (linked mode, second pass).
        self.unions = {}
        self.union_plan = union_plan or {}
        self.union_occurrences = []  # (key, branches, keyword, base_dir, defs, shareable), for pass 1
        # $defs entries other blocks reference ("...schema.yaml#/$defs/K"): built as elements named K.
        self.export_defs = set(export_defs or ())
        self.def_elements = {}       # K -> xmi id, for those (and for recursive $defs)
        self.def_kinds = {}          # K -> uml kind of that element
        self.def_stack = []          # $defs keys being expanded inline (recursion guard)
        self.exports_needed = set()  # (bb path, K) this block references in other blocks, for pass 1
        self.default_pkg = default_pkg
        # (owner name, role) of the property whose union is being built, and that context
        # handed to one alternative's inline object (union -> map_value), for naming them
        self.use, self.alt_use = None, None
        self._recursive = {}  # id of a $defs table -> its recursive keys (self.defs may be swapped)

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
        dir_pkg = split_dir_name(target)[1]
        name, kind = block_root(target)
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

    def resolve_local(self, prop, where):
        """Follow #/$defs/ aliases; returns (schema, the last $defs key followed or None)."""
        key = None
        while set(prop) == {"$ref"} and prop["$ref"].startswith("#/$defs/"):
            key = prop["$ref"][len("#/$defs/"):]
            if key not in self.defs:
                raise Unmapped(f"{where}: unresolved {prop['$ref']}")
            prop = self.defs[key]
        return prop, key

    def recursive_keys(self):
        if id(self.defs) not in self._recursive:
            self._recursive[id(self.defs)] = (self.defs, recursive_defs(self.defs))
        return self._recursive[id(self.defs)][1]

    def build_def(self, key, base_dir, where):
        """The element for $defs entry `key` of this block, named `key` (exported for other
        blocks' references): an object, union or enum; None for anything else."""
        if key in self.def_elements:
            return self.def_elements[key]
        d, final = self.resolve_local({"$ref": f"#/$defs/{key}"}, where)
        if final != key:
            eid = self.build_def(final, base_dir, where)
            self.def_kinds[key] = self.def_kinds.get(final)
        else:
            d = {k: v for k, v in d.items() if k != "$comment"} if isinstance(d, dict) else d
            pkg = rdf_prefix(d) if isinstance(d, dict) and d.get("properties") else None
            pkg = pkg or self.default_pkg
            is_object = d.get("type") == "object" or ("properties" in d and "type" not in d)
            is_union = set(d) - {"description"} in ({"anyOf"}, {"oneOf"})
            is_enum = d.get("type") == "string" and set(d) - {"description"} == {"type", "enum"}
            if not (is_object or is_union or is_enum):
                self.def_elements[key] = None
                return None
            # registered before building, so a definition that refers to itself resolves here
            self.def_elements[key] = self.element_id(pkg, key)
            self.def_kinds[key] = (classify(key, d) if is_object else
                                   "uml:DataType" if is_union else "uml:Enumeration")
            if is_object:
                eid = self.element(d, key, base_dir, f"{where}#/$defs/{key}", fallback_pkg=pkg)
            elif is_union:
                keyword = "anyOf" if "anyOf" in d else "oneOf"
                eid = self.union(d[keyword], keyword, (key, pkg), "", base_dir, where, name=key,
                                 body=d.get("description", ""))
            else:
                eid = self.enumeration(d["enum"], (key, pkg), key, where)
            if key in self.export_defs:
                self.elements[eid]["body"] += directive("exportedDef")
        self.def_elements[key] = eid
        return eid

    def fragment_ref(self, ref, base_dir, where):
        """A $ref into another block's $defs. Returns (type ref, kind, ``bb path#/$defs/K``).
        An object, union or enum definition is the element K in that block's file; anything
        else is mapped like an inline property, in that block's context."""
        path, _, frag = ref.partition("#")
        if not frag.startswith("/$defs/"):
            raise Unmapped(f"{where}: $ref fragment {frag!r} is not into $defs")
        target = (base_dir / path).resolve().parent
        sources = next((p for p in target.parents if p.name == "_sources"), None)
        if sources is None:
            raise Unmapped(f"$ref {ref} does not point into a _sources tree")
        bb_path = target.relative_to(sources).as_posix()
        schema = yaml.safe_load((target / "schema.yaml").read_text(encoding="utf-8"))
        tdefs = schema.get("$defs", {})
        key = frag[len("/$defs/"):]
        if key not in tdefs:
            raise Unmapped(f"{where}: {ref} has no such $defs entry")
        d = tdefs[key]
        while set(d) == {"$ref"} and d["$ref"].startswith("#/$defs/"):  # alias inside that block
            key = d["$ref"][len("#/$defs/"):]
            d = tdefs[key]
        schema_ref = f"{bb_path}#{frag}"
        is_object = d.get("type") == "object" and not id_reference(d)[0] and not is_iri_reference(d)
        is_union = set(d) - {"description"} in ({"anyOf"}, {"oneOf"}) and not is_iri_reference(d)
        is_enum = d.get("type") == "string" and set(d) - {"description"} == {"type", "enum"}
        if is_object or is_union or is_enum:
            self.exports_needed.add((bb_path, key))
            kind = classify(key, d) if is_object else ("uml:Enumeration" if is_enum else "uml:DataType")
            if self.link:
                return ("href", f"{self.link.href_to(bb_path)}#{bb_id(bb_path)}.{key}"), kind, schema_ref
            pkg = rdf_prefix(d) or split_dir_name(target)[1] or split_dir_name(target.parent)[1] or "cdif"
            body = DEFINITION_HEADER + directive("buildingBlock", schema_ref)
            eid = self.add(f"{pkg}.{key}", {"kind": kind, "pkg": pkg, "name": key, "body": body, "attrs": []})
            return ("idref", eid), kind, schema_ref
        # a scalar / reference definition: map it as written, in the other block's context
        saved = self.defs
        self.defs = tdefs
        try:
            type_ref, _, _, target_class, _ = self.attr_type(dict(d), target, where)
        finally:
            self.defs = saved
        return type_ref, ("uml:Class" if target_class else None), schema_ref

    def alternative_name(self, branch, index):
        """Attribute name for one alternative of a union, from what the alternative is."""
        b = {k: v for k, v in branch.items() if k not in ("description", "default", *EXTRA_KEYWORDS)}
        if b.get("type") == "array":
            return self.alternative_name(b.get("items", {}), index) + "List"
        if is_iri_reference(b):
            return "iri"
        if id_reference(b)[0]:
            return "idReference"
        if role_key(b):
            return role_key(b).split(":")[-1] + "Role"
        ref = b.get("$ref", "") if set(b) == {"$ref"} else ""
        while ref.startswith("#/$defs/") and isinstance(self.defs.get(ref[len("#/$defs/"):]), dict) \
                and set(self.defs[ref[len("#/$defs/"):]]) == {"$ref"}:
            ref = self.defs[ref[len("#/$defs/"):]]["$ref"]  # an alias of another reference
        if ref.startswith("#/$defs/"):
            name = ref[len("#/$defs/"):]
        elif "#/$defs/" in ref:
            name = ref.rsplit("/", 1)[-1]  # another block's $defs entry: named by its key
        elif ref:
            name = split_dir_name((Path("x") / ref).parent)[0]
        elif b.get("type") == "object" and "@type" in b.get("properties", {}) \
                and rdf_type_parts(b["properties"]["@type"]):
            name = rdf_type_parts(b["properties"]["@type"])[0][0].split(":")[1]
        elif b == {"type": "object"}:
            name = "object"  # any object
        elif b.get("type") == "object" and self.use:
            # an object with no RDF type to name it: named after the property using the union
            name = local_role(self.use[1])
        elif "enum" in b:
            name = "enum"
        elif set(b) in ({"anyOf"}, {"oneOf"}):
            name = "choice"
        elif b.get("type") in ("string", "integer", "boolean", "number"):
            name = b["type"]
        else:
            name = f"option{index + 1}"
        return name[:1].lower() + name[1:]

    def canonical(self, node, base_dir, expanding=()):
        """A schema fragment with local $defs resolved and file $refs made absolute (bb:<path>),
        so the same content compares equal in any block. A recursive $defs reference is left as
        is (and then only matches the same recursion)."""
        if isinstance(node, list):
            return [self.canonical(x, base_dir, expanding) for x in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and set(node) == {"$ref"}:
            key = ref[len("#/$defs/"):] if ref.startswith("#/$defs/") else None
            if key in self.defs and key not in expanding:
                return self.canonical(self.defs[key], base_dir, expanding + (key,))
            if key is not None:
                return node
            if not ref.startswith("#"):
                path, _, frag = ref.partition("#")
                target = (base_dir / path).resolve().parent
                sources = next((p for p in target.parents if p.name == "_sources"), None)
                return {"$ref": "bb:" + (target.relative_to(sources).as_posix() if sources else str(target))
                        + (f"#{frag}" if frag else "")}
        return {k: self.canonical(v, base_dir, expanding) for k, v in node.items()}

    def verbatim(self, node, base_dir, where):
        """A schema fragment for a verbatim directive: local $refs to aliases of a block followed,
        file $refs written as bb:<path under _sources>/<file>[#fragment], for uml_to_schema.py
        to make relative to wherever it writes the schema."""
        if isinstance(node, list):
            return [self.verbatim(x, base_dir, where) for x in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str):
            while ref.startswith("#/$defs/") and set(self.defs.get(ref[len("#/$defs/"):], {})) == {"$ref"}:
                ref = self.defs[ref[len("#/$defs/"):]]["$ref"]
            if ref.startswith("#/$defs/"):
                # a definition of this block: its element, kept in $defs (:exportedDef:)
                key = ref[len("#/$defs/"):]
                eid = self.build_def(key, base_dir, where)
                if not eid and key in self.defs and key not in self.def_stack:
                    # a definition that can't be an element (an array): its content, inline
                    self.def_stack.append(key)
                    try:
                        return self.verbatim({**self.defs[key], **{k: v for k, v in node.items() if k != "$ref"}},
                                             base_dir, where)
                    finally:
                        self.def_stack.pop()
                if not eid:
                    raise Unmapped(f"{where}: local {ref} in a verbatim schema")
                if directive("exportedDef") not in self.elements[eid]["body"]:
                    self.elements[eid]["body"] += directive("exportedDef")
                return {k: (v if k == "$ref" else self.verbatim(v, base_dir, where)) for k, v in node.items()}
            if ref.startswith("#"):
                raise Unmapped(f"{where}: local {ref} in a verbatim schema")
            path, _, frag = ref.partition("#")
            target = (base_dir / path).resolve()
            sources = next((p for p in target.parents if p.name == "_sources"), None)
            if sources is None:
                raise Unmapped(f"{where}: {ref} is outside _sources")
            node = {**node, "$ref": "bb:" + target.relative_to(sources).as_posix() + (f"#{frag}" if frag else "")}
        return {k: (v if k == "$ref" else self.verbatim(v, base_dir, where)) for k, v in node.items()}

    def shareable(self, stripped):
        """Whether a union's alternatives are all references, scalars or @id / IRI references (no
        inline objects), so one union element can serve every block that uses it."""
        for b in stripped:
            b = {k: v for k, v in b.items() if k not in EXTRA_KEYWORDS}
            if b.get("type") == "array":
                b = b.get("items", {})
            ok = (is_iri_reference(b) or id_reference(b)[0] or (set(b) == {"$ref"} and b["$ref"].startswith("bb:"))
                  or (set(b) <= {"type", "format"} and b.get("type") in ("string", "integer", "boolean", "number")))
            if not ok:
                return False
        return True

    def union_ref(self, branches, keyword, owner, base_dir, where, role=None):
        """A union property's type: the union DataType for this content, created on its first use
        (or taken from the shared unions file), plus overrides of the per-use annotations that
        differ from the defining occurrence's ({alternative name: {key: value or None}})."""
        split = [split_alternative(b) for b in branches]
        stripped = self.canonical([s for s, _ in split], base_dir)
        notes = [n for _, n in split]
        key = json.dumps([keyword, stripped], sort_keys=True)
        self.union_occurrences.append((key, branches, keyword, base_dir, self.defs, self.shareable(stripped)))
        if key in self.union_plan:
            eid, base_notes, alts = self.union_plan[key]
            type_ref = ("href", f"{self.link.href_to_unions()}#{eid}")
        elif key in self.unions:
            type_ref, base_notes, alts = self.unions[key]
        else:
            outer_use, self.use = self.use, ((owner[0], role) if role else None)
            alts = self.alternative_names(branches)
            # anyOf (at least one) joins with Or, oneOf (exactly one) with Xor; a union of more
            # than three alternatives in one block is named by its first use, <Owner><Role>Choice
            name = ("Xor" if keyword == "oneOf" else "Or").join(a[:1].upper() + a[1:] for a in alts)
            if len(alts) > 3 and role:
                name = f"{owner[0]}{cap(local_role(role))}Choice"
            eid = self.union(branches, keyword, owner, "", base_dir, where, name=name)
            self.use = outer_use
            type_ref, base_notes = ("idref", eid), notes
            self.unions[key] = (type_ref, base_notes, alts)
        overrides = {}
        for alt, mine, base in zip(alts, notes, base_notes):
            diff = {k: mine.get(k) for k in sorted(set(mine) | set(base)) if mine.get(k) != base.get(k)}
            if diff:
                overrides[alt] = diff
        return type_ref, overrides

    def alternative_names(self, branches):
        names, used = [], {}
        for i, branch in enumerate(branches):
            alt = self.alternative_name(branch, i)
            used[alt] = used.get(alt, 0) + 1
            names.append(f"{alt}{used[alt]}" if used[alt] > 1 else alt)
        return names

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
            self.alt_use = self.use
            try:
                attrs.append(self.attribute(alt, branch, False, (name, owner[1]), base_dir, f"{where}|{i}"))
            finally:
                self.alt_use = None
        elem = {"kind": "uml:DataType", "pkg": owner[1], "name": name,
                "body": DEFINITION_HEADER + body + directive("union", keyword), "attrs": attrs}
        return self.add_unique(owner[1], name, elem)

    def enumeration(self, literals, owner, role, where):
        """A uml:Enumeration for an inline string enum, named after the property."""
        if not all(isinstance(v, str) for v in literals):
            raise Unmapped(f"{where}: non-string enum {literals}")
        elem = {"kind": "uml:Enumeration", "pkg": owner[1], "name": cap(local_role(role)),
                "body": DEFINITION_HEADER, "attrs": [], "literals": list(literals)}
        return self.add_unique(owner[1], elem["name"], elem)

    def element(self, schema, name, base_dir, where, fallback_name=None, fallback_pkg=None, extends=None,
                extends_kind=None):
        """uml:Class (schema declares @id) or uml:DataType for an object schema; returns its xmi id.
        A schema that is only a union (anyOf / oneOf of alternatives) becomes a union DataType."""
        for keyword in ("anyOf", "oneOf"):
            if keyword in schema and "properties" not in schema \
                    and not all(set(b) == {"required"} for b in schema[keyword]):
                extra = set(schema) - {"$schema", "title", "description", "type", keyword}
                if extra or schema.get("type", "object") != "object":
                    raise Unmapped(f"{where}: union with keywords {sorted(extra)} / type {schema.get('type')}")
                pkg = next((rdf_prefix(b) for b in schema[keyword] if isinstance(b, dict)
                            and rdf_prefix(b)), None) or self.default_pkg
                body = schema.get("description", "")
                if "title" in schema:
                    body += text_directive("title", schema["title"])
                if "type" in schema:
                    body += directive("objectUnion")  # the union also declares type: object
                return self.union(schema[keyword], keyword, (name, pkg), "", base_dir, where,
                                  name=name, body=body)
        if name is not None and set(schema) <= {"$schema", "title", "description", "allOf"} \
                and len(schema.get("allOf", [])) == 1 and set(schema["allOf"][0]) == {"$ref"} \
                and schema["allOf"][0]["$ref"].startswith("#/$defs/"):
            # root alias: the block's root is one of its $defs (skosConcept: allOf [$ref Concept])
            key = schema["allOf"][0]["$ref"][len("#/$defs/"):]
            eid = self.build_def(key, base_dir, where)
            if not eid:
                raise Unmapped(f"{where}: root alias of {key}, which is not an object, union or enum")
            body = directive("rootAlias")
            if "title" in schema:
                body += text_directive("title", schema["title"])
            if "description" in schema:
                body += text_directive("rootDescription", schema["description"])
            self.elements[eid]["body"] += body
            return eid
        members = schema.get("allOf", [])
        refs = [m for m in members if isinstance(m, dict) and set(m) == {"$ref"}
                and not m["$ref"].startswith("#") and "#" not in m["$ref"]]
        if (name is not None or fallback_name) and extends is None and "properties" not in schema \
                and set(schema) <= {"$schema", "title", "description", "type", "allOf", "$defs"} \
                and refs and len(refs) + sum(isinstance(m, dict) for m in members) - len(refs) == len(members):
            # a specialization of other blocks: allOf [$ref ../X/schema.yaml, ..., {own properties}]
            # (a composite profile, or cdifDataDescription's variable that is both a VariableMeasured
            # and an InstanceVariable). The object member with the most properties gives the
            # attributes; any other members are kept as constraints.
            rest = [m for m in members if m not in refs]
            objects = [m for m in rest if m.get("type") == "object" or "properties" in m]
            own = max(objects, key=lambda m: len(m.get("properties", {}))) if objects else {"type": "object"}
            constraints = [m for m in rest if m is not own]
            if {"title", "description"} & set(own) & set(schema):
                raise Unmapped(f"{where}: extension object with its own title / description")
            parents = [self.bblock_ref(m["$ref"], base_dir) for m in refs]
            kinds = [k for _, k in parents]
            merged = {**own, **{k: schema[k] for k in ("title", "description") if k in schema}}
            eid = self.element(merged, name, base_dir, where, fallback_name=fallback_name, fallback_pkg=fallback_pkg,
                               extends=[p for p, _ in parents],
                               extends_kind="uml:Class" if "uml:Class" in kinds else kinds[0])
            for c in constraints:
                self.elements[eid]["body"] += text_directive("constraint", self.verbatim(c, base_dir, where))
            if name is not None and "type" not in schema:
                self.elements[eid]["body"] += directive("untypedRoot")  # no type: object beside allOf
            if not objects:
                self.elements[eid]["body"] += directive("noOwnSchema")  # only the $refs (a composite)
            return eid
        props = schema.get("properties", {})
        # partial refinements of properties defined elsewhere: one verbatim constraint, not attributes
        partials = {k: v for k, v in props.items() if is_partial(v)}
        props = {k: v for k, v in props.items() if k not in partials}
        top_constraint = {k: schema[k] for k in CONSTRAINT_KEYWORDS if k in schema}
        closed = schema.get("additionalProperties") is False
        schema = {k: v for k, v in schema.items() if k not in top_constraint
                  and not (k == "additionalProperties" and closed)}
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
        # a specialization has its general's kind (a DataType can't specialize a Class)
        kind = extends_kind or classify(name, schema)
        group = self.plan.get((rdf_type, kind)) if rdf_type and extends is None else None
        if group and named_from_type:
            # one of several profiles of this RDF type: named by its block
            name = f"{self.block_name}{name}"
        pkg = rdf_prefix(schema) or fallback_pkg or self.default_pkg
        required = required_props(schema)
        groups = choice_groups(schema)
        for r in (r for g in groups for alt in g for r in alt):
            if r not in props:
                self.warnings.append(f"{where}: choice constraint names '{r}', which is not a declared property")

        body = DEFINITION_HEADER + schema.get("description", "")
        if "title" in schema:
            body += text_directive("title", schema["title"])
        if (not rdf_type or type_schema is not None) and pkg:
            # the prefix of this element's property keys (otherwise that of its @type)
            body += directive("prefix", pkg)
        if "@id" in props and kind == "uml:DataType":
            body += directive("hasId")  # a DataType (by classification) that declares @id
        if "@id" in required:
            body += directive("idRequired")
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
            if "@context" in required:
                body += directive("contextRequired")
        for unknown in set(props) & JSONLD_KEYWORDS - {"@type", "@id", "@context"}:
            raise Unmapped(f"{where}.{unknown}: JSON-LD keyword property not handled")
        for constraint in other_constraints(schema):
            body += text_directive("constraint", self.verbatim(constraint, base_dir, where))
        if partials:
            # written back as an allOf member {properties: ...}, which means the same
            body += text_directive("constraint", self.verbatim({"properties": partials}, base_dir, where))
        if top_constraint:
            # at the top level in the source; written back as an allOf member, which means the same
            body += text_directive("constraint", self.verbatim(top_constraint, base_dir, where))
        if closed:
            body += directive("closed")
        undeclared = [r for r in schema.get("required", []) if r not in props]
        if undeclared:
            # required keys with no property schema: no attribute to carry them
            body += text_directive("constraint", {"required": undeclared})
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
        if kind == "uml:Class" and "@id" not in props:
            body += directive("noId")  # a class whose schema declares no @id property
        if extends is not None:
            body += directive("extends")
        elem = {"kind": kind, "pkg": pkg, "name": name, "body": body, "attrs": attrs,
                "rdf_key": rdf_type if extends is None else None}
        if extends is not None:
            elem["general"] = [p[1] for p in extends]
        if group:
            # Attributes identical in every profile live on the shared base, inherited.
            elem["attrs"] = [a for a in attrs if attr_signature(a) not in group["common"]]
            elem["general"] = f"{self.link.href_to_shared()}#{group['base_id']}"
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
        if set(prop) == {"$ref"} and prop["$ref"].startswith("#/$defs/"):
            resolved, _ = self.resolve_local(prop, where)
            if isinstance(resolved, dict) and resolved.get("type") == "array":
                # a definition that is an array (cdifManifest's resourcePartArray): its own
                # description gives way to the property's, as when the schema is resolved
                prop = {k: v for k, v in resolved.items() if k != "description"}
        if prop.get("type") == "array":
            array_keywords = {k: prop.pop(k) for k in list(prop) if k in EXTRA_KEYWORDS | ARRAY_KEYWORDS}
            if array_keywords:
                directives.append(text_directive("arrayKeywords", self.verbatim(array_keywords, base_dir, where)))
            if set(prop) - {"type", "items", "minItems"}:
                raise Unmapped(f"{where}: array keywords {sorted(set(prop) - {'type', 'items', 'minItems'})}")
            if "minItems" in prop:
                # uml_to_schema.py derives minItems from the lower bound; element() keeps
                # this directive only where the two disagree.
                directives.append(text_directive("minItems", prop["minItems"]))
            elif required:
                directives.append(text_directive("minItems", None))
            prop, upper = dict(prop.get("items", {})), "*"  # no items: any item (only contains)
            items_desc = prop.pop("description", None)
            if "default" in prop:
                directives.append(text_directive("itemsDefault", prop.pop("default")))
        prop = dict(prop)
        keywords = {k: prop.pop(k) for k in list(prop) if k in EXTRA_KEYWORDS}
        if keywords:
            directives.append(text_directive("keywords", keywords))
        return self.resolve_value(prop, base_dir, where, owner, role, upper, directives, items_desc)

    def resolve_value(self, prop, base_dir, where, owner, role, upper, directives, items_desc):
        """A value schema to a type: local $defs followed, then map_value."""
        resolved, def_key = self.resolve_local(prop, where)
        described_union = isinstance(resolved, dict) and "description" in resolved             and set(resolved) - {"description"} in ({"anyOf"}, {"oneOf"})
        if def_key and (def_key in self.export_defs or def_key in self.def_stack or def_key in self.def_elements
                        or described_union or def_key in self.recursive_keys()):
            # (a union definition with its own description: the element named after it keeps it)
            # a definition other blocks also reference, or one that refers back to itself: the
            # one element named after it
            eid = self.build_def(def_key, base_dir, where)
            if eid:
                return self.typed(("idref", eid), self.def_kinds[def_key], upper, directives, items_desc)
        if def_key:
            self.def_stack.append(def_key)
        try:
            return self.map_value(resolved, base_dir, where, owner, role, upper, directives, items_desc)
        finally:
            if def_key:
                self.def_stack.pop()

    def map_value(self, prop, base_dir, where, owner, role, upper, directives, items_desc):
        """attr_type's second half: the value schema (array items, local $defs resolved) to a type."""
        bare = {k: v for k, v in prop.items() if k != "description"}
        if "description" in prop and (is_iri_reference(bare) or id_reference(bare)[0]
                                      or (set(bare) == {"$ref"} and not bare["$ref"].startswith("#"))):
            # a $defs entry with its own description (skosConcept ConceptRef): kept on the value
            directives.append(text_directive("valueDescription", prop["description"]))
            prop = {k: v for k, v in prop.items() if k != "description"}
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
        if set(prop) == {"allOf"} and len(prop["allOf"]) >= 2 and set(prop["allOf"][0]) == {"$ref"} \
                and all(not {"type", "properties", "$ref"} & set(m) for m in prop["allOf"][1:]):
            # X and further constraints on it (cdifCodelist: a narrower concept that must have broader)
            directives.append(text_directive("valueAllOf", self.verbatim(prop["allOf"][1:], base_dir, where)))
            return self.resolve_value(prop["allOf"][0], base_dir, where, owner, role, upper, directives, items_desc)
        if set(prop) == {"$ref"} and "#" in prop["$ref"] and not prop["$ref"].startswith("#"):
            type_ref, kind, schema_ref = self.fragment_ref(prop["$ref"], base_dir, where)
            directives.append(text_directive("schemaRef", schema_ref))
            return self.typed(type_ref, kind, upper, directives, items_desc)
        if set(prop) == {"anyOf"}:
            mapped = self.value_or_reference(prop["anyOf"], base_dir, where, owner, role, upper,
                                             directives, items_desc)
            if mapped:
                return mapped
        for keyword in ("anyOf", "oneOf"):
            if set(prop) == {keyword}:
                type_ref, overrides = self.union_ref(prop[keyword], keyword, owner, base_dir, where, role)
                if overrides:
                    directives.append(text_directive("alternativeOverrides", overrides))
                return type_ref, upper, directives, None, items_desc
        if prop == {}:
            # any JSON value: an attribute with no type
            return (None, None), upper, directives, None, items_desc
        if prop and (is_conditional(prop) or is_refinement(prop)):
            # a conditional value (cdifProvActivity's prov:used items, cdifDataDescription's
            # distribution items): untyped, schema verbatim
            directives.append(text_directive("valueSchema", self.verbatim(prop, base_dir, where)))
            return (None, None), upper, directives, None, items_desc
        if isinstance(prop.get("type"), list):
            types = prop["type"]
            if set(prop) != {"type"} or not set(types) <= {"string", "integer", "boolean", "number"}:
                raise Unmapped(f"{where}: {json.dumps(prop)[:160]}")
            # several scalar types: String, with the exact list carried in :keywords:
            directives.append(text_directive("keywords", {"type": types}))
            return ("prim", "String"), upper, directives, None, items_desc
        if prop.get("type") == "string" and set(prop) == {"type", "enum"}:
            return ("idref", self.enumeration(prop["enum"], owner, role, where)), upper, directives, None, items_desc
        if set(prop) == {"$ref"} and not prop["$ref"].startswith("#"):
            type_ref, kind = self.bblock_ref(prop["$ref"], base_dir)
        elif prop.get("type") == "object" or (set(prop) == {"allOf"} and any(
                isinstance(m, dict) and set(m) == {"$ref"} and "#" not in m["$ref"] for m in prop["allOf"])):
            wrapper = role_key(prop)
            use, self.alt_use = self.alt_use, None
            if wrapper:
                fallback = wrapper.split(":")[-1][:1].upper() + wrapper.split(":")[-1][1:] + "Role"
            elif use:
                # a union alternative: named after the property using the union, not the union
                fallback = f"{use[0]}{cap(local_role(use[1]))}" + ("Object" if prop == {"type": "object"} else "")
            else:
                fallback = f"{owner[0]}{cap(local_role(role))}" if owner else None
            eid = self.element(prop, None, base_dir, where, fallback_name=fallback,
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
        return self.typed(type_ref, kind, upper, directives, items_desc)

    def value_or_reference(self, branches, base_dir, where, owner, role, upper, directives, items_desc):
        """anyOf alternatives that are a value or a reference to one: a plain string and/or an
        {"@id"} node reference beside the value's own alternatives. The attribute is typed by the
        value (an association, for a Class; a union of the values, if several), and
        :orReference: lists the alternatives in order ("value" where the value goes). With no
        value, [string, {"@id"}] is common.IriReference (:referenceFirst: when {"@id"} comes
        first). The {"@id"}'s description goes in :idRefDescription:. None if not this pattern."""
        tokens, values, id_desc = [], [], None
        for b in branches:
            bare = {k: v for k, v in b.items() if k != "description"}
            # a description on the alternative itself: {"string": {"description": ...}} in the list
            token = ("string" if bare == {"type": "string"} else "idReference" if id_reference(bare)[0]
                     else "objectReference" if self.is_object_reference(bare, base_dir) else None)
            if token:
                tokens.append({token: {"description": b["description"]}} if "description" in b else token)
                if token == "idReference":
                    id_desc = id_reference(bare)[1]
            else:
                values.append(b)
                if "value" not in tokens:
                    tokens.append("value")
        kinds = [t if isinstance(t, str) else next(iter(t)) for t in tokens]
        has_reference = bool({"idReference", "objectReference"} & set(kinds))
        if not values and "objectReference" in tokens:  # (one with its own description stays a token)
            # objectReference and nothing but scalars beside it: it is the value
            i = kinds.index("objectReference")
            values, tokens[i], kinds[i] = [branches[i]], "value", "value"
        if not has_reference or any(kinds.count(k) > 1 for k in ("string", "idReference", "objectReference")) \
                or any(v.get("type") == "array" for v in values):
            return None  # (an array alternative needs a union: the attribute's bound is fixed)
        if id_desc is not None:
            directives.append(text_directive("idRefDescription", id_desc))
        if not values and tokens == kinds and sorted(kinds) == ["idReference", "string"]:  # a bare string and {"@id"}: IriReference
            if tokens[0] == "idReference":
                directives.append(directive("referenceFirst"))
            return self.support("common.IriReference"), upper, directives, None, items_desc
        directives.append(text_directive("orReference", tokens))
        if not values:  # string and {"@id"} with their own descriptions
            return (None, None), upper, directives, None, items_desc
        value = values[0] if len(values) == 1 else {"anyOf": values}
        return self.resolve_value(value, base_dir, where, owner, role, upper, directives, items_desc)

    def is_object_reference(self, b, base_dir):
        """Whether b is a $ref (perhaps through a local alias) to the cdifDataType/objectReference
        block, which is the {"@id"} node reference as a building block."""
        ref = b.get("$ref", "") if set(b) == {"$ref"} else ""
        while ref.startswith("#/$defs/") and set(self.defs.get(ref[len("#/$defs/"):], {})) == {"$ref"}:
            ref = self.defs[ref[len("#/$defs/"):]]["$ref"]
        if not ref or ref.startswith("#") or "#" in ref:
            return False
        return (base_dir / ref).resolve().as_posix().endswith(OBJECT_REFERENCE_BLOCK + "/schema.yaml")

    def typed(self, type_ref, kind, upper, directives, items_desc):
        """attr_type's result for a type of the given kind; a Class makes it an association end."""
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
        abstract = ' isAbstract="true"' if elem.get("abstract") else ""
        self.add(depth, f'<packagedElement xmi:type="{elem["kind"]}" xmi:id="{eid}" xmi:uuid="{self.uid(eid)}"{abstract}>')
        self.add(depth + 1, f"<name>{elem['name']}</name>")
        self.comment(depth + 1, eid, elem["body"])
        generals = elem.get("general") or []
        for i, general in enumerate([generals] if isinstance(generals, str) else generals):
            gid = f"{eid}.generalization" + (str(i + 1) if i else "")
            self.add(depth + 1, f'<generalization xmi:type="uml:Generalization" xmi:id="{gid}" xmi:uuid="{self.uid(gid)}">')
            self.add(depth + 2, f'<general href="{escape(general)}"/>')
            self.add(depth + 1, "</generalization>")
        for a in elem["attrs"]:
            aid = f"{eid}.{a['name']}"
            self.add(depth + 1, f'<ownedAttribute xmi:type="uml:Property" xmi:id="{aid}" xmi:uuid="{self.uid(aid)}">')
            self.add(depth + 2, f"<name>{a['name']}</name>")
            kind, ref = a["type"]
            if kind is None:
                pass  # untyped: any value
            elif kind == "prim":
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
            # a literal with whitespace or control characters (csvw:lineTerminators "\r\n") gets
            # its JSON escape in the id and character references in the name, which XML keeps
            lid = f"{eid}.{lit}" if lit.isprintable() and lit.strip() == lit else f"{eid}.{json.dumps(lit)[1:-1]}"
            self.add(depth + 1, f'<ownedLiteral xmi:type="uml:EnumerationLiteral" xmi:id="{escape(lid)}" '
                                f'xmi:uuid="{self.uid(lid)}">')
            self.add(depth + 2, f"<name>{escape(lit, CHAR_REFS)}</name>")
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
    mb = ModelBuilder(schema.get("$defs", {}), default_pkg=rdf_prefix(schema) or split_dir_name(bdir)[1]
                      or split_dir_name(bdir.parent)[1])
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
    pkgs = sorted({e["pkg"] for e in mb.elements.values()}, key=lambda p: (p not in support, str(p)))
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


def build_linked(bblock_dir, out_root, plan=None, union_plan=None, export_defs=None):
    """Linked mode: return (XMI file path, class name, class package, XMI text, warnings,
    model builder, link).

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
    class_name = root_name(bdir, schema)
    default_pkg = rdf_prefix(schema) or split_dir_name(bdir)[1] or split_dir_name(bdir.parent)[1]
    mb = ModelBuilder(schema.get("$defs", {}), link, plan=plan, block_name=class_name, union_plan=union_plan,
                      export_defs=export_defs, default_pkg=default_pkg)
    root_id = mb.element({k: v for k, v in schema.items() if k != "$defs"}, class_name, bdir, bdir.name)
    for key in sorted(export_defs or ()):
        if key in mb.defs:
            mb.build_def(key, bdir, bdir.name)
    class_name = mb.elements[root_id]["name"]

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
    return (link.file, class_name, mb.elements[root_id]["pkg"], "\n".join(out.lines) + "\n",
            mb.warnings, mb, link)


def shared_bases(builds, out_root):
    """From a first pass over the blocks ([(model builder, link)]), the RDF types that more than
    one element of the same kind defines. Returns (plan for the second pass, {base id: element}).

    Each such type gets an abstract base element in the shared file holding the attributes
    identical in all of them (type references re-pointed from the shared file's location)."""
    members = {}
    for mb, link in builds:
        for elem in mb.elements.values():
            key = elem.get("rdf_key")
            if key and " " not in key:  # one RDF type, not "A | B" / "A & B"
                members.setdefault((key, elem["kind"]), []).append((link, elem))
    plan, bases = {}, {}
    for (key, kind), ms in sorted(members.items()):
        if len(ms) < 2:
            continue
        by_sig = [{attr_signature(a): a for a in e["attrs"] if attr_signature(a)} for _, e in ms]
        common = set.intersection(*(set(s) for s in by_sig))
        name = key.split(":", 1)[1]
        base_id = SHARED_PREFIX + name
        if base_id in bases:
            base_id = SHARED_PREFIX + key.replace(":", "_")
        attrs = []
        for a in ms[0][1]["attrs"]:
            if attr_signature(a) in common:
                kind_, ref = a["type"]
                if kind_ == "href":  # relative to the member's file; re-point from the shared file
                    path, _, frag = ref.partition("#")
                    target = (ms[0][0].file.parent / path).resolve()
                    ref = os.path.relpath(target, Path(out_root).resolve()).replace(os.sep, "/") + "#" + frag
                attrs.append({**a, "type": (kind_, ref)})
        places = ", ".join(sorted({link.bb_path for link, _ in ms}))
        bases[base_id] = {"kind": kind, "pkg": key.split(":")[0], "name": name, "abstract": True,
                          "attrs": attrs,
                          "body": DEFINITION_HEADER + f"Abstract base of the {len(ms)} {key} profiles in "
                                  f"{places}. Each specializes it; this holds the attributes identical "
                                  f"in all of them." + directive("rdfType", key)}
        plan[(key, kind)] = {"base_id": base_id, "common": common}
    return plan, bases


def block_deps(bb_path, sources, _cache={}):
    """The building blocks (paths under _sources) a block's schema $refs, directly."""
    if bb_path not in _cache:
        deps = set()
        bdir = Path(sources) / bb_path
        text = (bdir / "schema.yaml").read_text(encoding="utf-8") if (bdir / "schema.yaml").exists() else ""
        for ref in re.findall(r"\$ref['\"]?\s*:\s*['\"]?([^'\"#\s]+)", text):
            if not ref.startswith("#") and ref.endswith("schema.yaml"):
                target = (bdir / ref).resolve().parent
                try:
                    deps.add(target.relative_to(Path(sources).resolve()).as_posix())
                except ValueError:
                    pass
        _cache[bb_path] = deps - {bb_path}
    return _cache[bb_path]


def reachable(bb_paths, sources):
    """The blocks bb_paths reference, directly or through other blocks, and themselves."""
    seen, stack = set(), list(bb_paths)
    while stack:
        b = stack.pop()
        if b not in seen:
            seen.add(b)
            stack.extend(block_deps(b, sources))
    return seen


def shared_unions(builds, out_root, sources=None):
    """Unions whose content more than one block uses, and whose alternatives are all references
    or scalars: built once in the shared unions file. Returns (union plan, XMI text or None)."""
    blocks_by_key, first = {}, {}
    for mb, link in builds:
        for key, branches, keyword, base_dir, defs, shareable in mb.union_occurrences:
            if shareable:
                blocks_by_key.setdefault(key, set()).add(link.bb_path)
                first.setdefault(key, (branches, keyword, base_dir, defs))
    keys = [k for k in sorted(blocks_by_key) if len(blocks_by_key[k]) > 1]
    # The shared file must be imported after the blocks its unions reference and before the
    # blocks that use them: a union referencing a block that uses a shared union stays local.
    while True:
        users = set().union(*(blocks_by_key[k] for k in keys))
        # (through the blocks they reference in turn: an import cycle all the same)
        local = [k for k in keys if (reachable(set(re.findall(r'"bb:([^"#]+)', k)), sources) if sources
                                     else set(re.findall(r'"bb:([^"#]+)', k))) & users]
        if not local:
            break
        keys = [k for k in keys if k not in local]
    if not keys:
        return {}, None
    link = SharedLink(SHARED_UNIONS_FILE, UNION_NS, Path(out_root).resolve())
    sb = ModelBuilder({}, link)
    plan = {}
    for key in keys:
        branches, keyword, base_dir, defs = first[key]
        sb.defs = defs
        (_, eid), _ = sb.union_ref(branches, keyword, ("Shared", None), base_dir, "shared union")
        plan[key] = (eid, sb.unions[key][1], sb.unions[key][2])
    out = Writer(common_uid)
    out.lines += XMI_HEADER
    out.add(1, f'<uml:Model xmi:id="{UNION_NS}.model" xmi:uuid="{common_uid(UNION_NS + ".model")}">')
    out.add(2, "<name>cdifSharedUnions</name>")
    out.add(2, f'<packagedElement xmi:type="uml:Package" xmi:id="{UNION_NS}" xmi:uuid="{common_uid(UNION_NS)}">')
    out.add(3, "<name>cdifSharedUnions</name>")
    out.add(3, "<URI>https://w3id.org/cdif/union/xmi/</URI>")
    for eid in sorted(sb.elements):
        out.element(3, eid, sb.elements[eid])
    for eid in sorted(sb.elements):
        out.associations(3, eid, sb.elements[eid])
    out.add(2, "</packagedElement>")
    out.add(1, "</uml:Model>")
    out.add(0, "</xmi:XMI>")
    return plan, "\n".join(out.lines) + "\n"


def shared_types_xmi(bases):
    out = Writer(common_uid)
    out.lines += XMI_HEADER
    out.add(1, f'<uml:Model xmi:id="cdif.sharedTypes" xmi:uuid="{common_uid("cdif.sharedTypes")}">')
    out.add(2, "<name>cdifSharedTypes</name>")
    out.add(2, f'<packagedElement xmi:type="uml:Package" xmi:id="cdif.shared" xmi:uuid="{common_uid("cdif.shared")}">')
    out.add(3, "<name>cdifSharedTypes</name>")
    out.add(3, "<URI>https://w3id.org/cdif/shared/xmi/</URI>")
    for eid, elem in sorted(bases.items()):
        out.element(3, eid, elem)
    out.add(2, "</packagedElement>")
    out.add(1, "</uml:Model>")
    out.add(0, "</xmi:XMI>")
    return "\n".join(out.lines) + "\n"


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
    """Write the common and shared types files and one XMI file per building block; return
    {bblock dir: (path, class name, package, warnings)}.

    Two passes: the first finds RDF types several elements define (shared_bases); the second
    builds each block with those elements specializing a shared abstract base."""
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / COMMON_TYPES_FILE).write_text(common_types_xmi(), encoding="utf-8")
    first = [build_linked(d, out_root)[5:] for d in bblock_dirs]
    exports = {}
    for mb, _ in first:
        for bb_path, key in mb.exports_needed:
            exports.setdefault(bb_path, set()).add(key)
    plan, bases = shared_bases(first, out_root)
    sources = next(p for p in Path(bblock_dirs[0]).resolve().parents if p.name == "_sources")
    union_plan, unions_xml = shared_unions(first, out_root, sources)
    for name, text in ((SHARED_TYPES_FILE, bases and shared_types_xmi(bases)), (SHARED_UNIONS_FILE, unions_xml)):
        if text:
            (out_root / name).write_text(text, encoding="utf-8")
        elif (out_root / name).exists():
            (out_root / name).unlink()
    results = {}
    for d in bblock_dirs:
        bb_path = Path(d).resolve().relative_to(
            next(p for p in Path(d).resolve().parents if p.name == "_sources")).as_posix()
        path, class_name, pkg, xml, warnings, _, _ = build_linked(d, out_root, plan, union_plan,
                                                                   exports.get(bb_path))
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
