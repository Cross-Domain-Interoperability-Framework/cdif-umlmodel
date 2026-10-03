#!/usr/bin/env python3
"""
Generate a CDIF building-block schema.yaml from a canonical UML 2.5 / XMI 2.5.1
export of a DDI-CDI / UCMIS-style class model.

Inputs:
  --xmi PATH                    canonical XMI file (UML 2.5.1 / XMI 2.5.1)
  --class A[,B,...]             one or more classes to use as BB roots
  --bb-name DDICDIThing         building block directory name
  --out-dir DIR                 parent directory for the new BB folder

Conventions encoded (see uml_to_schema.md for details):
  - JSON-LD root: anyOf [single node, array of nodes, {@context, @graph}].
  - @type required, validated via array `contains` const "cdi:<ClassName>".
  - @id is always optional on every node.
  - UML attributes typed by uml:DataType (UCMIS dt-*) expand inline as $defs,
    OR are emitted as $ref to a sibling shared-types BB if the type name is
    found there (default: ../ddicdiDataTypes/schema.yaml).
  - UML attributes typed by uml:Class become {$ref: "#/$defs/id-reference"},
    unless the target class is one of --inline=ClassA,ClassB or the user
    explicitly passes --reference for a class that would otherwise be inlined.
  - UML attributes typed by uml:Enumeration emit `enum: [literal, ...]`.
  - Multiplicity:
      lower>=1, upper==1 → required + single value.
      lower==0, upper==1 → optional + single value.
      upper=='*'         → array-only (`type: array, items: {...}`,
                                       minItems if lower>=1).
  - Generalizations are walked: inherited attributes are merged into the
    child node (child overrides parent on name collision).
  - The Node $def name is the class's UML name. Docs are taken from the
    "Definition" section of the class's ownedComment (cleaned).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import textwrap
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from xml.etree import ElementTree as ET

import yaml

NS = {
    "uml": "http://www.omg.org/spec/UML/20131001",
    "xmi": "http://www.omg.org/spec/XMI/20131001",
}
XMI_ID = f"{{{NS['xmi']}}}id"
XMI_IDREF = f"{{{NS['xmi']}}}idref"
XMI_TYPE = f"{{{NS['xmi']}}}type"
XMI_HREF = "href"

# Building-block sources this tool reads (sibling-BB discovery, shared-datatype
# fallback, :buildingBlock: refs) and writes beside. Default assumes the
# metadataBuildingBlocks repo is a sibling of cdif-umlmodel; override with --sources-dir.
DEFAULT_SOURCES_DIR = Path(__file__).resolve().parents[2] / "metadataBuildingBlocks" / "_sources"

# ---------------------------------------------------------------------------
# Model classes
# ---------------------------------------------------------------------------

@dataclass
class Property:
    id: str
    name: str
    type_id: Optional[str]      # idref to a Class/DataType/Enum in this XMI
    primitive: Optional[str]    # 'String', 'Integer', 'Boolean', 'Real', etc.
    lower: int                  # 0 or positive; default 1 (UML default)
    upper: int                  # -1 sentinel for *
    doc: Optional[str]
    is_assoc_end: bool          # True if <association> ref present
    aggregation: Optional[str]  # 'composite', 'shared', or None
    type_href: Optional[str] = None  # raw <type href>, resolved across files by --linked


@dataclass
class UmlClass:
    id: str
    name: str
    package: str
    doc: Optional[str]
    is_abstract: bool
    parents: list[str]          # generalization superClass ids
    properties: list[Property]
    kind: str = "class"         # 'class' | 'datatype' | 'enumeration'
    literals: list[str] = field(default_factory=list)
    # v1.1: render this config class mapping as <packagedElement uml:DataType>
    # instead of uml:Class (value types with no identity, e.g. Identifier,
    # Reference). Kept separate from `kind` so closure/association walking still
    # treats it as a class (it is referenced as an attribute type, not an
    # association end).
    is_datatype: bool = False


@dataclass
class Model:
    elements: dict[str, UmlClass]      # id -> element
    name_to_id: dict[str, list[str]]   # name -> [ids] (multiple if dupes)


# ---------------------------------------------------------------------------
# XMI parsing
# ---------------------------------------------------------------------------

PRIMITIVE_FRAGMENTS = {
    "String": "string",
    "Integer": "integer",
    "Boolean": "boolean",
    "Real": "number",
    "UnlimitedNatural": "integer",
}


def _text(elem: ET.Element, child: str) -> Optional[str]:
    e = elem.find(child)
    if e is not None and e.text is not None:
        return e.text.strip()
    return None


def _local_doc(elem: ET.Element) -> Optional[str]:
    comment = elem.find("ownedComment")
    if comment is None:
        return None
    body = comment.find("body")
    if body is None or body.text is None:
        return None
    return body.text


def _parse_int_value(elem: Optional[ET.Element], default: int) -> int:
    if elem is None:
        return default
    v = elem.find("value")
    raw = v.text.strip() if (v is not None and v.text) else ""
    if raw == "*":
        return -1
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_property(elem: ET.Element, owner_name: str = "") -> Property:
    pid = elem.get(XMI_ID, "")
    name = _text(elem, "name") or ""
    type_id = None
    primitive = None
    href = None
    type_el = elem.find("type")
    if type_el is not None:
        if XMI_IDREF in type_el.attrib:
            type_id = type_el.get(XMI_IDREF)
        elif XMI_HREF in type_el.attrib:
            href = type_el.get(XMI_HREF, "")
            frag = href.split("#", 1)[-1]
            primitive = PRIMITIVE_FRAGMENTS.get(frag, None)
            if primitive is None:
                primitive = "string"  # unknown primitive → string fallback
    lower = _parse_int_value(elem.find("lowerValue"), 1)
    upper = _parse_int_value(elem.find("upperValue"), 1)
    doc = _local_doc(elem)
    assoc_el = elem.find("association")
    is_assoc = assoc_el is not None
    aggregation = elem.get("aggregation")

    # Recover a role name when the navigable end has no <name>: the assoc id
    # ends with `<Owner>_<role>_<Target>` (where role may contain underscores).
    # The id may be prefixed with a path using dots and/or dashes — match the
    # owner_role_target tail anchored at end-of-string.
    if not name and is_assoc and owner_name:
        assoc_id = assoc_el.get(XMI_IDREF, "") if assoc_el is not None else ""
        m = re.search(rf"(?:^|[.\-_]){re.escape(owner_name)}_(.+)_([A-Za-z][\w]*)$", assoc_id)
        if m:
            name = m.group(1)

    return Property(
        id=pid, name=name, type_id=type_id, primitive=primitive,
        lower=lower, upper=upper, doc=doc,
        is_assoc_end=is_assoc, aggregation=aggregation, type_href=href,
    )


def _parse_class(elem: ET.Element, package: str, kind: str) -> UmlClass:
    cid = elem.get(XMI_ID, "")
    name = _text(elem, "name") or cid
    is_abstract = elem.get("isAbstract", "false") == "true"
    doc = _local_doc(elem)
    parents = []
    for gen in elem.findall("generalization"):
        # `general` may be either an attribute on <generalization> or a child
        # element <general xmi:idref="..."/>; canonical XMI 2.5.1 uses the
        # child form.
        sup = gen.get("general")
        if not sup:
            child = gen.find("general")
            if child is not None:
                # an href ("other.xmi#id") is resolved across files by --linked
                sup = child.get(XMI_IDREF) or child.get(XMI_HREF)
        if sup:
            parents.append(sup)
    props: list[Property] = []
    owner_name = _text(elem, "name") or ""
    for attr in elem.findall("ownedAttribute"):
        props.append(_parse_property(attr, owner_name=owner_name))
    literals: list[str] = []
    if kind == "enumeration":
        for lit in elem.findall("ownedLiteral"):
            n = _text(lit, "name")
            if n == "":
                n = lit.find("name").text  # a whitespace literal (e.g. "\r\n"), kept as is
            n = n or lit.get(XMI_ID, "")
            literals.append(n)
    return UmlClass(
        id=cid, name=name, package=package, doc=doc,
        is_abstract=is_abstract, parents=parents, properties=props,
        kind=kind, literals=literals,
    )


def _walk_packages(root: ET.Element, model: Model, current_path: list[str]):
    for child in list(root):
        ctype = child.get(XMI_TYPE, "")
        if ctype == "uml:Package":
            pname = _text(child, "name") or child.get(XMI_ID, "")
            _walk_packages(child, model, current_path + [pname])
            continue
        kind = None
        if ctype == "uml:Class":
            kind = "class"
        elif ctype == "uml:DataType":
            kind = "datatype"
        elif ctype == "uml:Enumeration":
            kind = "enumeration"
        elif ctype == "uml:PrimitiveType":
            # Capture model-local PrimitiveTypes (e.g. DDI-CDI's XsdAnyUri,
            # XsdDate, XsdLanguage) so they can be referenced by name in
            # ucmism2m config dataType fields.
            kind = "datatype"
        if kind:
            cls = _parse_class(child, ".".join(current_path), kind)
            if cls.id in model.elements:
                # Duplicate id (shouldn't happen in canonical XMI); keep first.
                continue
            model.elements[cls.id] = cls
            model.name_to_id.setdefault(cls.name, []).append(cls.id)


def _parse_canonical_xmi(root: ET.Element) -> Model:
    """Parse a canonical XMI 2.5.1 export (OMG namespaces, uml:Model)."""
    # XMI root is <xmi:XMI>; the model lives in <uml:Model> inside.
    uml_model = root.find("uml:Model", NS)
    if uml_model is None:
        # Some exports omit the namespace prefix on Model.
        for child in root:
            if child.tag.endswith("Model"):
                uml_model = child
                break
    if uml_model is None:
        raise SystemExit("Could not find <uml:Model> in XMI")
    model = Model(elements={}, name_to_id={})
    _walk_packages(uml_model, model, [_text(uml_model, "name") or "Model"])
    return model


# ---------------------------------------------------------------------------
# Enterprise Architect XMI 1.1 parsing
#
# EA's native export (xmi.version="1.1", xmlns:UML="omg.org/UML1.3") differs
# structurally from canonical XMI 2.5.1:
#   - classes/datatypes/enums are all <UML:Class>, distinguished by the
#     `ea_stype` tagged value and an `enumeration` stereotype;
#   - attributes carry their type as a `type` tagged value plus a
#     <UML:StructuralFeature.type><UML:Classifier xmi.idref="..."/> ;
#   - generalizations are top-level <UML:Generalization subtype=.. supertype=..>;
#   - associations are top-level <UML:Association> with two <UML:AssociationEnd>
#     children — navigable ends are synthesised here into assoc-end Properties
#     on the owning class, mirroring the canonical ownedAttribute form.
# The output Model/UmlClass/Property structures are identical to the canonical
# parser's, so everything downstream is format-agnostic.
# ---------------------------------------------------------------------------

def _ea_local(elem: ET.Element) -> str:
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def _ea_tagged_values(elem: ET.Element) -> dict:
    out: dict[str, str] = {}
    for child in elem:
        if _ea_local(child) == "ModelElement.taggedValue":
            for tv in child:
                if _ea_local(tv) == "TaggedValue":
                    out[tv.get("tag")] = tv.get("value")
    return out


def _ea_is_enumeration(elem: ET.Element, tv: dict) -> bool:
    if tv.get("stereotype") == "enumeration":
        return True
    for child in elem:
        if _ea_local(child) == "ModelElement.stereotype":
            for st in child:
                if _ea_local(st) == "Stereotype" and st.get("name") == "enumeration":
                    return True
    return False


def _parse_ea_multiplicity(s: Optional[str], def_lower: int = 1,
                           def_upper: int = 1) -> tuple[int, int]:
    s = (s or "").strip()
    if not s:
        return def_lower, def_upper
    if s == "*":
        return 0, -1
    if ".." in s:
        lo, hi = (p.strip() for p in s.split("..", 1))
        lower = int(lo) if lo.lstrip("-").isdigit() else 0
        upper = -1 if hi == "*" else (int(hi) if hi.lstrip("-").isdigit() else 1)
        return lower, upper
    if s.lstrip("-").isdigit():
        n = int(s)
        return n, n
    return def_lower, def_upper


def _parse_ea_attribute(attr: ET.Element, name_to_id: dict) -> Property:
    name = attr.get("name") or ""
    atv = _ea_tagged_values(attr)
    type_name = (atv.get("type") or "").strip()
    # idref on the StructuralFeature.type child points at the type element
    idref = None
    for child in attr:
        if _ea_local(child) == "StructuralFeature.type":
            for c in child:
                if _ea_local(c) == "Classifier":
                    idref = c.get("xmi.idref")
    primitive = None
    type_id = None
    if type_name in PRIMITIVE_FRAGMENTS:
        primitive = PRIMITIVE_FRAGMENTS[type_name]
    elif idref and idref in name_to_id.get("__ids__", set()):
        type_id = idref
    elif type_name and type_name in name_to_id:
        type_id = name_to_id[type_name][0]
    else:
        primitive = "string"  # unknown / unresolved → string fallback
    lower, upper = _parse_ea_multiplicity(atv.get("lowerBound"),
                                          def_lower=1, def_upper=1)
    # EA stores lower/upper separately; upperBound "*" → -1
    if (atv.get("upperBound") or "").strip() == "*":
        upper = -1
    elif (atv.get("upperBound") or "").strip().lstrip("-").isdigit():
        upper = int(atv["upperBound"])
    if (atv.get("lowerBound") or "").strip().lstrip("-").isdigit():
        lower = int(atv["lowerBound"])
    return Property(
        id=atv.get("ea_guid", name), name=name, type_id=type_id,
        primitive=primitive, lower=lower, upper=upper,
        doc=atv.get("description"), is_assoc_end=False, aggregation=None,
    )


def parse_ea_xmi(root: ET.Element) -> Model:
    """Parse an Enterprise Architect native XMI 1.1 export into a Model."""
    model = Model(elements={}, name_to_id={})
    class_elems: dict[str, ET.Element] = {}

    # ---- pass 1: class/datatype/enum shells (no members yet) --------------
    for elem in root.iter():
        if _ea_local(elem) != "Class":
            continue
        cid = elem.get("xmi.id")
        name = elem.get("name")
        if not cid or not name:
            continue
        tv = _ea_tagged_values(elem)
        stype = tv.get("ea_stype", "")
        if stype not in ("Class", "DataType"):
            continue  # skip Note / Object / Text / etc.
        is_enum = _ea_is_enumeration(elem, tv)
        kind = "enumeration" if is_enum else ("datatype" if stype == "DataType" else "class")
        cls = UmlClass(
            id=cid, name=name, package=tv.get("package_name", ""),
            doc=tv.get("documentation"),
            is_abstract=elem.get("isAbstract", "false") == "true",
            parents=[], properties=[], kind=kind, literals=[],
        )
        if cid in model.elements:
            continue
        model.elements[cid] = cls
        model.name_to_id.setdefault(name, []).append(cid)
        class_elems[cid] = elem

    # index of all known element ids, for attribute type resolution
    model.name_to_id["__ids__"] = set(model.elements.keys())  # type: ignore

    # ---- pass 2: attributes (and enum literals) --------------------------
    for cid, elem in class_elems.items():
        cls = model.elements[cid]
        for feat in elem:
            if _ea_local(feat) != "Classifier.feature":
                continue
            for a in feat:
                if _ea_local(a) != "Attribute":
                    continue
                if cls.kind == "enumeration":
                    lit = a.get("name")
                    if lit:
                        cls.literals.append(lit)
                else:
                    cls.properties.append(_parse_ea_attribute(a, model.name_to_id))

    # ---- pass 3: generalizations -----------------------------------------
    for elem in root.iter():
        if _ea_local(elem) != "Generalization":
            continue
        sub = elem.get("subtype")
        sup = elem.get("supertype")
        if sub in model.elements and sup:
            model.elements[sub].parents.append(sup)

    # ---- pass 4: associations → synthesised assoc-end Properties ---------
    for elem in root.iter():
        if _ea_local(elem) != "Association":
            continue
        tv = _ea_tagged_values(elem)
        if tv.get("ea_type") not in (None, "Association", "Aggregation"):
            continue  # skip trace / dependency / abstraction connectors
        role = elem.get("name") or tv.get("mt") or ""
        ends = []
        for conn in elem:
            if _ea_local(conn) != "Association.connection":
                continue
            for ae in conn:
                if _ea_local(ae) != "AssociationEnd":
                    continue
                aetv = _ea_tagged_values(ae)
                ends.append({
                    "type": ae.get("type"),
                    "mult": ae.get("multiplicity", ""),
                    "navigable": ae.get("isNavigable") == "true",
                    "aggregation": ae.get("aggregation", "none"),
                    "end": aetv.get("ea_end", ""),
                })
        if len(ends) != 2:
            continue
        src = next((e for e in ends if e["end"] == "source"), ends[0])
        tgt = next((e for e in ends if e["end"] == "target"), ends[1])

        def _mk(owner_id, target_id, mult_str, fallback_mb, aggregation):
            if owner_id not in model.elements or not target_id:
                return
            lower, upper = _parse_ea_multiplicity(mult_str or fallback_mb,
                                                  def_lower=0, def_upper=-1)
            agg = aggregation if aggregation not in ("none", "", None) else None
            model.elements[owner_id].properties.append(Property(
                id=f"{owner_id}_{role}_{target_id}", name=role,
                type_id=target_id, primitive=None,
                lower=lower, upper=upper, doc=None,
                is_assoc_end=True, aggregation=agg,
            ))

        # navigable source→target : the SOURCE class gets the role property
        if tgt["navigable"]:
            _mk(src["type"], tgt["type"], tgt["mult"], tv.get("rb"),
                tgt["aggregation"])
        if src["navigable"]:
            _mk(tgt["type"], src["type"], src["mult"], tv.get("lb"),
                src["aggregation"])

    # drop the internal id index before returning
    model.name_to_id.pop("__ids__", None)  # type: ignore
    return model


def _detect_xmi_format(root: ET.Element) -> str:
    """Return 'ea' for Enterprise Architect native XMI 1.1, else 'canonical'."""
    ver = root.get("xmi.version", "")  # EA uses the un-namespaced attribute
    if ver.startswith("1."):
        return "ea"
    # EA declares xmlns:UML="omg.org/UML1.3"; canonical uses the OMG UML NS.
    for v in (root.tag, *(root.attrib.values())):
        if "omg.org/UML1.3" in (v or ""):
            return "ea"
    return "canonical"


def parse_xmi(path: Path) -> Model:
    """Parse either a canonical XMI 2.5.1 or an EA native XMI 1.1 export.

    The format is auto-detected from the XMI root; both parsers emit the same
    Model / UmlClass / Property structures so the rest of the generator is
    format-agnostic.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    fmt = _detect_xmi_format(root)
    if fmt == "ea":
        print("Detected Enterprise Architect XMI 1.1 export", file=sys.stderr)
        return parse_ea_xmi(root)
    print("Detected canonical XMI 2.5.1 export", file=sys.stderr)
    return _parse_canonical_xmi(root)


# Linked XMI (--linked): one canonical XMI file per building block, written by
# bblock_to_xmi.py --linked. Elements are identified <register id of the block>.<Name>,
# e.g. cdif.bbr.metadata.schemaorgProperties.person.Person, and reference other
# blocks' elements with <type href="../organization/organization.xmi#<id>"/>.
REGISTER_PREFIX = "cdif.bbr.metadata."


def element_bb_path(element_id: str) -> Optional[str]:
    """Building-block path under _sources/ for a linked element id, or None if not one."""
    if not element_id.startswith(REGISTER_PREFIX):
        return None
    return element_id[len(REGISTER_PREFIX):].rsplit(".", 1)[0].replace(".", "/")


def load_linked_xmi(path: Path) -> Model:
    """Parse a canonical XMI file and, transitively, every XMI file its <type href>s point
    to, into one Model. An href to a file that does not exist leaves a placeholder
    datatype, so references to building blocks without XMI still resolve to a $ref."""
    model = Model(elements={}, name_to_id={})
    queue, seen = [Path(path).resolve()], set()
    while queue:
        f = queue.pop(0)
        if f in seen:
            continue
        seen.add(f)
        if not f.exists():
            print(f"WARN: linked XMI file not found: {f}", file=sys.stderr)
            continue
        part = _parse_canonical_xmi(ET.parse(f).getroot())
        for el in part.elements.values():
            for p in el.properties:
                file_part, _, frag = (p.type_href or "").partition("#")
                if file_part and "://" not in file_part:
                    p.type_id, p.primitive = frag, None
                    queue.append((f.parent / file_part).resolve())
            for i, parent in enumerate(el.parents):
                file_part, sep, frag = parent.partition("#")
                if sep and file_part and "://" not in file_part:
                    el.parents[i] = frag
                    queue.append((f.parent / file_part).resolve())
        for eid, el in part.elements.items():
            if eid not in model.elements:
                model.elements[eid] = el
                model.name_to_id.setdefault(el.name, []).append(eid)
    for el in list(model.elements.values()):
        for p in el.properties:
            if p.type_id and p.type_id not in model.elements and element_bb_path(p.type_id):
                name = p.type_id.rsplit(".", 1)[1]
                model.elements[p.type_id] = UmlClass(
                    id=p.type_id, name=name, package="", doc=None, is_abstract=False,
                    parents=[], properties=[], kind="datatype")
    return model


# ---------------------------------------------------------------------------
# Doc cleaning
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"^(Definition|Examples?|Explanatory notes?)\s*\n=+\s*\n",
    re.MULTILINE,
)


def clean_definition(doc: Optional[str]) -> Optional[str]:
    """Extract the "Definition" section from a UCMIS-style ownedComment body.

    UCMIS comments are formatted as:
        Definition
        ==========
        <text>

        Examples
        ========
        ...

    Return only the Definition body (collapsed whitespace). If no recognizable
    section markers are present, return the whole body trimmed.

    RST-augmented bodies (starting with 'Documentation\\n===') are passed
    through unchanged — they were already composed as full documentation
    blocks by rst_augment.
    """
    if not doc:
        return None
    # Preserve RST-augmented bodies (composed by rst_augment.py).
    if doc.lstrip().startswith("Documentation\n===") or doc.lstrip().startswith("Documentation\r\n==="):
        return doc.rstrip() + "\n"
    # Normalize whitespace and split on section headers
    parts = _SECTION_RE.split(doc.strip())
    # _SECTION_RE.split returns: [pre, header1, body1, header2, body2, ...]
    if len(parts) >= 3:
        # Walk pairs (header, body) and pick "Definition"
        for i in range(1, len(parts) - 1, 2):
            if parts[i].lower().startswith("definition"):
                body = parts[i + 1].strip()
                return _normalize_whitespace(body)
        # No Definition header: take first non-empty body
        for i in range(2, len(parts), 2):
            if parts[i].strip():
                return _normalize_whitespace(parts[i].strip())
    return _normalize_whitespace(doc.strip())


def _normalize_whitespace(s: str) -> str:
    # Collapse runs of whitespace; preserve paragraph breaks
    paragraphs = re.split(r"\n\s*\n", s)
    cleaned = [re.sub(r"\s+", " ", p).strip() for p in paragraphs]
    return "\n\n".join(p for p in cleaned if p)


def verbatim_definition(doc: Optional[str]) -> Optional[str]:
    """Like clean_definition, but return the Definition section exactly as
    written (no whitespace collapsing or trimming). Used by --verbatim-docs."""
    if not doc:
        return None
    headers = list(_SECTION_RE.finditer(doc))
    for i, m in enumerate(headers):
        if m.group(1).lower().startswith("definition"):
            if i + 1 < len(headers):
                return doc[m.end():headers[i + 1].start()].rstrip("\n")
            return doc[m.end():]
    return doc


# Comment directives (--comment-directives): lines at the end of an ownedComment,
# after the definition text, carrying JSON-LD facts UML has no slot for.
# On a class / datatype:
#   :rdfType: ``schema:PropertyValue``      -> @type const (default prefix:ClassName);
#                                              on a datatype also makes @type required.
#             ``schema:A | schema:B``        -> @type contains anyOf [{const: A}, {const: B}]
#             ``schema:A & schema:B``        -> @type allOf [{contains: {const: A}}, ...], minItems 2
#   :typeDescription: "text"                 -> @type description
#   :idDescription: "text"                   -> @id description (with the directives, @id
#                                               is written as declared, never invented)
#   :title: "text"                           -> the schema title (root class only)
#   :allowedTypes: ``schema:A | schema:B``   -> @type items restricted to enum [A, B]
#   :typeDefault: ``schema:A | schema:B``    -> @type default, always emitted as an array [A, B]
#   :choiceConstraints:
#   - ``value | url & name``                 -> anyOf [{required: [value]}, {required: [url, name]}]
#   :buildingBlock: ``schemaorgProperties/identifier``
#                                            -> the type is defined by that BB (path under
#                                               _sources/); references become a $ref to it
# On an attribute:
#   :inlineOrByReference: ``inline``         -> class-typed value: embedded node only
#                         ``byReference``    -> {"@id"} reference only
#                                               (absent: either, the default anyOf)
#   :alsoAcceptsString:                      -> anyOf [<type>, {type: string}]
#                      ``first``             -> anyOf [{type: string}, <type>]
#   :default: <JSON value>                   -> the property's default
#   :descriptionOnItems:                     -> description goes on the array's items
#   :minItems: <n or null>                   -> array minItems, where the lower bound doesn't
#                                               imply it (null: none, though required)
#   :idRefDescription: "text"                -> description of @id in an --id-reference-type value
#   :jsonName: "dcterms:conformsTo"          -> the property key, where it isn't prefix:attribute
#   :keywords: {...} / :arrayKeywords: {...} -> extra keywords (title, minLength, pattern, ...)
#                                               on the value schema / on the array
#   :itemsDefault: <JSON value>              -> default on the array's items
#   :itemsDescription: "text"                -> description on the array's items, where the
#                                               array has its own
# On a class / datatype, also:
#   :typeOptional:                           -> @type is declared but not required
#   :noId:                                   -> a class whose schema declares no @id
#   :exportedDef:                            -> a $defs entry other blocks reference: kept in $defs
#   :rootAlias:, :rootDescription: "text"    -> the block's root is allOf [$ref to this $defs entry]
#   :extends:                                -> root is allOf [$ref <parent block>, own schema]; the
#                                               parent's attributes are not merged in
#   :closed:                                 -> additionalProperties: false
#   :hasId:                                  -> a datatype that declares @id
#   :idRequired:                             -> @id is required
#   :untypedRoot:                            -> with :extends:, the root has no type: object
#   :objectUnion:                            -> a :union: that also declares type: object
#   :extends: on a nested element            -> its $defs entry is allOf [$ref <parent block>, own schema]
# On an attribute:
#   :schemaRef: "<bb path>#/$defs/K"         -> the value is exactly that $ref into another block
#   :orReference: ["value", "string", "idReference"]
#                                            -> anyOf of the value and a plain string and/or an
#                                               {"@id"} reference (with :idRefDescription:), in order;
#                                               {"string": {"description": ...}} annotates one
#                                               (no "value" entry: the attribute has no type);
#                                               "objectReference": $ref to cdifDataType/objectReference
#   :referenceFirst:                         -> on an --iri-reference-type value: {"@id"} first
#   :valueSchema: {...}                      -> the value schema, verbatim (e.g. a bare if/then);
#                                               $refs "bb:<path under sources>" made relative
#   :valueDescription: "text"                -> description on the value schema itself
# On an attribute typed by a :union: datatype:
#   :alternativeOverrides: {"alt": {...}}    -> per-use annotations of the alternatives
#                                               (description, default, title, idRefDescription;
#                                               null removes), the union inlined with them
#   :prefix: ``schema``                      -> prefix of its property keys, where it has no
#                                               :rdfType: to take it from
#   :union: ``anyOf`` / ``oneOf``            -> a union (ISO 19103 style): the schema is that
#                                               keyword over its attributes, one per alternative
#   :contextSchema: {...}                    -> the @context property schema, verbatim
#   :typeSchema: {...}                       -> the @type property schema, verbatim (an @type
#                                               constraint in a form :rdfType: can't express)
#   :constraint: {...}                       -> an allOf member UML has no form for (e.g.
#                                               if/then/else), verbatim; may repeat
# Names in :choiceConstraints: are JSON property keys when they contain ':' or start with '@'.
# Values in ``...`` are names or codes; other values (text, defaults) are JSON.
_DIRECTIVE_NAMES = ("rdfType", "allowedTypes", "typeDefault", "typeDescription", "idDescription",
                    "title", "choiceConstraints", "buildingBlock", "inlineOrByReference",
                    "alsoAcceptsString", "default", "descriptionOnItems", "minItems",
                    "idRefDescription", "jsonName", "keywords", "arrayKeywords", "typeOptional",
                    "itemsDefault", "prefix", "union", "contextSchema", "constraint", "typeSchema",
                    "itemsDescription", "noId", "alternativeOverrides", "schemaRef", "exportedDef",
                    "rootAlias", "rootDescription", "extends", "valueDescription", "closed",
                    "objectUnion", "valueSchema", "hasId", "idRequired", "untypedRoot",
                    "orReference", "referenceFirst")
_DIRECTIVE_RE = re.compile(r"\n:(" + "|".join(_DIRECTIVE_NAMES) + r"):")


def split_comment_directives(doc: Optional[str]) -> tuple[Optional[str], dict[str, Any]]:
    """Return (doc without directives, {directive name: value}). Valueless
    directives map to True; choiceConstraints maps to a list of groups, each a
    list of alternatives, each a list of attribute names."""
    if not doc:
        return doc, {}
    m = _DIRECTIVE_RE.search(doc)
    if not m:
        return doc, {}
    directives: dict[str, Any] = {}
    for line in doc[m.start() + 1:].splitlines():
        dm = re.match(r":(\w+):\s*(.*)$", line)
        if dm and dm.group(1) in _DIRECTIVE_NAMES:
            raw = dm.group(2).strip()
            if raw.startswith("``"):
                value: Any = raw.strip("`")
            elif raw:
                value = json.loads(raw)
            else:
                value = True
            if dm.group(1) == "constraint":
                directives.setdefault("constraint", []).append(value)
            else:
                directives[dm.group(1)] = [] if dm.group(1) == "choiceConstraints" else value
        elif line.startswith("- ``") and "choiceConstraints" in directives:
            body = line.strip().removeprefix("- ``").removesuffix("``")
            directives["choiceConstraints"].append([alt.split(" & ") for alt in body.split(" | ")])
    return doc[:m.start()], directives


def _directives(ctx: "BuildContext", doc: Optional[str]) -> dict[str, Any]:
    return split_comment_directives(doc)[1] if ctx.comment_directives else {}


def _doc_text(ctx: "BuildContext", doc: Optional[str]) -> Optional[str]:
    """Schema description for a UML element's doc, honouring the opt-in flags."""
    if ctx.comment_directives:
        doc = split_comment_directives(doc)[0]
    return verbatim_definition(doc) if ctx.verbatim_docs else clean_definition(doc)


def _element_description(ctx: "BuildContext", doc: str) -> Optional[str]:
    """Class / datatype description. The raw-doc fallback is kept for the
    default mode only: with the opt-in flags an empty definition means none."""
    if ctx.comment_directives or ctx.verbatim_docs:
        return _doc_text(ctx, doc) or None
    return clean_definition(doc) or doc.strip()


def _building_block_ref(ctx: "BuildContext", cls: "UmlClass") -> Optional[dict]:
    """$ref to the BB named by a type's :buildingBlock: directive, relative to the output BB."""
    bb = _directives(ctx, cls.doc).get("buildingBlock")
    if not bb and ctx.root_bb_path is not None:
        # --linked: an element of another building block is that block's schema.
        bb = element_bb_path(cls.id)
        if bb == ctx.root_bb_path:
            bb = None
    if not bb or ctx.bb_out_dir is None:
        return None
    target = (ctx.sources_dir / bb / "schema.yaml").resolve()
    return {"$ref": os.path.relpath(target, ctx.bb_out_dir.resolve()).replace(os.sep, "/")}


# --xsd-formats: XSD datatypes rendered as JSON strings (with format where one exists)
# instead of as JSON-LD node $defs.
XSD_STRING_FORMATS = {"XsdAnyUri": "uri", "XsdDate": "date", "XsdDateTime": "date-time",
                      "XsdLanguage": None}


def _id_reference_value_schema() -> dict:
    """--id-reference-type: a JSON-LD node reference {"@id": ...} and nothing else."""
    return {"type": "object", "required": ["@id"], "additionalProperties": False,
            "properties": {"@id": {"type": "string"}}}


def _iri_reference_value_schema() -> dict:
    """--iri-reference-type: a plain string or a JSON-LD node reference {"@id": ...}."""
    return {
        "anyOf": [
            {"type": "string"},
            {"type": "object", "additionalProperties": False, "required": ["@id"],
             "properties": {"@id": {"type": "string"}}},
        ],
    }


def _class_prefix(cls: "UmlClass", ctx: "BuildContext") -> str:
    """Prefix of a class's property keys: its :prefix:, else its :rdfType:'s, else --prefix."""
    d = _directives(ctx, cls.doc)
    if d.get("prefix"):
        return d["prefix"]
    if d.get("rdfType"):
        return re.split(r" [|&] ", d["rdfType"])[0].split(":")[0]
    return ctx.prefix


def _declares_type(cls: "UmlClass", ctx: "BuildContext") -> bool:
    """Whether a class/datatype def gets an @type property. Always, by default; with
    --comment-directives only when the model records one (:rdfType: or :typeSchema:)."""
    if not ctx.comment_directives:
        return True
    d = _directives(ctx, cls.doc)
    return "rdfType" in d or "typeSchema" in d


def _root_alias_or_extension(cls: "UmlClass", d: dict, ctx: "BuildContext",
                              title: Optional[str], description: Optional[str]) -> dict:
    """Root schema for :rootAlias: (allOf [$ref '#/$defs/<root>']) or :extends: (allOf
    [$ref <parent block>, own schema])."""
    node = copy.deepcopy(ctx.local_defs[cls.name])
    if d.get("extends"):
        node = node["allOf"][1]  # class_to_node_def's allOf [$ref <parent>, own schema]
    node.pop("title", None)
    root: dict[str, Any] = OrderedDict()
    root["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    if title:
        root["title"] = title
    defs = OrderedDict(sorted(ctx.local_defs.items()))
    if d.get("rootAlias"):
        if d.get("rootDescription"):
            root["description"] = d["rootDescription"]
        defs[cls.name] = node
        root["allOf"] = [{"$ref": f"#/$defs/{cls.name}"}]
    else:
        node.pop("description", None)
        if description:
            root["description"] = description
        bb = element_bb_path(cls.parents[0])
        target = (ctx.sources_dir / bb / "schema.yaml").resolve()
        if not d.get("untypedRoot"):
            root["type"] = "object"
        root["allOf"] = [{"$ref": os.path.relpath(target, ctx.bb_out_dir.resolve()).replace(os.sep, "/")}, node]
        defs.pop(cls.name, None)
    if defs:
        root["$defs"] = defs
    return root


def _apply_alternative_overrides(union: "UmlClass", overrides: dict, ctx: "BuildContext") -> dict:
    """The union's schema, inline, with one use's annotations of its alternatives applied."""
    schema = _union_def(union, ctx)
    keyword = "oneOf" if "oneOf" in schema else "anyOf"
    branches = []
    for prop, branch in zip([p for p in union.properties if p.name], schema[keyword]):
        branch = dict(branch)
        for key, value in overrides.get(prop.name, {}).items():
            if key == "idRefDescription":
                at_id = dict(branch["properties"]["@id"])
                if value is None:
                    at_id.pop("description", None)
                else:
                    at_id["description"] = value
                branch = {**branch, "properties": {"@id": at_id}}
            elif value is None:
                branch.pop(key, None)
            else:
                branch[key] = value
        branches.append(branch)
    schema = dict(schema)
    schema.pop("description", None)
    schema[keyword] = branches
    return schema


def _union_def(cls: "UmlClass", ctx: "BuildContext") -> dict:
    """A :union: datatype as {description?, anyOf|oneOf: [one schema per attribute]}."""
    keyword = _directives(ctx, cls.doc)["union"]
    schema: dict[str, Any] = {}
    desc = _element_description(ctx, cls.doc) if cls.doc else None
    if desc:
        schema["description"] = desc
    branches = []
    for prop in cls.properties:
        branch = property_to_schema(prop, ctx)
        if branch is not None:
            branches.append(branch)
    schema[keyword] = branches
    if _directives(ctx, cls.doc).get("objectUnion"):
        schema = {"type": "object", **schema}
    return schema


def _bb_refs(node: Any, ctx: "BuildContext") -> Any:
    """A verbatim fragment with its "bb:<path under sources>" $refs made relative to the output BB."""
    if isinstance(node, list):
        return [_bb_refs(x, ctx) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: _bb_refs(v, ctx) for k, v in node.items()}
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("bb:"):
        path, _, frag = ref[len("bb:"):].partition("#")
        target = (ctx.sources_dir / path).resolve()
        out["$ref"] = os.path.relpath(target, ctx.bb_out_dir.resolve()).replace(os.sep, "/") + (f"#{frag}" if frag else "")
    return out


def _type_schema_and_choices(cls: "UmlClass", ctx: "BuildContext") -> tuple[dict, list[dict]]:
    """@type property schema and allOf members for a class/datatype def.
    Without --comment-directives the @type const is prefix:ClassName and there
    are no allOf members."""
    d = _directives(ctx, cls.doc)
    prefix = _class_prefix(cls, ctx)
    key = lambda n: n if (":" in n or n.startswith("@")) else _qname(prefix, n)
    choices = [{"anyOf": [{"required": [key(n) for n in alt]} for alt in g]}
               for g in d.get("choiceConstraints", [])] + [_bb_refs(c, ctx) for c in d.get("constraint", [])]
    type_schema: dict[str, Any] = {}
    if d.get("typeDefault"):
        # @type is an array, so its default is one too.
        type_schema["default"] = d["typeDefault"].split(" | ")
    type_schema["type"] = "array"
    type_schema["items"] = {"type": "string"}
    if d.get("allowedTypes"):
        type_schema["items"]["enum"] = d["allowedTypes"].split(" | ")
    if "typeSchema" in d:
        return d["typeSchema"], choices
    rdf_type = d.get("rdfType") or _qname(ctx.prefix, cls.name)
    if " & " in rdf_type:
        types = rdf_type.split(" & ")
        type_schema["allOf"] = [{"contains": {"const": t}} for t in types]
        type_schema["minItems"] = len(types)
    else:
        types = rdf_type.split(" | ")
        type_schema["contains"] = ({"const": types[0]} if len(types) == 1
                                   else {"anyOf": [{"const": t} for t in types]})
        type_schema["minItems"] = 1
    if "typeDescription" in d:
        type_schema["description"] = d["typeDescription"]
    return type_schema, choices


# ---------------------------------------------------------------------------
# Schema construction
# ---------------------------------------------------------------------------

@dataclass
class BuildContext:
    model: Model
    prefix: str                       # e.g. "cdi"
    shared_bb_relpath: str            # e.g. "../ddicdiDataTypes/schema.yaml"
    shared_defs: set[str]             # names available in the shared BB
    inline_class_names: set[str]      # uml:Class names to inline (no id-ref fallback)
    inline_or_ref_class_names: set[str]  # explicit inline-OR-id-reference (now also default)
    reference_class_names: set[str]   # uml:Class names to force as id-reference only
    exclude_class_names: set[str]     # uml:Class / uml:DataType names to drop entirely (any property typed by one of these is omitted; the type's $def is not registered)
    exclude_property_specs: set[str]  # `ClassName.propertyName` pairs to drop. Use when an inherited UML property is conceptually wrong on a specific subclass (e.g. DataStructure.isDefinedBy belongs on DataStructureComponent subclasses, not on DataStructure variants).
    inline_datatypes: bool            # if True, never $ref shared bb; inline all
    strict_required: bool             # if True, emit `required` per UML lower>=1
    external_class_refs: dict[str, str]  # ClassName -> $ref target if defined in another BB
    local_defs: dict[str, dict]       # name -> $def schema fragment
    expanding: set[str]               # currently-being-expanded type names (cycle guard)
    # v1.1 transitive datatype policy (None = inactive for legacy callers).
    datatype_substitutions: Optional[dict[str, str]] = None
    exclude_datatypes: Optional[set[str]] = None
    target_classes_by_name: Optional[dict[str, "UmlClass"]] = None
    # Opt-in JSON-LD conventions for schema emit (all off by default; see
    # --xsd-formats, --iri-reference-type, --comment-directives, --verbatim-docs).
    xsd_formats: bool = False
    iri_reference_type: Optional[str] = None
    id_reference_type: Optional[str] = None
    comment_directives: bool = False
    verbatim_docs: bool = False
    bb_out_dir: Optional[Path] = None   # output BB dir; base for :buildingBlock: $refs
    sources_dir: Path = DEFAULT_SOURCES_DIR  # metadataBuildingBlocks/_sources
    root_bb_path: Optional[str] = None  # --linked: BB path of the class being emitted


def collect_inherited_properties(class_id: str, model: Model) -> list[Property]:
    """Walk parent chain leaf-first. Within a single class, all properties are
    kept (UCMIS overloads role names — e.g. CodeList has two `cdi:has`
    associations to different targets). Across inheritance, a subclass that
    redefines a name shadows ancestors of that name."""
    result: list[Property] = []
    visited: set[str] = set()
    shadowed: set[str] = set()  # names defined at a more specific level

    def visit(cid: str):
        if cid in visited or cid not in model.elements:
            return
        visited.add(cid)
        cls = model.elements[cid]
        for prop in cls.properties:
            if prop.name and prop.name in shadowed:
                continue
            result.append(prop)
        # Names defined here shadow same-named props on ancestors
        shadowed.update(p.name for p in cls.properties if p.name)
        for parent_id in cls.parents:
            visit(parent_id)

    visit(class_id)
    return result


def _qname(prefix: str, local: str) -> str:
    return f"{prefix}:{local}"


def _ref_to_shared(ctx: BuildContext, def_name: str) -> dict:
    return {"$ref": f"{ctx.shared_bb_relpath}#/$defs/{def_name}"}


def _ref_to_local(def_name: str) -> dict:
    return {"$ref": f"#/$defs/{def_name}"}


def _id_reference_schema(ctx: BuildContext) -> dict:
    if "id-reference" in ctx.shared_defs and not ctx.inline_datatypes:
        return _ref_to_shared(ctx, "id-reference")
    if "id-reference" not in ctx.local_defs:
        ctx.local_defs["id-reference"] = {
            "type": "object",
            "description": "JSON-LD @id reference to a node defined elsewhere",
            "properties": {
                "@id": {"type": "string"},
            },
            "required": ["@id"],
        }
    return _ref_to_local("id-reference")


def _group_to_schema(group: list[Property], ctx: BuildContext) -> Optional[dict]:
    """Render N UML properties that share a role name as a single JSON-Schema
    fragment. For len==1 this is just `property_to_schema`. For len>1 the
    target type schemas are combined into a flat `anyOf`, and multiplicity is
    union-ed (lower = min, upper = max with `*` dominating)."""
    if len(group) == 1:
        return property_to_schema(group[0], ctx)
    inner_options: list[dict] = []
    for p in group:
        opt = _resolve_property_type(p, ctx)
        if opt is None:
            continue
        # Flatten nested anyOf: a class target now resolves to
        # `{anyOf: [target, id-ref]}`, so when N such options are merged into
        # the outer anyOf we want one flat list rather than anyOf-of-anyOf.
        sub_options = opt["anyOf"] if (set(opt.keys()) == {"anyOf"}) else [opt]
        for sub in sub_options:
            if sub not in inner_options:
                inner_options.append(sub)
    if not inner_options:
        return None
    inner = {"anyOf": inner_options} if len(inner_options) > 1 else inner_options[0]
    combined_lower = min(p.lower for p in group)
    uppers = [p.upper for p in group]
    combined_upper = -1 if -1 in uppers else max(uppers)
    synthetic = Property(
        id=group[0].id, name=group[0].name,
        type_id=None, primitive=None,
        lower=combined_lower, upper=combined_upper,
        doc=None, is_assoc_end=False, aggregation=None,
    )
    out = _wrap_multiplicity(inner, synthetic)
    # Combine docs from each property; dedup while preserving first-seen order.
    docs: list[str] = []
    for p in group:
        if not p.doc:
            continue
        d = _doc_text(ctx, p.doc)
        if d and d not in docs:
            docs.append(d)
    if docs:
        out = dict(out)
        out["description"] = "\n\n".join(docs)
    return out


def _target_type_name(prop: Property, ctx: BuildContext) -> Optional[str]:
    """Return the simple class/datatype/enum name of a property's target,
    or None for primitives / unresolvable targets / excluded targets."""
    if prop.primitive or not prop.type_id:
        return None
    target = ctx.model.elements.get(prop.type_id)
    if target is None:
        return None
    if target.name in ctx.exclude_class_names:
        return None
    return target.name


def _has_distinct_named_targets(group: list[Property], ctx: BuildContext) -> bool:
    """True when the group has at least two properties resolving to distinct
    named targets (class / datatype / enum). Used to decide whether to
    disambiguate same-named UML associations by suffixing the target class —
    UCMIS / DDI-CDI re-use bare role names like `has` / `uses` for several
    distinct targets within a single class, and emitting them as one anyOf
    key strips type information from the JSON-LD tree."""
    seen: set[str] = set()
    for prop in group:
        n = _target_type_name(prop, ctx)
        if n is None:
            continue
        seen.add(n)
        if len(seen) >= 2:
            return True
    return False


def _build_properties_dict(
    prop_list: list[Property], ctx: BuildContext,
    owner_class_name: Optional[str] = None,
    owner_prefix: Optional[str] = None,
) -> tuple[OrderedDict, list[str]]:
    """Group UML properties by role name, then build the JSON Schema
    `properties` dict and the matching `required` list. When N>1 properties
    share a role name and target distinct classes, emit one key per target
    suffixed with `_<TargetName>` so each remains unambiguous in the JSON
    tree (the source class is implicit from position).

    `owner_class_name` is the name of the class/datatype whose properties
    are being emitted; used to honour `--exclude-property
    ClassName.propertyName` exclusions (e.g. drop an inherited property
    from a specific subclass)."""
    groups: OrderedDict[str, list[Property]] = OrderedDict()
    for prop in prop_list:
        if not prop.name:
            print(f"WARN: skipping unnamed property id={prop.id}", file=sys.stderr)
            continue
        groups.setdefault(prop.name, []).append(prop)
    properties: OrderedDict = OrderedDict()
    required: list[str] = []
    for name, group in groups.items():
        if owner_class_name and f"{owner_class_name}.{name}" in ctx.exclude_property_specs:
            continue  # excluded for this owner
        if len(group) > 1 and _has_distinct_named_targets(group, ctx):
            # Disambiguate by suffixing each property with its target class.
            # Properties to the same target are merged into a shared anyOf at
            # the same suffixed key.
            by_target: OrderedDict[str, list[Property]] = OrderedDict()
            for prop in group:
                tn = _target_type_name(prop, ctx)
                if tn is None:
                    print(
                        f"WARN: cannot suffix '{name}' for prop id={prop.id} "
                        "(primitive or excluded target); dropping",
                        file=sys.stderr,
                    )
                    continue
                by_target.setdefault(tn, []).append(prop)
            for tn, sub_group in by_target.items():
                sub = _group_to_schema(sub_group, ctx)
                if sub is None:
                    continue
                key = _qname(ctx.prefix, f"{name}_{tn}")
                properties[key] = sub
                if ctx.strict_required and any(p.lower >= 1 for p in sub_group):
                    required.append(key)
        else:
            sub = _group_to_schema(group, ctx)
            if sub is None:
                continue
            key = (_directives(ctx, group[0].doc).get("jsonName")
                   or _qname(owner_prefix or ctx.prefix, name))
            properties[key] = sub
            if ctx.strict_required and any(p.lower >= 1 for p in group):
                required.append(key)
    return properties, required


def datatype_to_def(dt: UmlClass, ctx: BuildContext) -> dict:
    """Build a $def schema body for a uml:DataType."""
    if "union" in _directives(ctx, dt.doc):
        return _union_def(dt, ctx)
    schema: dict[str, Any] = {"type": "object"}
    if "title" in _directives(ctx, dt.doc):
        schema["title"] = _directives(ctx, dt.doc)["title"]
    if dt.doc:
        desc = _element_description(ctx, dt.doc)
        if desc:
            schema["description"] = desc
    type_schema, choices = _type_schema_and_choices(dt, ctx)
    props: OrderedDict = OrderedDict()
    if "contextSchema" in _directives(ctx, dt.doc):
        props["@context"] = _directives(ctx, dt.doc)["contextSchema"]
    if _declares_type(dt, ctx):
        props["@type"] = type_schema
    if _directives(ctx, dt.doc).get("hasId"):
        props["@id"] = {"type": "string"}
        if "idDescription" in _directives(ctx, dt.doc):
            props["@id"]["description"] = _directives(ctx, dt.doc)["idDescription"]
    extra, required = _build_properties_dict(
        collect_inherited_properties(dt.id, ctx.model), ctx,
        owner_class_name=dt.name, owner_prefix=_class_prefix(dt, ctx),
    )
    props.update(extra)
    if props or not ctx.comment_directives:  # an empty object stays {"type": "object"}
        schema["properties"] = props
    dt_d = _directives(ctx, dt.doc)
    if ("rdfType" in dt_d or "typeSchema" in dt_d) and not dt_d.get("typeOptional"):
        # A datatype with a declared RDF type is a typed JSON-LD node: @type required.
        required = ["@type", *required]
    if dt_d.get("idRequired"):
        required = ["@id", *required]
    if required:
        schema["required"] = required
    if choices:
        schema["allOf"] = choices
    if dt_d.get("closed"):
        schema["additionalProperties"] = False
    return schema


def class_to_node_def(cls: UmlClass, ctx: BuildContext) -> dict:
    """Build a $def schema body for a uml:Class (a JSON-LD node)."""
    if "union" in _directives(ctx, cls.doc):
        return _union_def(cls, ctx)
    schema: dict[str, Any] = {"type": "object"}
    if "title" in _directives(ctx, cls.doc):
        schema["title"] = _directives(ctx, cls.doc)["title"]
    if cls.doc:
        desc = _element_description(ctx, cls.doc)
        if desc:
            schema["description"] = desc
    type_schema, choices = _type_schema_and_choices(cls, ctx)
    props: OrderedDict = OrderedDict()
    if "contextSchema" in _directives(ctx, cls.doc):
        props["@context"] = _directives(ctx, cls.doc)["contextSchema"]
    required: list[str] = [] if _directives(ctx, cls.doc).get("typeOptional") else ["@type"]
    if _declares_type(cls, ctx):
        props["@type"] = type_schema
    else:
        required = []
    if ctx.comment_directives:
        # @id as the model declares it: on classes only, with the recorded description.
        d = _directives(ctx, cls.doc)
        if (cls.kind != "datatype" and not d.get("noId")) or d.get("hasId"):
            props["@id"] = {"type": "string"}
            if "idDescription" in d:
                props["@id"]["description"] = d["idDescription"]
    else:
        props["@id"] = {
            "type": "string",
            "description": f"Identifier for this {cls.name} node",
        }
    own = (cls.properties if _directives(ctx, cls.doc).get("extends")
           else collect_inherited_properties(cls.id, ctx.model))
    extra, extra_req = _build_properties_dict(
        own, ctx, owner_class_name=cls.name, owner_prefix=_class_prefix(cls, ctx),
    )
    props.update(extra)
    required.extend(extra_req)
    if _directives(ctx, cls.doc).get("idRequired"):
        required.insert(0, "@id")
    if props or not ctx.comment_directives:  # an empty object stays {"type": "object"}
        schema["properties"] = props
    if required:
        schema["required"] = required
    if choices:
        schema["allOf"] = choices
    if _directives(ctx, cls.doc).get("closed"):
        schema["additionalProperties"] = False
    if _directives(ctx, cls.doc).get("extends"):
        # a specialization of another block's root: allOf [$ref <that block>, own schema]
        target = (ctx.sources_dir / element_bb_path(cls.parents[0]) / "schema.yaml").resolve()
        schema = {"allOf": [{"$ref": os.path.relpath(target, ctx.bb_out_dir.resolve()).replace(os.sep, "/")},
                            schema]}
    return schema


def _wrap_multiplicity(inner: dict, prop: Property) -> dict:
    """Apply UML multiplicity to a JSON Schema fragment.

    upper==1   → return inner unchanged.
    upper==-1  → wrap in array (array-only convention; minItems if lower>=1).
    upper>1    → array with maxItems.
    """
    if prop.upper == 1:
        return inner
    array_schema: dict[str, Any] = {"type": "array", "items": inner}
    if prop.lower >= 1:
        array_schema["minItems"] = prop.lower
    if prop.upper > 1:
        array_schema["maxItems"] = prop.upper
    return array_schema


def property_to_schema(prop: Property, ctx: BuildContext) -> Optional[dict]:
    """Render one UML property as a JSON Schema fragment, including
    multiplicity wrapping and an optional `description`."""
    if not prop.name:
        # Anonymous association end where role name couldn't be recovered;
        # skip rather than emit a malformed `cdi:` key.
        print(f"WARN: skipping unnamed property id={prop.id}", file=sys.stderr)
        return None
    inner = (_resolve_property_type(prop, ctx)
             if not _directives(ctx, prop.doc).get("schemaRef") else {})
    if inner is None:
        return None
    d = _directives(ctx, prop.doc)
    if d.get("schemaRef"):
        bb, _, frag = d["schemaRef"].partition("#")
        target = (ctx.sources_dir / bb / "schema.yaml").resolve()
        inner = {"$ref": os.path.relpath(target, ctx.bb_out_dir.resolve()).replace(os.sep, "/") + "#" + frag}
    if d.get("alternativeOverrides") and prop.type_id in ctx.model.elements:
        inner = _apply_alternative_overrides(ctx.model.elements[prop.type_id], d["alternativeOverrides"], ctx)
    if "valueSchema" in d:
        inner = _bb_refs(d["valueSchema"], ctx)
    if "valueDescription" in d:
        inner = {**inner, "description": d["valueDescription"]}
    if inner == _iri_reference_value_schema() and ("idRefDescription" in d or d.get("referenceFirst")):
        string, ref = copy.deepcopy(inner["anyOf"])
        if "idRefDescription" in d:
            ref["properties"]["@id"]["description"] = d["idRefDescription"]
        inner = {"anyOf": [ref, string] if d.get("referenceFirst") else [string, ref]}
    elif "orReference" in d:
        ref = _id_reference_value_schema()
        if "idRefDescription" in d:
            ref["properties"]["@id"]["description"] = d["idRefDescription"]
        objref = (ctx.sources_dir / "cdifDataType" / "objectReference" / "schema.yaml").resolve()
        alts = {"value": inner, "string": {"type": "string"}, "idReference": ref,
                "objectReference": {"$ref": os.path.relpath(objref, ctx.bb_out_dir.resolve()).replace(os.sep, "/")}}
        # an entry is a name or {name: {annotations of that alternative}}
        inner = {"anyOf": [alts[t] if isinstance(t, str) else {**alts[next(iter(t))], **next(iter(t.values()))}
                           for t in d["orReference"]]}
    elif "idRefDescription" in d and inner.get("properties", {}).get("@id"):
        inner = {**inner, "properties": {"@id": {**inner["properties"]["@id"],
                                                 "description": d["idRefDescription"]}}}
    if d.get("keywords"):
        inner = {**inner, **d["keywords"]}
    also_string = d.get("alsoAcceptsString")
    if also_string == "first":
        inner = {"anyOf": [{"type": "string"}, inner]}
    elif also_string:
        inner = {"anyOf": [inner, {"type": "string"}]}
    out = _wrap_multiplicity(inner, prop)
    if "itemsDescription" in d and out.get("type") == "array":
        out = {**out, "items": {**out["items"], "description": d["itemsDescription"]}}
    if "itemsDefault" in d and out.get("type") == "array":
        out = {**out, "items": {"default": d["itemsDefault"], **out["items"]}}
    if d.get("arrayKeywords") and out.get("type") == "array":
        out = {**out, **d["arrayKeywords"]}
    if "minItems" in d and out.get("type") == "array":
        out = dict(out)
        if d["minItems"] is None:
            out.pop("minItems", None)
        else:
            out["minItems"] = d["minItems"]
    desc = _doc_text(ctx, prop.doc) if prop.doc else None
    if desc:
        # Prefer the description on the outer (multiplicity) wrapper if array,
        # else inline it on the inner.
        out = dict(out)  # copy so we don't mutate $ref dicts shared across props
        if d.get("descriptionOnItems") and "items" in out:
            out["items"] = {**out["items"], "description": desc}
        else:
            out["description"] = desc
    if "default" in d:
        out = {"default": d["default"], **out}
    return out


def _resolve_property_type(prop: Property, ctx: BuildContext) -> Optional[dict]:
    if prop.primitive:
        return {"type": prop.primitive}
    if not prop.type_id:
        if ctx.comment_directives and prop.type_href is None:
            return {}  # an untyped attribute: any value
        return {"type": "string"}  # unknown type → permissive string
    target = ctx.model.elements.get(prop.type_id)
    if target is None:
        # Type id not present in this XMI (could be cross-package ref).
        return {"type": "string"}

    if target.name in ctx.exclude_class_names:
        # Caller drops this property entirely (also prevents the target's $def
        # from being registered, since _resolve_class_target / _resolve_datatype_ref
        # are never reached).
        return None

    if target.kind == "enumeration":
        # Emit literal-list enum
        return {"type": "string", "enum": list(target.literals)}

    if ctx.iri_reference_type and target.name == ctx.iri_reference_type:
        return _iri_reference_value_schema()
    if ctx.id_reference_type and target.name == ctx.id_reference_type:
        return _id_reference_value_schema()
    if ctx.xsd_formats and target.name in XSD_STRING_FORMATS:
        fmt = XSD_STRING_FORMATS[target.name]
        return {"type": "string", "format": fmt} if fmt else {"type": "string"}

    if target.kind == "datatype":
        return _resolve_datatype_ref(target, ctx)

    # uml:Class:
    placement = _directives(ctx, prop.doc).get("inlineOrByReference")
    if target.name in ctx.reference_class_names or placement == "byReference":
        return _id_reference_schema(ctx)
    if target.name in ctx.inline_class_names or placement == "inline":
        # Force inline-only (no id-reference fallback). Useful for true
        # compositions where linking by @id doesn't make sense.
        return _resolve_class_target(target, ctx)
    # Default: inline-or-ref. Prefer a $ref to whichever other BB already
    # defines this class; if no other BB has it, inline locally. Either way,
    # also accept a plain id-reference (the JSON-LD embed-or-link pattern).
    return {
        "anyOf": [
            _resolve_class_target(target, ctx),
            _id_reference_schema(ctx),
        ],
    }


def _resolve_class_target(cls: UmlClass, ctx: BuildContext) -> dict:
    """Resolve a uml:Class target to a $ref. Prefers an external BB that
    already defines the class (so we don't duplicate definitions across BBs);
    falls back to inlining locally."""
    bb_ref = _building_block_ref(ctx, cls)
    if bb_ref:
        return bb_ref
    if ctx.comment_directives:
        # Defined in this XMI: never replaced by a sibling BB that happens to own a class
        # of the same name (sibling-BB lookup keys on the @type's local name).
        return _inline_class_ref(cls, ctx)
    ext = ctx.external_class_refs.get(cls.name)
    if ext:
        return {"$ref": ext}
    return _inline_class_ref(cls, ctx)


def _resolve_datatype_ref(dt: UmlClass, ctx: BuildContext) -> dict:
    """Return a schema fragment that references this dt-type, either via
    shared-BB $ref or by inlining (and registering) a local $def."""
    bb_ref = _building_block_ref(ctx, dt)
    if bb_ref:
        return bb_ref
    if not ctx.inline_datatypes and dt.name in ctx.shared_defs and not ctx.comment_directives:
        return _ref_to_shared(ctx, dt.name)
    # Inline: ensure local $def exists
    if dt.name not in ctx.local_defs:
        if dt.name in ctx.expanding:
            # Cycle: rely on the in-progress $def already being registered or
            # mark a placeholder that will be filled when the outer call returns.
            ctx.local_defs[dt.name] = {
                "type": "object",
                "$comment": f"cycle stub for {dt.name}",
            }
        else:
            ctx.expanding.add(dt.name)
            ctx.local_defs[dt.name] = datatype_to_def(dt, ctx)
            ctx.expanding.discard(dt.name)
    return _ref_to_local(dt.name)


def _inline_class_ref(cls: UmlClass, ctx: BuildContext) -> dict:
    """Inline a uml:Class as a local $def (used for compositions)."""
    if cls.name not in ctx.local_defs:
        if cls.name in ctx.expanding:
            return _ref_to_local(cls.name)
        ctx.expanding.add(cls.name)
        ctx.local_defs[cls.name] = class_to_node_def(cls, ctx)
        ctx.expanding.discard(cls.name)
    return _ref_to_local(cls.name)


# ---------------------------------------------------------------------------
# Top-level schema assembly
# ---------------------------------------------------------------------------

def build_root_schema(
    classes: list[UmlClass],
    ctx: BuildContext,
    title: str,
    description: Optional[str],
) -> dict:
    """Emit a BB schema.yaml whose root *is* the Node definition (single root)
    or an `anyOf` of Node $refs (multiple roots). The wrapper pattern that
    accepts single/array/@graph forms is intentionally omitted — that wrapping
    is applied by profile schemas that compose this BB."""
    # Build per-class node defs (registers them in ctx.local_defs)
    node_def_names: list[str] = []
    for cls in classes:
        if cls.name not in ctx.local_defs:
            ctx.expanding.add(cls.name)
            ctx.local_defs[cls.name] = class_to_node_def(cls, ctx)
            ctx.expanding.discard(cls.name)
        node_def_names.append(cls.name)

    if ctx.comment_directives and ctx.root_bb_path:
        # $defs entries other blocks reference (:exportedDef:) stay in $defs even if unused here
        for el in list(ctx.model.elements.values()):
            if element_bb_path(el.id) == ctx.root_bb_path and el.name not in ctx.local_defs                     and el not in classes and _directives(ctx, el.doc).get("exportedDef"):
                if el.kind == "enumeration":
                    ctx.local_defs[el.name] = {"type": "string", "enum": list(el.literals)}
                elif el.kind == "class":
                    _inline_class_ref(el, ctx)
                else:
                    _resolve_datatype_ref(el, ctx)
    if ctx.comment_directives and len(classes) == 1:
        d = _directives(ctx, classes[0].doc)
        if d.get("rootAlias") or d.get("extends"):
            return _root_alias_or_extension(classes[0], d, ctx, title, description)

    root: dict[str, Any] = OrderedDict()
    root["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    if title:
        root["title"] = title
    if description:
        root["description"] = description

    if len(node_def_names) == 1:
        # Single root: promote the class def to the schema root. Drop the
        # class-level `description` since the root carries one already.
        node = copy.deepcopy(ctx.local_defs[node_def_names[0]])
        node.pop("description", None)
        for k, v in node.items():
            root[k] = v
    else:
        # Multiple roots: $ref each at the root via anyOf, classes live in $defs.
        root["anyOf"] = [_ref_to_local(n) for n in node_def_names]

    # Detect self-references to a single-root class: if the root class is
    # referenced by `#/$defs/<name>` from anywhere (its own properties or a
    # helper $def), keep that root class in $defs so the ref resolves.
    self_referenced_roots: set[str] = set()
    if len(node_def_names) == 1:
        only_root = node_def_names[0]
        target = f"#/$defs/{only_root}"
        def _has_ref(o: Any) -> bool:
            if isinstance(o, dict):
                if o.get("$ref") == target:
                    return True
                return any(_has_ref(v) for v in o.values())
            if isinstance(o, list):
                return any(_has_ref(v) for v in o)
            return False
        scan_targets = [v for k, v in root.items() if k != "$defs"]
        scan_targets.extend(v for k, v in ctx.local_defs.items() if k != only_root)
        if any(_has_ref(t) for t in scan_targets):
            self_referenced_roots.add(only_root)

    # $defs holds helpers (and, for the multi-root case, the root classes too).
    helper_keys = sorted(k for k in ctx.local_defs if k not in node_def_names)
    defs = OrderedDict()
    if len(node_def_names) > 1:
        for n in node_def_names:
            defs[n] = ctx.local_defs[n]
    else:
        for n in node_def_names:
            if n in self_referenced_roots:
                defs[n] = ctx.local_defs[n]
    for k in helper_keys:
        defs[k] = ctx.local_defs[k]
    if defs:
        root["$defs"] = defs
    return root


# ---------------------------------------------------------------------------
# YAML emit (block style, quoted JSON-LD/prefixed keys, folded descriptions)
# ---------------------------------------------------------------------------

class _BBYamlDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, data: str):
    style = None
    if "\n" in data:
        style = "|"
    elif len(data) > 80:
        style = ">"
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _ordered_dict_representer(dumper, data):
    return dumper.represent_dict(data.items())


_BBYamlDumper.add_representer(str, _str_representer)
_BBYamlDumper.add_representer(OrderedDict, _ordered_dict_representer)


def _quote_special_keys(obj: Any) -> Any:
    """JSON-LD keys (@type, @id, @graph) and prefixed keys (cdi:foo) need to be
    quoted in YAML. PyYAML quotes strings that *contain* `:` automatically when
    used as values, but as mapping keys it sometimes emits them unquoted. We
    side-step that by ensuring every key is a plain str — the str representer
    above promotes any string with `:` or `@` to single-quoted form.
    """
    return obj  # placeholder — handled at representer level below


_IDENT_RE = re.compile(r"^[\w@$#./:-]+$")


def _key_representer_safer(dumper, data: str):
    # Heuristic: distinguish structural identifiers (keys, $refs, CURIEs,
    # JSON-Pointers, URIs without spaces) from prose. Identifiers go single-
    # quoted so YAML-significant chars (`@`, `:`, `#`, `$`) are unambiguous;
    # prose with whitespace gets folded for readability.
    style = None
    if not data:
        style = "'"
    elif _IDENT_RE.match(data) and (
        data.startswith(("@", "$", "#", "/")) or ":" in data or "#" in data
    ):
        style = "'"
    elif "\n" in data:
        style = "|"
    elif len(data) > 80:
        style = ">"
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


# Override default str representer with the key-aware one. This is fine because
# our schemas don't have legitimate long-string *values* that need different
# treatment; we want keys quoted and we'll re-fold long descriptions manually.
_BBYamlDumper.add_representer(str, _key_representer_safer)


def emit_yaml(schema: dict, out_path: Path):
    text = yaml.dump(
        schema,
        Dumper=_BBYamlDumper,
        sort_keys=False,
        default_flow_style=False,
        width=88,
        allow_unicode=True,
        indent=2,
    )
    out_path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Companion files
# ---------------------------------------------------------------------------

def emit_companions(out_dir: Path, bb_name: str, title: str, abstract: str,
                    class_names: list[str], prefix: str, prefix_iri: str,
                    today: str):
    bblock = OrderedDict([
        ("$schema", "metaschema.yaml"),
        ("name", title),
        ("abstract", abstract),
        ("status", "under-development"),
        ("dateTimeAddition", f"{today}T00:00:00Z"),
        ("itemClass", "schema"),
        ("register", "cdif-building-block-register"),
        ("version", "0.1"),
        ("dateOfLastChange", today),
        ("link", "https://github.com/usgin/metadataBuildingBlocks"),
        ("maturity", "draft"),
        ("scope", "unstable"),
        ("tags", ["CDIF", "DDI-CDI"] + class_names),
        ("sources", [
            {
                "title": "DDI-CDI 1.0 Specification",
                "link": "https://ddialliance.org/Specification/DDI-CDI/1.0/",
            }
        ]),
    ])
    (out_dir / "bblock.json").write_text(
        json.dumps(bblock, indent=2) + "\n", encoding="utf-8")

    context = {"@context": {prefix: prefix_iri}}
    (out_dir / "context.jsonld").write_text(
        json.dumps(context, indent=2) + "\n", encoding="utf-8")

    shacl = textwrap.dedent(f"""\
        # SHACL rules skeleton for {title}.
        # TODO: author NodeShape and PropertyShape constraints from the UML model.

        @prefix sh:   <http://www.w3.org/ns/shacl#> .
        @prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
        @prefix {prefix}:  <{prefix_iri}> .
        @prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
        """)
    (out_dir / "rules.shacl").write_text(shacl, encoding="utf-8")

    examples = [
        {
            "title": f"Minimal {class_names[0]}",
            "content": "TODO: replace with a JSON-LD example.",
        }
    ]
    (out_dir / "examples.yaml").write_text(
        yaml.safe_dump(examples, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Shared-types discovery
# ---------------------------------------------------------------------------

def discover_shared_defs(shared_yaml_path: Path) -> set[str]:
    if not shared_yaml_path.exists():
        return set()
    with open(shared_yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return set()
    defs = data.get("$defs") or {}
    return set(defs.keys())


def _extract_root_class_names(doc: dict, prefix: str) -> list[str]:
    """Extract the class name(s) owned by a BB whose root *is* the Node.
    Three shapes recognized:
      1) Single-class:    properties.@type.contains.const = "<prefix>:<Class>".
      2) Multi-class @type: properties.@type.anyOf is a list of
         {contains: {const: "<prefix>:<Class>"}}.
      3) Multi-root anyOf at schema root: anyOf of $refs to local $defs,
         each $def carrying a single-class @type contains.const.
    Returns the class names (without prefix). Empty list if no root class
    detected (e.g. umbrella BBs like ddicdiAgent that only dispatch via
    external $refs).
    """
    names: list[str] = []
    plen = len(prefix) + 1

    def _const_to_name(const) -> Optional[str]:
        if isinstance(const, str) and const.startswith(prefix + ":"):
            return const[plen:]
        return None

    props = doc.get("properties")
    if isinstance(props, dict):
        type_prop = props.get("@type")
        if isinstance(type_prop, dict):
            contains = type_prop.get("contains")
            if isinstance(contains, dict):
                n = _const_to_name(contains.get("const"))
                if n:
                    names.append(n)
            for branch in type_prop.get("anyOf") or []:
                if isinstance(branch, dict):
                    sub = branch.get("contains")
                    if isinstance(sub, dict):
                        n = _const_to_name(sub.get("const"))
                        if n and n not in names:
                            names.append(n)

    if not names:
        # Multi-root anyOf at top: each branch is {$ref: '#/$defs/X'}.
        for branch in doc.get("anyOf") or []:
            if not isinstance(branch, dict):
                continue
            ref = branch.get("$ref")
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                continue
            def_name = ref[len("#/$defs/"):]
            sub = (doc.get("$defs") or {}).get(def_name)
            if isinstance(sub, dict):
                sub_props = sub.get("properties")
                if isinstance(sub_props, dict):
                    sub_type = sub_props.get("@type")
                    if isinstance(sub_type, dict):
                        contains = sub_type.get("contains")
                        if isinstance(contains, dict):
                            n = _const_to_name(contains.get("const"))
                            if n and n not in names:
                                names.append(n)
    return names


def discover_external_class_refs(
    out_bb_dir: Path, sources_dir: Path, prefix: str,
) -> dict[str, str]:
    """Walk every other schema.yaml under sources_dir and return a map of
    {ClassName: $ref-target} pointing to the BB that "owns" each class.
    A class is owned by a BB only when it is that BB's *root* class — i.e.,
    the schema's properties['@type'].contains.const is "<prefix>:<ClassName>".
    $defs entries are intentionally NOT registered: they can be private
    inlinings rather than canonical homes, and registering them introduces
    order-dependence between BBs. The $ref-target is a relative path from
    `out_bb_dir`.

    When a class name appears as a root in multiple BBs (e.g. when the same
    UML class has been cloned into a parallel directory like ddiProperties
    vs ddiCDIFProperties), prefer the BB whose parent directory matches
    out_bb_dir's parent — i.e. don't leak a cross-package reference."""
    import os
    out_resolved = out_bb_dir.resolve()
    out_parent = out_resolved.parent
    # Collect candidates per class as (rel_path, same_parent) tuples,
    # then pick the same-parent one if available; otherwise the first.
    candidates: dict[str, list[tuple[str, bool]]] = {}
    for schema_path in sorted(sources_dir.rglob("schema.yaml")):
        bb_dir = schema_path.parent.resolve()
        if bb_dir == out_resolved:
            continue
        try:
            with open(schema_path, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        root_classes = _extract_root_class_names(doc, prefix)
        # Also derive a class name from the BB directory name (project
        # convention: ddicdi<ClassName>; also kebab `ddi-cdif-<kebab-name>`).
        # This catches abstract parents like `ValueDomain` whose concrete
        # subclasses are roots of the same BB, and umbrella BBs like
        # `ddi-cdif-agent` whose root is anyOf of sibling-BB $refs.
        bb_dir_name = bb_dir.name
        kebab_prefixes = ("ddi-cdif-",)
        camel_prefixes = ("ddicdi", "cdif", "skos", "dcat", "schema", "prov", "xas")
        derived: Optional[str] = None
        for prefix_name in kebab_prefixes:
            if bb_dir_name.startswith(prefix_name) and len(bb_dir_name) > len(prefix_name):
                tail = bb_dir_name[len(prefix_name):]
                derived = "".join(p[:1].upper() + p[1:] for p in tail.split("-") if p)
                break
        if derived is None:
            for prefix_name in camel_prefixes:
                if bb_dir_name.startswith(prefix_name) and len(bb_dir_name) > len(prefix_name):
                    tail = bb_dir_name[len(prefix_name):]
                    if tail[:1].isupper():
                        derived = tail
                    break
        if derived and derived not in root_classes:
            root_classes.append(derived)
        if not root_classes:
            continue
        try:
            rel = os.path.relpath(bb_dir, out_resolved).replace(os.sep, "/")
        except ValueError:
            continue
        is_same_parent = bb_dir.parent == out_parent
        for root_class in root_classes:
            candidates.setdefault(root_class, []).append((rel, is_same_parent))

    registry: dict[str, str] = {}
    for cls, opts in candidates.items():
        same = [o for o in opts if o[1]]
        chosen = same[0] if same else opts[0]
        registry[cls] = f"{chosen[0]}/schema.yaml"
    return registry


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _today_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()


def _split_csv(s: Optional[str]) -> list[str]:
    return [x.strip() for x in s.split(",")] if s else []


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate a CDIF building-block schema.yaml from canonical XMI.",
    )
    ap.add_argument("--xmi", type=Path, required=True,
                    help="Path to canonical XMI 2.5.1 file.")
    ap.add_argument("--class", dest="classes", default=None,
                    help="Comma-separated list of root class names. "
                         "Ignored when --config is given.")
    ap.add_argument("--bb-name", default=None,
                    help="Building-block directory name (e.g. ddicdiValueDomain). "
                         "Required for schema emit; ignored when --config + "
                         "--uml-only are used together.")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Parent directory; the BB folder will be created/replaced inside. "
                         "Required for schema emit.")
    ap.add_argument("--linked", action="store_true",
                    help="--xmi is one file of a linked set (bblock_to_xmi.py --linked): follow "
                         "<type href>s into the other files, and emit a $ref to another building "
                         "block's schema.yaml for any type that block defines.")
    ap.add_argument("--sources-dir", type=Path, default=DEFAULT_SOURCES_DIR,
                    help="metadataBuildingBlocks _sources directory, read for sibling-BB "
                         "class lookup and shared datatypes. Default: "
                         "../metadataBuildingBlocks/_sources relative to cdif-umlmodel.")
    ap.add_argument("--config", type=Path, default=None,
                    help="Path to a ucmism2m JSON configuration file. When given, "
                         "the configuration drives UML emit (target classes, renames, "
                         "associations, profile metadata). Pair with --emit-uml PATH "
                         "and --uml-only to skip schema emit.")
    ap.add_argument("--title", default=None,
                    help="Human-readable BB title (default: derived from --bb-name).")
    ap.add_argument("--description", default=None,
                    help="BB-level description (default: extracted from first class docstring).")
    ap.add_argument("--prefix", default="cdi",
                    help="JSON-LD prefix label (default: cdi).")
    ap.add_argument("--prefix-iri",
                    default="http://ddialliance.org/Specification/DDI-CDI/1.0/RDF/",
                    help="IRI for the prefix.")
    ap.add_argument("--shared-bb",
                    default="../ddicdiDataTypes/schema.yaml",
                    help="Relative path to shared-types BB schema.yaml "
                         "(default: ../ddicdiDataTypes/schema.yaml).")
    ap.add_argument("--inline", default="",
                    help="Comma-separated uml:Class names to inline as compositions.")
    ap.add_argument("--inline-or-ref", default="",
                    help="Comma-separated names that may be either an inlined object "
                         "or an @id reference (emitted as anyOf).")
    ap.add_argument("--reference", default="",
                    help="Comma-separated names to force as id-reference (overrides --inline).")
    ap.add_argument("--exclude-class", default="",
                    help="Comma-separated uml:Class / uml:DataType names to drop entirely. "
                         "Properties typed by an excluded class are omitted; the type's $def "
                         "is not emitted. Use for classes the project intentionally won't "
                         "implement (e.g. CatalogDetails).")
    ap.add_argument("--exclude-property", default="",
                    help="Comma-separated `ClassName.propertyName` pairs to drop. "
                         "Use when an inherited UML property is conceptually wrong on a "
                         "specific subclass (e.g. DataStructure.isDefinedBy belongs on "
                         "DataStructureComponent subclasses, not on the DataStructure variants).")
    ap.add_argument("--inline-datatypes", action="store_true",
                    help="Inline all dt-types locally instead of $ref-ing the shared BB.")
    ap.add_argument("--schema-only", action="store_true",
                    help="Only emit schema.yaml; skip bblock.json/context.jsonld/rules.shacl/examples.yaml.")
    ap.add_argument("--strict-required", action="store_true",
                    help="Emit `required` for every property with UML lower>=1. "
                         "Default is project-style: only @type required.")
    # Opt-in JSON-LD conventions (schema emit only; used for JSON Schema <-> XMI round trips)
    ap.add_argument("--xsd-formats", action="store_true",
                    help="Render XsdAnyUri / XsdDate / XsdDateTime / XsdLanguage-typed "
                         "attributes as JSON strings (with format uri / date / date-time) "
                         "instead of as JSON-LD node $defs.")
    ap.add_argument("--iri-reference-type", default=None, metavar="NAME",
                    help="DataType name whose attributes accept a plain string OR a "
                         "JSON-LD node reference {\"@id\": ...} (e.g. IriReference).")
    ap.add_argument("--id-reference-type", default=None, metavar="NAME",
                    help="DataType name whose attributes are a JSON-LD node reference "
                         "{\"@id\": ...} with no other keys (e.g. IdReference).")
    ap.add_argument("--comment-directives", action="store_true",
                    help="Read JSON-LD directives from comments and drop them from "
                         "descriptions: on classes :rdfType:, :choiceConstraints:, "
                         ":buildingBlock:; on attributes :inlineOrByReference:, "
                         ":alsoAcceptsString:. See split_comment_directives().")
    ap.add_argument("--verbatim-docs", action="store_true",
                    help="Keep the Definition text of comments exactly as written "
                         "instead of collapsing whitespace.")
    # ------------------------------------------------------------------ UML emit
    ap.add_argument("--emit-uml", type=Path, default=None,
                    help="Also write an Eclipse UML2 (XMI 2.5) profile model to PATH. "
                         "Uses the same --class roots + --inline / --reference / "
                         "--exclude-class logic as schema emit.")
    ap.add_argument("--emit-ea-xmi", type=Path, default=None,
                    help="Also write an Enterprise Architect XMI 1.1 profile model "
                         "to PATH. Same source data, different on-the-wire format "
                         "for direct import into EA.")
    ap.add_argument("--emit-puml", type=Path, default=None,
                    help="Also write per-class PlantUML diagrams under DIR. "
                         "Produces index.pu plus Classes/<Name>.pu and "
                         "DataTypes/<Name>.pu. Render to SVG with plantuml.jar.")
    ap.add_argument("--emit-html", type=Path, default=None,
                    help="Also write a static HTML model browser under DIR. "
                         "If --emit-puml is given in the same run, diagrams "
                         "are embedded from that location; otherwise pass "
                         "--puml-dir DIR to point at pre-rendered .pu files.")
    ap.add_argument("--puml-dir", type=Path, default=None,
                    help="Directory of pre-rendered PlantUML files for HTML "
                         "embedding. Ignored unless --emit-html is used.")
    ap.add_argument("--plantuml-jar", type=Path,
                    default=Path(os.environ.get("PLANTUML_JAR", "")) if os.environ.get("PLANTUML_JAR") else None,
                    help="Path to plantuml.jar. When set together with "
                         "--emit-puml, .pu files are rendered to .svg via "
                         "java -jar. Falls back to PLANTUML_JAR env var.")
    ap.add_argument("--java-exe",
                    default=os.environ.get("JAVA_EXE", "java"),
                    help="Path to java executable to use for plantuml.jar. "
                         "Default 'java' (on PATH). Falls back to JAVA_EXE "
                         "env var. PlantUML 1.2024+ requires Java 11+.")
    ap.add_argument("--cross-profile-registry", type=Path, default=None,
                    help="Path to JSON file mapping {className: profileName} "
                         "for cross-profile linking in HTML output. Build via "
                         "build-docs.ps1 or hand-author for explicit control.")
    ap.add_argument("--uml-only", action="store_true",
                    help="Skip schema.yaml + companions; only write UML/EA-XMI files.")
    ap.add_argument("--uml-acronym", default=None,
                    help="Short identifier used to build xmi:id values "
                         "(e.g. 'cdifCodelist'). Defaults to --bb-name with "
                         "any 'ddicdi' prefix stripped, lowercased first-letter.")
    ap.add_argument("--uml-profile-uri", default=None,
                    help="URI for the profile namespace, used as xmi:uuid prefix "
                         "(e.g. 'https://w3id.org/cdif/codelist/1.0/xmi/').")
    ap.add_argument("--uml-model-name", default=None,
                    help="Name of the uml:Model element (e.g. 'CDIFCodelist'). "
                         "Defaults to --bb-name.")
    ap.add_argument("--uml-model-definition", default=None,
                    help="Top-level model description (becomes a Model ownedComment).")
    ap.add_argument("--uml-main-package", default=None,
                    help="Name of the main uml:Package inside the model "
                         "(defaults to --uml-model-name).")
    ap.add_argument("--uml-main-package-definition", default=None,
                    help="Definition for the main package.")
    ap.add_argument("--uml-classes-package", default="Classes",
                    help="Name of the sub-package that holds the selected classes "
                         "(default: Classes). Use empty string to put classes "
                         "directly under the main package.")
    ap.add_argument("--uml-classes-package-definition", default=None,
                    help="Definition for the classes sub-package.")
    args = ap.parse_args(argv)

    model = load_linked_xmi(args.xmi) if args.linked else parse_xmi(args.xmi)
    print(f"Parsed XMI: {len(model.elements)} elements", file=sys.stderr)

    # ----- Config-driven UML emit short-circuit (Phase 2 / EA / PlantUML / HTML)
    if args.config:
        if not (args.emit_uml or args.emit_ea_xmi or args.emit_puml or args.emit_html):
            print("ERROR: --config requires at least one of "
                  "--emit-uml / --emit-ea-xmi / --emit-puml / --emit-html",
                  file=sys.stderr)
            return 2
        overrides = {
            "inline": set(_split_csv(args.inline)),
            "exclude_class": set(_split_csv(args.exclude_class)),
        }
        if args.emit_uml:
            emit_uml_from_config(args.config, model, args.emit_uml,
                                 ctx_overrides=overrides, output_format="canonical")
            print(f"Wrote {args.emit_uml}", file=sys.stderr)
        if args.emit_ea_xmi:
            emit_uml_from_config(args.config, model, args.emit_ea_xmi,
                                 ctx_overrides=overrides, output_format="ea")
            print(f"Wrote {args.emit_ea_xmi}", file=sys.stderr)
        if args.emit_puml:
            puml_overrides = dict(overrides)
            if args.cross_profile_registry and args.cross_profile_registry.exists():
                with open(args.cross_profile_registry, "r", encoding="utf-8") as f:
                    puml_overrides["cross_profile_registry"] = json.load(f)
            emit_uml_from_config(args.config, model, args.emit_puml,
                                 ctx_overrides=puml_overrides, output_format="puml")
            if args.plantuml_jar and args.plantuml_jar.exists():
                n = render_puml_to_svg(args.emit_puml, args.plantuml_jar,
                                       java_exe=args.java_exe)
                print(f"Rendered {n} SVGs via {args.plantuml_jar}", file=sys.stderr)
        if args.emit_html:
            html_overrides = dict(overrides)
            html_overrides["puml_dir"] = args.puml_dir or args.emit_puml
            if args.cross_profile_registry and args.cross_profile_registry.exists():
                with open(args.cross_profile_registry, "r", encoding="utf-8") as f:
                    html_overrides["cross_profile_registry"] = json.load(f)
            emit_uml_from_config(args.config, model, args.emit_html,
                                 ctx_overrides=html_overrides, output_format="html")
        return 0

    # Argparse-required-vs-config check: classic args required only when
    # --config is not used.
    if not args.classes or not args.bb_name or not args.out_dir:
        print("ERROR: --class, --bb-name and --out-dir are required unless --config is given",
              file=sys.stderr)
        return 2

    # Resolve class names → ids
    class_names = _split_csv(args.classes)
    classes: list[UmlClass] = []
    for name in class_names:
        ids = model.name_to_id.get(name, [])
        if not ids:
            print(f"ERROR: class '{name}' not found in XMI", file=sys.stderr)
            return 2
        if len(ids) > 1:
            print(f"WARN: '{name}' is ambiguous ({len(ids)} elements); using first",
                  file=sys.stderr)
        cls = model.elements[ids[0]]
        if cls.kind != "class":
            print(f"WARN: '{name}' is a {cls.kind}, not a uml:Class; emitting anyway",
                  file=sys.stderr)
        classes.append(cls)

    # Compute shared-defs registry from the sibling BB.
    # Try (1) relative to the output BB dir (the textually-correct lookup),
    # then (2) fall back to the canonical _sources/ddiProperties tree so the
    # script also works when --out-dir is a scratch directory.
    bb_out_dir = args.out_dir / args.bb_name
    bb_out_dir.mkdir(parents=True, exist_ok=True)
    shared_yaml_abs = (bb_out_dir / args.shared_bb).resolve()
    shared_defs = discover_shared_defs(shared_yaml_abs)
    if not shared_defs:
        # Fallback: resolve the relative path from a hypothetical sibling under
        # _sources/ddiProperties/<bb_name>.
        fallback = (args.sources_dir / "ddiProperties" / args.bb_name / args.shared_bb).resolve()
        shared_defs = discover_shared_defs(fallback)
        if shared_defs:
            print(f"INFO: shared $defs discovered via fallback: {fallback}",
                  file=sys.stderr)
    if not shared_defs and not args.inline_datatypes:
        print(f"WARN: no shared $defs discovered at {shared_yaml_abs}; "
              "set --inline-datatypes if intentional.", file=sys.stderr)

    # Build a registry of class names defined in OTHER BBs so we can $ref
    # out instead of duplicating definitions. Falls back to local inline if
    # no external definition is found.
    external_class_refs = discover_external_class_refs(
        out_bb_dir=bb_out_dir, sources_dir=args.sources_dir, prefix=args.prefix,
    )
    # The current BB's own root classes win over any external registration
    # (e.g. a cloned parallel BB). Without this, an inter-class reference
    # like Descriptor.hasValueFrom -> DescriptorValueDomain would resolve to
    # a parallel-package BB instead of the local $def.
    own_root_names = {c.name for c in classes}
    for own in own_root_names:
        external_class_refs.pop(own, None)
    if external_class_refs:
        print(f"INFO: discovered {len(external_class_refs)} class definitions "
              f"in sibling BBs", file=sys.stderr)

    ctx = BuildContext(
        model=model,
        prefix=args.prefix,
        shared_bb_relpath=args.shared_bb,
        shared_defs=shared_defs,
        inline_class_names=set(_split_csv(args.inline)),
        inline_or_ref_class_names=set(_split_csv(args.inline_or_ref)),
        reference_class_names=set(_split_csv(args.reference)),
        exclude_class_names=set(_split_csv(args.exclude_class)),
        exclude_property_specs=set(_split_csv(args.exclude_property)),
        inline_datatypes=args.inline_datatypes,
        strict_required=args.strict_required,
        external_class_refs=external_class_refs,
        local_defs=OrderedDict(),
        expanding=set(),
        xsd_formats=args.xsd_formats,
        iri_reference_type=args.iri_reference_type,
        id_reference_type=args.id_reference_type,
        comment_directives=args.comment_directives,
        verbatim_docs=args.verbatim_docs,
        bb_out_dir=bb_out_dir,
        sources_dir=args.sources_dir,
        root_bb_path=element_bb_path(classes[0].id) if args.linked else None,
    )

    # Title and description
    if args.title:
        title = args.title
    elif args.comment_directives:
        # The title the model records, or none.
        title = split_comment_directives(classes[0].doc)[1].get("title")
    else:
        title = " ".join(_humanize(args.bb_name).split())
    if args.description:
        description = args.description
    else:
        description = _doc_text(ctx, classes[0].doc)

    # ----- schema emit (skip when --uml-only)
    if not args.uml_only:
        schema = build_root_schema(classes, ctx, title, description)
        schema_path = bb_out_dir / "schema.yaml"
        emit_yaml(schema, schema_path)
        print(f"Wrote {schema_path}", file=sys.stderr)

        if not args.schema_only:
            abstract = (description or "").strip().replace("\n\n", " ")
            if not abstract:
                abstract = f"Building block for {', '.join(class_names)}."
            emit_companions(
                out_dir=bb_out_dir,
                bb_name=args.bb_name,
                title=title,
                abstract=abstract,
                class_names=class_names,
                prefix=args.prefix,
                prefix_iri=args.prefix_iri,
                today=_today_iso(),
            )
            print(f"Wrote bblock.json, context.jsonld, rules.shacl, examples.yaml",
                  file=sys.stderr)

    # ----- UML emit
    if args.emit_uml:
        if args.uml_only and not args.emit_uml:
            print("ERROR: --uml-only requires --emit-uml PATH", file=sys.stderr)
            return 2

        acronym = args.uml_acronym
        if not acronym:
            # Strip 'ddicdi' prefix if present; lower-first-letter for the rest.
            stripped = args.bb_name
            if stripped.lower().startswith("ddicdi") and len(stripped) > 6:
                stripped = stripped[6:]
            acronym = stripped[:1].lower() + stripped[1:] if stripped else args.bb_name

        profile_uri = args.uml_profile_uri or args.prefix_iri
        model_name = args.uml_model_name or args.bb_name
        main_pkg = args.uml_main_package or model_name
        classes_pkg = args.uml_classes_package if args.uml_classes_package != "" else None
        model_def = args.uml_model_definition or (description or "")

        emit_uml(
            args.emit_uml,
            acronym=acronym,
            profile_uri=profile_uri,
            model_name=model_name,
            model_definition=model_def,
            main_package_name=main_pkg,
            main_package_definition=args.uml_main_package_definition or "",
            classes_package_name=classes_pkg,
            classes_package_definition=args.uml_classes_package_definition or "",
            roots=classes,
            ctx=ctx,
            model=model,
        )
        print(f"Wrote {args.emit_uml}", file=sys.stderr)

    return 0


def _humanize(name: str) -> str:
    """ddicdiValueDomain → DDI-CDI Value Domain."""
    if name.lower().startswith("ddicdi"):
        rest = name[6:]
        return "DDI-CDI " + re.sub(r"(?<=[a-z])(?=[A-Z])", " ", rest)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


# ---------------------------------------------------------------------------
# Eclipse UML2 (XMI 2.5) emit
#
# Produces a profile UML file in the same canonical-XMI shape as the existing
# cdif-ddsc / cdif-codelist hand-built UMLs. Reuses the parsed Model and the
# BuildContext's inline/reference/exclude flags so the same --class / --inline
# args that drive schema emit also drive UML emit.
# ---------------------------------------------------------------------------

UML2_NS = "http://www.eclipse.org/uml2/5.0.0/UML"
UML2_XMI_NS = "http://www.omg.org/spec/XMI/20131001"
STANDARD_PROFILE_NS = "http://www.eclipse.org/uml2/5.0.0/UML/Profile/Standard"
PRIMITIVE_HREF_BASE = "http://www.eclipse.org/uml2/5.0.0/UML/PrimitiveTypes.xmi"

# Reverse of PRIMITIVE_FRAGMENTS — JSON-schema-type back to the UML primitive
# local name used in the href fragment.
PRIMITIVE_LOCAL = {
    "string": "String",
    "integer": "Integer",
    "boolean": "Boolean",
    "number": "Real",
}


@dataclass
class UmlClosure:
    """Result of walking the type/class closure from a set of root classes."""
    classes: list[UmlClass] = field(default_factory=list)        # in include order
    datatypes: list[UmlClass] = field(default_factory=list)      # in discovery order
    enumerations: list[UmlClass] = field(default_factory=list)
    class_ids: set[str] = field(default_factory=set)             # ids of included classes
    type_ids: set[str] = field(default_factory=set)              # ids of included datatypes/enums
    # association_id -> (subject_cls_id, object_cls_id, role_name, lower, upper, aggregation)
    associations: list[tuple] = field(default_factory=list)


def compute_uml_closure(roots: list[UmlClass], ctx: BuildContext,
                        model: Model,
                        datatype_substitutions: Optional[dict[str, str]] = None,
                        exclude_datatypes: Optional[set[str]] = None,
                        target_classes_by_name: Optional[dict[str, UmlClass]] = None) -> UmlClosure:
    """Walk the closure from `roots` adding referenced DataTypes/Enumerations
    and (when in ctx.inline_class_names) referenced Classes. Also gathers
    association edges between included classes so they can be emitted as
    UML Associations.

    v1.1 transitive datatype policy:
      * datatype_substitutions: when a property's type points at a source
        DataType named in the map, the closure rebinds the reference to the
        substitution target (a local target class or a UML primitive) before
        deciding whether to pull the original into the closure. This prevents
        excluded/substituted source DataTypes from being pulled in transitively
        via attributes of other DataTypes (e.g. source Identifier.versionRationale
        typed RationaleDefinition).
      * exclude_datatypes: source DataType names to skip entirely. A property
        whose type points at an excluded DataType is not followed (the DataType
        does not appear in the closure, and is not emitted).
    """
    out = UmlClosure()
    out.classes.extend(roots)
    out.class_ids.update(c.id for c in roots)
    # Pull policy from ctx if not explicitly overridden
    subs = datatype_substitutions or getattr(ctx, "datatype_substitutions", None) or {}
    excludes = exclude_datatypes or getattr(ctx, "exclude_datatypes", None) or set()
    targets_by_name = target_classes_by_name or getattr(ctx, "target_classes_by_name", None) or {}
    # Property pairs we've already considered as association edges (avoid duplicates)
    seen_assocs: set[tuple] = set()
    # FIFO queue of classes whose properties still need walking
    queue: list[UmlClass] = list(roots)
    while queue:
        cls = queue.pop(0)
        # Pull the full property list including inherited (matches schema emit)
        props = collect_inherited_properties(cls.id, model)
        for prop in props:
            tid = prop.type_id
            if not tid:
                continue
            target = model.elements.get(tid)
            if target is None:
                continue
            if target.name in ctx.exclude_class_names:
                continue
            # v1.1: skip excluded source DataTypes entirely (don't pull them
            # or anything they transitively reference).
            if target.name in excludes:
                continue
            # v1.1: rebind substituted source DataType references to the
            # substitution target before deciding what to pull. If the
            # substitution maps to a UML primitive name we just skip the pull
            # (primitives are referenced by href to Eclipse's PrimitiveTypes.xmi
            # on the attribute itself, not emitted as local packagedElements).
            if target.name in subs:
                repl = subs[target.name]
                if repl in {"String", "Integer", "Boolean", "Real"}:
                    continue
                repl_cls = targets_by_name.get(repl)
                if repl_cls is not None:
                    target = repl_cls
                    tid = repl_cls.id
            if target.kind in ("datatype", "enumeration"):
                if tid not in out.type_ids:
                    out.type_ids.add(tid)
                    (out.datatypes if target.kind == "datatype" else out.enumerations).append(target)
                    # Walk datatype attributes for further types
                    queue.append(target)
            elif target.kind == "class":
                # Decide whether to inline the target class as well.
                want_inline = target.name in ctx.inline_class_names
                # Treat associations to non-root classes as id-references unless
                # explicitly inlined. (Same conservative default as schema emit.)
                if want_inline and tid not in out.class_ids:
                    out.class_ids.add(tid)
                    out.classes.append(target)
                    queue.append(target)
                # If both ends are now included AND this is an assoc end, record
                # the edge so we can emit a UML Association element later.
                if prop.is_assoc_end and tid in out.class_ids:
                    key = (cls.id, tid, prop.name)
                    if key not in seen_assocs:
                        seen_assocs.add(key)
                        out.associations.append((
                            prop.id,        # property id (= UCMIS owner_role_target tail)
                            cls.id, tid,
                            prop.name,
                            prop.lower, prop.upper,
                            prop.aggregation,
                        ))
    return out


def _xml_escape_attr(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _xml_escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _XmiWriter:
    """Small hand-rolled XMI 2.5 writer. Produces output close in style to the
    existing CDIF canonical-XMI files (children-then-name ordering, no
    self-closing on empty elements that have explicit closing in the source)."""

    def __init__(self):
        self.lines: list[str] = []
        self.indent_str = "   "  # match existing canonical files (3 spaces)
        self.depth = 0

    def _open(self, qname: str, attrs: dict, self_close: bool = False) -> None:
        attr_str = "".join(f' {k}="{_xml_escape_attr(v)}"'
                           for k, v in attrs.items() if v is not None)
        sep = "" if not attr_str else ""
        self.lines.append(f"{self.indent_str * self.depth}<{qname}{sep}{attr_str}{' /' if self_close else ''}>")
        if not self_close:
            self.depth += 1

    def _close(self, qname: str) -> None:
        self.depth -= 1
        self.lines.append(f"{self.indent_str * self.depth}</{qname}>")

    def _leaf(self, qname: str, text: str = "", attrs: Optional[dict] = None) -> None:
        attr_str = ""
        if attrs:
            attr_str = "".join(f' {k}="{_xml_escape_attr(v)}"'
                               for k, v in attrs.items() if v is not None)
        if text:
            self.lines.append(
                f"{self.indent_str * self.depth}<{qname}{attr_str}>"
                f"{_xml_escape_text(text)}</{qname}>"
            )
        else:
            self.lines.append(f"{self.indent_str * self.depth}<{qname}{attr_str}/>")

    def output(self) -> str:
        return "\n".join(self.lines) + "\n"


def _profile_xmi_id(acronym: str, *parts: str) -> str:
    """Stable xmi:id for a profile element. e.g. ('ddsc','InstanceVariable')
    → 'ddsc.InstanceVariable'."""
    return ".".join((acronym, *parts))


def _profile_uuid(profile_uri: str, *parts: str) -> str:
    """xmi:uuid for a profile element, namespaced under the profile URI."""
    return f"{profile_uri}#" + "-".join(parts) if parts else profile_uri


def _emit_owned_comment(w: _XmiWriter, owner_id: str, owner_uuid: str,
                        body: str) -> None:
    """Emit an <ownedComment> child of the current open element."""
    if not body:
        return
    w._open("ownedComment", {
        "xmi:id": f"{owner_id}-ownedComment",
        "xmi:uuid": f"{owner_uuid}-ownedComment",
        "xmi:type": "uml:Comment",
    })
    w._leaf("annotatedElement", attrs={"xmi:idref": owner_id})
    w._leaf("body", text=body)
    w._close("ownedComment")


def _emit_value(w: _XmiWriter, tag: str, prop_id: str, prop_uuid: str,
                xmi_type: str, value: str) -> None:
    w._open(tag, {
        "xmi:id": f"{prop_id}-{tag}",
        "xmi:uuid": f"{prop_uuid}-{tag}",
        "xmi:type": xmi_type,
    })
    w._leaf("value", text=value)
    w._close(tag)


def _emit_model_identification(w: _XmiWriter, acronym: str, profile_uri: str,
                               fields: dict) -> None:
    """Emit a model-level <packagedElement uml:DataType> named
    'ModelIdentification' carrying constant, read-only identification metadata
    for the profile model (prefix, version, title, language, uri). Mirrors
    DDI-CDI's own ModelIdentification pattern so the model self-describes."""
    dt_id = _profile_xmi_id(acronym, "ModelIdentification")
    dt_uuid = f"{profile_uri}#ModelIdentification"
    w._open("packagedElement", {
        "xmi:id": dt_id, "xmi:uuid": dt_uuid, "xmi:type": "uml:DataType",
    })
    _emit_owned_comment(
        w, dt_id, dt_uuid,
        "Identification metadata for this profile model: constant, read-only "
        "attributes carrying the model's prefix, version, title, language and URI.")
    w._leaf("name", text="ModelIdentification")
    # (attr-name, value, primitive-local-name, doc) in emission order
    spec = [
        ("prefix",       fields.get("prefix") or "cdif",   "String",  "Namespace prefix of the model."),
        ("majorVersion", fields.get("majorVersion"),       "Integer", "Major version number of the model."),
        ("minorVersion", fields.get("minorVersion"),       "Integer", "Minor version number of the model."),
        ("title",        fields.get("title"),              "String",  "Title of the model."),
        ("language",     fields.get("language"),           "String",  "Primary language of the model."),
        ("uri",          fields.get("uri") or profile_uri, "String",  "Namespace URI of the model."),
    ]
    for name, value, local, doc in spec:
        if value is None or value == "":
            continue
        a_id = f"{dt_id}-{name}"
        a_uuid = f"{dt_uuid}-{name}"
        w._open("ownedAttribute", {
            "xmi:id": a_id, "xmi:uuid": a_uuid, "xmi:type": "uml:Property",
        })
        _emit_owned_comment(w, a_id, a_uuid, doc)
        _emit_value(w, "lowerValue", a_id, a_uuid, "uml:LiteralInteger", "1")
        _emit_value(w, "upperValue", a_id, a_uuid, "uml:LiteralInteger", "1")
        w._leaf("name", text=name)
        lit_type = "uml:LiteralInteger" if local == "Integer" else "uml:LiteralString"
        _emit_value(w, "defaultValue", a_id, a_uuid, lit_type, str(value))
        w._leaf("isReadOnly", text="true")
        w._leaf("type", attrs={
            "xmi:type": "uml:PrimitiveType",
            "href": f"{PRIMITIVE_HREF_BASE}#{local}",
        })
        w._close("ownedAttribute")
    w._close("packagedElement")


def _emit_property(w: _XmiWriter, owner_xmi_id: str, owner_uuid: str,
                   prop: Property, closure: UmlClosure, model: Model,
                   acronym: str, profile_uri: str,
                   association_xmi_id: Optional[str] = None,
                   prop_xmi_id: Optional[str] = None) -> None:
    """Emit an <ownedAttribute xmi:type='uml:Property'> on the open class.

    `prop_xmi_id` overrides the default `{owner}-{name}` id — used when a
    class has two properties with the same local name (typically an
    attribute and an association-end whose role happens to match, e.g.
    ConceptScheme has both an `identifier` XsdAnyUri attribute for @id
    and an `identifier` association-end to Identifier for schema:identifier
    PropertyValue). Both would otherwise get the same xmi:id and violate
    XMI 2.5 uniqueness."""
    if prop_xmi_id:
        prop_id = prop_xmi_id
        # keep the uuid distinct too, deriving from the same suffix
        suffix = prop_id[len(owner_xmi_id) + 1:] if prop_id.startswith(f"{owner_xmi_id}-") else prop.name
        prop_uuid = f"{owner_uuid}-{suffix}"
    else:
        prop_id = f"{owner_xmi_id}-{prop.name}"
        prop_uuid = f"{owner_uuid}-{prop.name}"
    w._open("ownedAttribute", {
        "xmi:id": prop_id,
        "xmi:uuid": prop_uuid,
        "xmi:type": "uml:Property",
        "aggregation": prop.aggregation if prop.aggregation else None,
    })
    if prop.doc:
        _emit_owned_comment(w, prop_id, prop_uuid, clean_definition(prop.doc) or "")
    # lower / upper values
    lower_val = "*" if prop.lower == -1 else str(prop.lower)
    upper_val = "*" if prop.upper == -1 else str(prop.upper)
    lower_type = "uml:LiteralUnlimitedNatural" if prop.lower == -1 else "uml:LiteralInteger"
    upper_type = "uml:LiteralUnlimitedNatural" if prop.upper == -1 else "uml:LiteralInteger"
    _emit_value(w, "lowerValue", prop_id, prop_uuid, lower_type, lower_val)
    _emit_value(w, "upperValue", prop_id, prop_uuid, upper_type, upper_val)
    w._leaf("name", text=prop.name)
    # type
    if prop.primitive is not None:
        local = PRIMITIVE_LOCAL.get(prop.primitive, "String")
        # Eclipse UML2 canonical form: reference the standard PrimitiveTypes
        # library by href rather than a profile-local PrimitiveType stub.
        w._leaf("type", attrs={
            "xmi:type": "uml:PrimitiveType",
            "href": f"{PRIMITIVE_HREF_BASE}#{local}",
        })
    elif prop.type_id:
        # Resolve the target's xmi:id within the profile if it's in the closure;
        # otherwise emit the source-model id directly (consumer can resolve via
        # the source model load).
        target = model.elements.get(prop.type_id)
        if target and (prop.type_id in closure.class_ids or prop.type_id in closure.type_ids):
            tgt_xmi = _profile_xmi_id(acronym, target.name)
            w._leaf("type", attrs={"xmi:idref": tgt_xmi})
        else:
            w._leaf("type", attrs={"xmi:idref": prop.type_id})
    if association_xmi_id:
        w._leaf("association", attrs={"xmi:idref": association_xmi_id})
    w._close("ownedAttribute")


def _emit_class(w: _XmiWriter, cls: UmlClass, closure: UmlClosure, model: Model,
                acronym: str, profile_uri: str,
                assoc_for_prop: dict[str, str],
                prop_xmi_for_prop: Optional[dict[tuple[str, str], str]] = None) -> None:
    xmi_id = _profile_xmi_id(acronym, cls.name)
    uuid = f"{profile_uri}#{cls.name}"
    w._open("packagedElement", {
        "xmi:id": xmi_id,
        "xmi:uuid": uuid,
        "xmi:type": "uml:DataType" if cls.is_datatype else "uml:Class",
        # DataTypes in this model are concrete value types; only classes carry isAbstract.
        "isAbstract": "true" if (cls.is_abstract and not cls.is_datatype) else None,
    })
    if cls.doc:
        _emit_owned_comment(w, xmi_id, uuid, clean_definition(cls.doc) or "")
    w._leaf("name", text=cls.name)
    # Generalizations (one <generalization> per parent id)
    for parent_id in cls.parents:
        parent = model.elements.get(parent_id)
        if parent is None:
            continue
        gen_xmi_id = f"{xmi_id}-generalization-{parent.name}"
        gen_uuid = f"{uuid}-generalization-{parent.name}"
        w._open("generalization", {
            "xmi:id": gen_xmi_id,
            "xmi:uuid": gen_uuid,
            "xmi:type": "uml:Generalization",
        })
        w._leaf("general", attrs={"xmi:idref": parent_id})
        w._close("generalization")
    # Inherited properties (matches schema-emit behavior)
    for prop in collect_inherited_properties(cls.id, model):
        if not prop.name:
            continue
        # Skip props whose type is excluded or unresolvable to a known target.
        if prop.type_id:
            tgt = model.elements.get(prop.type_id)
            if tgt is None:
                continue
            if tgt.name in [n for n in ()]:  # placeholder for future exclude
                continue
            # If this is an association-end and the target isn't included,
            # skip the property (alternative: emit as id-ref; conservative for now)
            if prop.is_assoc_end and prop.type_id not in closure.class_ids:
                continue
        association_xmi_id = assoc_for_prop.get(prop.id)
        prop_xmi_id = prop_xmi_for_prop.get((xmi_id, prop.id)) if prop_xmi_for_prop else None
        _emit_property(w, xmi_id, uuid, prop, closure, model, acronym,
                       profile_uri, association_xmi_id=association_xmi_id,
                       prop_xmi_id=prop_xmi_id)
    w._close("packagedElement")


def _emit_datatype(w: _XmiWriter, dt: UmlClass, closure: UmlClosure,
                   model: Model, acronym: str, profile_uri: str) -> None:
    xmi_id = _profile_xmi_id(acronym, dt.name)
    uuid = f"{profile_uri}#{dt.name}"
    w._open("packagedElement", {
        "xmi:id": xmi_id,
        "xmi:uuid": uuid,
        "xmi:type": "uml:DataType",
    })
    if dt.doc:
        _emit_owned_comment(w, xmi_id, uuid, clean_definition(dt.doc) or "")
    w._leaf("name", text=dt.name)
    for prop in dt.properties:
        if not prop.name:
            continue
        _emit_property(w, xmi_id, uuid, prop, closure, model, acronym,
                       profile_uri, association_xmi_id=None)
    w._close("packagedElement")


def _emit_enumeration(w: _XmiWriter, en: UmlClass, acronym: str,
                      profile_uri: str) -> None:
    xmi_id = _profile_xmi_id(acronym, en.name)
    uuid = f"{profile_uri}#{en.name}"
    w._open("packagedElement", {
        "xmi:id": xmi_id,
        "xmi:uuid": uuid,
        "xmi:type": "uml:Enumeration",
    })
    if en.doc:
        _emit_owned_comment(w, xmi_id, uuid, clean_definition(en.doc) or "")
    w._leaf("name", text=en.name)
    for lit in en.literals:
        lit_id = f"{xmi_id}-{lit}"
        w._open("ownedLiteral", {
            "xmi:id": lit_id,
            "xmi:uuid": f"{uuid}-{lit}",
            "xmi:type": "uml:EnumerationLiteral",
        })
        w._leaf("name", text=lit)
        w._close("ownedLiteral")
    w._close("packagedElement")


def _emit_association(w: _XmiWriter, assoc_entry: tuple, model: Model,
                      acronym: str, profile_uri: str,
                      prop_xmi_for_prop: Optional[dict[tuple[str, str], str]] = None) -> str:
    """Emit a top-level <packagedElement xmi:type='uml:Association'>. Returns
    its xmi:id so the corresponding property's <association xmi:idref=...> can
    point at it. `prop_xmi_for_prop` provides the subject-end property's
    disambiguated xmi:id when it collides with a same-named attribute on
    the subject class."""
    prop_id, subj_id, obj_id, role, lower, upper, aggregation = assoc_entry
    subj = model.elements[subj_id]
    obj = model.elements[obj_id]
    name = f"{subj.name}_{role}_{obj.name}"
    assoc_xmi = _profile_xmi_id(acronym, name)
    assoc_uuid = f"{profile_uri}#{name}"
    subj_prop_xmi = None
    if prop_xmi_for_prop:
        subj_owner_id = _profile_xmi_id(acronym, subj.name)
        subj_prop_xmi = prop_xmi_for_prop.get((subj_owner_id, prop_id))
    if not subj_prop_xmi:
        subj_prop_xmi = _profile_xmi_id(acronym, subj.name) + f"-{role}"
    w._open("packagedElement", {
        "xmi:id": assoc_xmi,
        "xmi:uuid": assoc_uuid,
        "xmi:type": "uml:Association",
    })
    w._leaf("name", text=name)
    # memberEnd points at the owned-attribute (subject side); the object side
    # is an ownedEnd if not navigable. For UCMIS-style we emit both.
    w._leaf("memberEnd", attrs={"xmi:idref": subj_prop_xmi})
    # Object-end as an ownedEnd Property
    owned_end_id = f"{assoc_xmi}-{obj.name}End"
    owned_end_uuid = f"{assoc_uuid}-{obj.name}End"
    w._open("ownedEnd", {
        "xmi:id": owned_end_id,
        "xmi:uuid": owned_end_uuid,
        "xmi:type": "uml:Property",
    })
    # No <name> on the ownedEnd — it is the non-navigable back-reference. The
    # navigable role name (e.g. "publisher", "creator") lives on the owner's
    # ownedAttribute end. Emitting a name like "person" here previously caused
    # EA to label the source-side end with the target class's lowercased name,
    # which was misleading (e.g. Dataset shown with role "person").
    w._leaf("type", attrs={"xmi:idref": _profile_xmi_id(acronym, obj.name)})
    w._leaf("association", attrs={"xmi:idref": assoc_xmi})
    w._close("ownedEnd")
    w._leaf("memberEnd", attrs={"xmi:idref": owned_end_id})
    w._close("packagedElement")
    return assoc_xmi


def emit_uml(out_path: Path, *,
             acronym: str, profile_uri: str, model_name: str,
             model_definition: str, main_package_name: str,
             main_package_definition: str,
             classes_package_name: Optional[str],
             classes_package_definition: Optional[str],
             roots: list[UmlClass], ctx: BuildContext, model: Model,
             model_identification: Optional[dict] = None) -> None:
    """Write an Eclipse UML2 (XMI 2.5) profile model to out_path.

    Layout:
        <uml:Model name=model_name uri=profile_uri>
          <packagedElement uml:Package name=main_package_name>
            <packagedElement uml:DataType .../>   # transitively-needed DataTypes
            <packagedElement uml:Enumeration .../># used Enums
            <packagedElement uml:Package name=classes_package_name>
              <packagedElement uml:Class .../>    # root + inlined classes
            </packagedElement>
            <packagedElement uml:Association .../># for assoc edges between included classes
          </packagedElement>
        </uml:Model>
    """
    closure = compute_uml_closure(roots, ctx, model)

    # Build a map: Property.id -> Association xmi:id, so each owned-attribute
    # association-end can carry the matching <association xmi:idref=...>.
    # We need this BEFORE emitting classes, so do a dry-run on associations.
    assoc_xmi_for_prop: dict[str, str] = {}
    for entry in closure.associations:
        prop_id, subj_id, obj_id, role, _, _, _ = entry
        subj = model.elements[subj_id]
        obj = model.elements[obj_id]
        assoc_xmi_for_prop[prop_id] = _profile_xmi_id(
            acronym, f"{subj.name}_{role}_{obj.name}"
        )

    # Build a map: (owner_xmi_id, Property.id) -> owned-attribute xmi:id,
    # disambiguating any same-named-on-same-class collision (typically a data
    # attribute vs an association-end sharing the local name — e.g.
    # ConceptScheme has both an `identifier` XsdAnyUri attribute for @id AND
    # an `identifier` association to Identifier for schema:identifier).
    # Without this, both get the same xmi:id and violate XMI 2.5 uniqueness.
    # First occurrence keeps the base `{owner}-{name}` id; subsequent
    # occurrences get `-{TargetClass}` suffixed.
    #
    # Keyed on (owner_xmi_id, prop.id) — not prop.id alone — because inherited
    # properties share the SAME prop.id across every subclass that emits them
    # (Agent's `identifier` prop.id is reused by Person and Organization), and
    # each subclass owns its own <ownedAttribute> under its own owner-prefixed
    # xmi:id.
    prop_xmi_for_prop: dict[tuple[str, str], str] = {}
    for cls in closure.classes:
        owner_id = _profile_xmi_id(acronym, cls.name)
        used_ids: set[str] = set()
        for prop in collect_inherited_properties(cls.id, model):
            if not prop.name:
                continue
            base = f"{owner_id}-{prop.name}"
            candidate = base
            if candidate in used_ids:
                target = model.elements.get(prop.type_id) if prop.type_id else None
                if target and target.name:
                    candidate = f"{base}-{target.name}"
                # last-resort dedup if the disambiguated form still collides
                n = 2
                while candidate in used_ids:
                    candidate = f"{base}-{n}"
                    n += 1
            used_ids.add(candidate)
            prop_xmi_for_prop[(owner_id, prop.id)] = candidate

    w = _XmiWriter()
    w.lines.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    w._open("xmi:XMI", {
        "xmlns:StandardProfile": STANDARD_PROFILE_NS,
        "xmlns:uml": UML2_NS,
        "xmlns:xmi": UML2_XMI_NS,
    })

    model_xmi_id = _profile_xmi_id(acronym, model_name)
    model_uuid = f"{profile_uri}#{model_name}"
    w._open("uml:Model", {
        "xmi:id": model_xmi_id,
        "xmi:uuid": model_uuid,
    })
    if model_definition:
        _emit_owned_comment(w, model_xmi_id, model_uuid, model_definition)
    w._leaf("name", text=model_name)
    if profile_uri:
        w._leaf("URI", text=profile_uri)

    # Model self-identification metadata (DataType sibling of the main package)
    if model_identification:
        _emit_model_identification(w, acronym, profile_uri, model_identification)

    # Main package - suffix with 'Package' if it would collide with the model id
    mp_id = _profile_xmi_id(acronym, main_package_name)
    if mp_id == model_xmi_id:
        mp_id = _profile_xmi_id(acronym, main_package_name + "Package")
    mp_uuid = f"{profile_uri}#{main_package_name}-package"
    w._open("packagedElement", {
        "xmi:id": mp_id,
        "xmi:uuid": mp_uuid,
        "xmi:type": "uml:Package",
    })
    if main_package_definition:
        _emit_owned_comment(w, mp_id, mp_uuid, main_package_definition)
    w._leaf("name", text=main_package_name)

    # DataTypes & Enumerations directly under the main package
    for dt in closure.datatypes:
        _emit_datatype(w, dt, closure, model, acronym, profile_uri)
    for en in closure.enumerations:
        _emit_enumeration(w, en, acronym, profile_uri)

    # Classes sub-package (optional)
    if classes_package_name:
        cp_id = _profile_xmi_id(acronym, classes_package_name)
        if cp_id in (model_xmi_id, mp_id):
            cp_id = _profile_xmi_id(acronym, classes_package_name + "Package")
        cp_uuid = f"{profile_uri}#{classes_package_name}-package"
        w._open("packagedElement", {
            "xmi:id": cp_id,
            "xmi:uuid": cp_uuid,
            "xmi:type": "uml:Package",
        })
        if classes_package_definition:
            _emit_owned_comment(w, cp_id, cp_uuid, classes_package_definition)
        w._leaf("name", text=classes_package_name)
        for cls in closure.classes:
            _emit_class(w, cls, closure, model, acronym, profile_uri,
                        assoc_xmi_for_prop, prop_xmi_for_prop=prop_xmi_for_prop)
        w._close("packagedElement")  # /classes_package
    else:
        for cls in closure.classes:
            _emit_class(w, cls, closure, model, acronym, profile_uri,
                        assoc_xmi_for_prop, prop_xmi_for_prop=prop_xmi_for_prop)

    # Associations live at the main-package level
    for entry in closure.associations:
        _emit_association(w, entry, model, acronym, profile_uri,
                          prop_xmi_for_prop=prop_xmi_for_prop)

    w._close("packagedElement")  # /main_package
    w._close("uml:Model")
    w._close("xmi:XMI")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(w.output(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Enterprise Architect XMI 1.1 emit
#
# Produces the EA-native export format (xmi.version="1.1", xmlns:UML="omg.org/
# UML1.3") for direct import into Enterprise Architect. Layout matches the
# DDI-CDI source's shape:
#
#   <XMI xmi.version="1.1" xmlns:UML="omg.org/UML1.3">
#     <XMI.header><XMI.documentation>...</XMI.documentation></XMI.header>
#     <XMI.content>
#       <UML:Model name=... xmi.id=MX_...>
#         <UML:Namespace.ownedElement>
#           <UML:Class name="EARootClass" xmi.id=EAID_111...AA00 isRoot=true.../>
#           <UML:Package name=... xmi.id=EAPK_...>
#             <UML:ModelElement.taggedValue>... ea_stype=Package ...</.taggedValue>
#             <UML:Namespace.ownedElement>
#               <UML:Class name=X ... ea_stype=Class>
#                 <UML:Classifier.feature>
#                   <UML:Attribute>...</UML:Attribute>
#                 </UML:Classifier.feature>
#               </UML:Class>
#               <UML:Class name=Y ... ea_stype=DataType>...</UML:Class>
#               <UML:Class name=Z ... ea_stype=enumeration>
#                 <UML:ModelElement.stereotype><UML:Stereotype name=enumeration/></...>
#                 ...
#               </UML:Class>
#             </UML:Namespace.ownedElement>
#           </UML:Package>
#           <!-- Primitive type stubs at model root -->
#           <UML:Class name="String" xmi.id=eaxmiid_string .../>
#           ...
#           <!-- Top-level relationships -->
#           <UML:Generalization subtype=EAID_... supertype=EAID_... xmi.id=EAID_.../>
#           <UML:Association name=role xmi.id=EAID_...>
#             <UML:Association.connection>
#               <UML:AssociationEnd type=EAID_subject isNavigable=false/>
#               <UML:AssociationEnd type=EAID_object isNavigable=true/>
#             </UML:Association.connection>
#           </UML:Association>
#         </UML:Namespace.ownedElement>
#       </UML:Model>
#     </XMI.content>
#   </XMI>
# ---------------------------------------------------------------------------

import hashlib as _hashlib


def _ea_id(prefix: str, *parts: str) -> str:
    """Deterministic EA-style id: prefix + UUID-shaped UPPERCASE hex derived
    from parts. Format: PREFIX_XXXXXXXX_XXXX_4XXX_YXXX_XXXXXXXXXXXX. EA's own
    exports use uppercase hex throughout (except the version-4 nibble and the
    8/9/a/b variant nibble in the 4th block which can be lowercase). Matching
    the source convention helps EA's importer recognise the ids."""
    h = _hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()
    return (f"{prefix}_{h[0:8].upper()}_{h[8:12].upper()}_4{h[13:16].lower()}_"
            f"8{h[17:20].upper()}_{h[20:32].upper()}")


def _ea_guid(*parts: str) -> str:
    """Brace-wrapped GUID-shaped string in EA's tagged-value style.
    e.g. {3EC60D5B-CB93-4b04-BCC0-0D4977E8AFC4} — mostly uppercase, version-4
    nibble lowercase."""
    h = _hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()
    return (f"{{{h[0:8].upper()}-{h[8:12].upper()}-4{h[13:16].lower()}-"
            f"8{h[17:20].upper()}-{h[20:32].upper()}}}")


# Stable, well-known ids for EA-side primitive type stubs.
EA_PRIMITIVE_IDS = {
    "string": "eaxmiid_String",
    "integer": "eaxmiid_Integer",
    "boolean": "eaxmiid_Boolean",
    "number": "eaxmiid_Real",
}

EA_PRIMITIVE_LOCAL = {
    "string": "String",
    "integer": "Integer",
    "boolean": "Boolean",
    "number": "Real",
}


class _EaXmiWriter(_XmiWriter):
    """EA XMI 1.1 writer. Reuses _XmiWriter's indentation/escape helpers."""
    def __init__(self):
        super().__init__()
        # EA's source uses tabs; we'll use tabs too for visual similarity
        self.indent_str = "\t"


def _ea_emit_tagged_values(w: _EaXmiWriter, items: list[tuple[str, str]]) -> None:
    if not items:
        return
    w._open("UML:ModelElement.taggedValue", {})
    for tag, value in items:
        if value is None:
            continue
        w._leaf("UML:TaggedValue", attrs={"tag": tag, "value": value})
    w._close("UML:ModelElement.taggedValue")


def _ea_emit_attribute(w: _EaXmiWriter, prop: Property,
                       closure: UmlClosure, model: Model,
                       acronym: str, position: int = 0) -> None:
    """Emit <UML:Attribute> for a non-association Property."""
    # Determine type
    type_label = None
    type_id_ref = None
    if prop.primitive is not None:
        type_label = EA_PRIMITIVE_LOCAL.get(prop.primitive, "String")
        type_id_ref = EA_PRIMITIVE_IDS.get(prop.primitive, EA_PRIMITIVE_IDS["string"])
    elif prop.type_id:
        target = model.elements.get(prop.type_id)
        if target:
            type_label = target.name
            type_id_ref = _ea_id("EAID", "type", acronym, target.name)

    w._open("UML:Attribute", {
        "name": prop.name,
        "changeable": "none",
        "visibility": "public",
        "ownerScope": "instance",
        "targetScope": "instance",
    })
    # Initial value placeholder
    w._open("UML:Attribute.initialValue", {})
    w._leaf("UML:Expression")
    w._close("UML:Attribute.initialValue")
    # Type reference
    if type_id_ref:
        w._open("UML:StructuralFeature.type", {})
        w._leaf("UML:Classifier", attrs={"xmi.idref": type_id_ref})
        w._close("UML:StructuralFeature.type")
    # Tagged values — include the full set EA emits so import doesn't choke.
    tvs: list[tuple[str, str]] = []
    if prop.doc:
        tvs.append(("description", clean_definition(prop.doc) or ""))
    if type_label:
        tvs.append(("type", type_label))
    tvs.append(("derived", "0"))
    tvs.append(("ordered", "0"))
    tvs.append(("scale", "0"))
    tvs.append(("static", "0"))
    tvs.append(("collection", "true" if prop.upper == -1 or prop.upper > 1 else "false"))
    tvs.append(("position", str(position)))
    tvs.append(("lowerBound", "*" if prop.lower == -1 else str(prop.lower)))
    tvs.append(("upperBound", "*" if prop.upper == -1 else str(prop.upper)))
    tvs.append(("duplicates", "0"))
    tvs.append(("ea_guid", _ea_guid("attr", acronym, prop.id, prop.name)))
    tvs.append(("styleex", "IsLiteral=0;volatile=0;union=0;"))
    _ea_emit_tagged_values(w, tvs)
    w._close("UML:Attribute")


def _ea_emit_class_element(w: _EaXmiWriter, cls: UmlClass, package_id: str,
                            closure: UmlClosure, model: Model,
                            acronym: str) -> str:
    """Emit one <UML:Class> for a class, datatype, or enumeration. Returns
    its EAID so generalizations/associations can reference it."""
    eaid = _ea_id("EAID", acronym, cls.name)
    is_enum = cls.kind == "enumeration"
    ea_stype = {"class": "Class", "datatype": "DataType",
                "enumeration": "enumeration"}.get(cls.kind, "Class")
    w._open("UML:Class", {
        "name": cls.name,
        "xmi.id": eaid,
        "visibility": "public",
        "namespace": package_id,
        "isRoot": "false",
        "isLeaf": "false",
        "isAbstract": "true" if cls.is_abstract else "false",
        "isActive": "false",
    })
    if is_enum:
        # Stereotype <<enumeration>>
        w._open("UML:ModelElement.stereotype", {})
        w._leaf("UML:Stereotype", attrs={"name": "enumeration"})
        w._close("UML:ModelElement.stereotype")
    tvs: list[tuple[str, str]] = []
    if cls.doc:
        tvs.append(("documentation", clean_definition(cls.doc) or ""))
    tvs.append(("isSpecification", "false"))
    tvs.append(("ea_stype", ea_stype))
    tvs.append(("ea_ntype", "0"))
    tvs.append(("version", "1.0"))
    tvs.append(("package", package_id))
    tvs.append(("ea_guid", _ea_guid("class", acronym, cls.name)))
    _ea_emit_tagged_values(w, tvs)
    # Features (attributes or enum literals)
    feature_items = []
    if is_enum:
        feature_items = list(cls.literals)
    else:
        # Non-association properties become attributes
        feature_items = [p for p in cls.properties if not p.is_assoc_end and p.name]
    if feature_items:
        w._open("UML:Classifier.feature", {})
        if is_enum:
            for lit in feature_items:
                w._open("UML:Attribute", {
                    "name": lit, "changeable": "frozen",
                    "visibility": "public", "ownerScope": "classifier",
                    "targetScope": "instance",
                })
                w._open("UML:Attribute.initialValue", {})
                w._leaf("UML:Expression")
                w._close("UML:Attribute.initialValue")
                _ea_emit_tagged_values(w, [("ea_guid", _ea_guid("lit", acronym, cls.name, lit))])
                w._close("UML:Attribute")
        else:
            for idx, prop in enumerate(feature_items):
                _ea_emit_attribute(w, prop, closure, model, acronym,
                                   position=idx)
        w._close("UML:Classifier.feature")
    w._close("UML:Class")
    return eaid


def _ea_emit_primitive_stubs(w: _EaXmiWriter, used: set[str]) -> None:
    """Emit minimal <UML:Class> placeholders for the primitives referenced by
    any attribute, so the attribute type idrefs resolve."""
    for prim in sorted(used):
        local = EA_PRIMITIVE_LOCAL.get(prim, "String")
        w._open("UML:Class", {
            "name": local,
            "xmi.id": EA_PRIMITIVE_IDS[prim],
            "visibility": "public",
            "isRoot": "true", "isLeaf": "false", "isAbstract": "false",
        })
        _ea_emit_tagged_values(w, [
            ("isSpecification", "false"),
            ("ea_stype", "Primitive"),
            ("ea_ntype", "0"),
        ])
        w._close("UML:Class")


def _ea_emit_association(w: _EaXmiWriter, assoc_entry: tuple, model: Model,
                          acronym: str) -> None:
    prop_id, subj_id, obj_id, role, lower, upper, aggregation = assoc_entry
    subj = model.elements[subj_id]
    obj = model.elements[obj_id]
    triple = f"{subj.name}_{role}_{obj.name}"
    assoc_eaid = _ea_id("EAID", "assoc", acronym, triple)
    subj_eaid = _ea_id("EAID", acronym, subj.name)
    obj_eaid = _ea_id("EAID", acronym, obj.name)
    w._open("UML:Association", {
        "name": role,
        "xmi.id": assoc_eaid,
        "visibility": "public",
        "isRoot": "false", "isLeaf": "false", "isAbstract": "false",
    })
    tvs = [
        ("ea_type", "Association"),
        ("direction", "Source -> Destination"),
        ("ea_sourceName", subj.name),
        ("ea_targetName", obj.name),
        ("ea_sourceType", "Class"),
        ("ea_targetType", "Class"),
        ("ea_guid", _ea_guid("assoc", acronym, triple)),
    ]
    _ea_emit_tagged_values(w, tvs)
    w._open("UML:Association.connection", {})
    # Source end (non-navigable by UCMIS convention)
    w._open("UML:AssociationEnd", {
        "visibility": "public",
        "aggregation": "none",
        "isOrdered": "false",
        "targetScope": "instance",
        "changeable": "none",
        "isNavigable": "false",
        "type": subj_eaid,
    })
    _ea_emit_tagged_values(w, [("ea_end", "source")])
    w._close("UML:AssociationEnd")
    # Target end (navigable; carries multiplicity)
    target_mult = "*" if upper == -1 else str(upper)
    if lower != upper and lower != 0:
        target_mult = f"{lower}..{target_mult}"
    elif lower == 0 and upper == -1:
        target_mult = "0..*"
    elif lower == 0 and upper == 1:
        target_mult = "0..1"
    w._open("UML:AssociationEnd", {
        "visibility": "public",
        "aggregation": aggregation if aggregation else "none",
        "isOrdered": "false",
        "targetScope": "instance",
        "changeable": "none",
        "isNavigable": "true",
        "multiplicity": target_mult,
        "type": obj_eaid,
    })
    _ea_emit_tagged_values(w, [("ea_end", "target")])
    w._close("UML:AssociationEnd")
    w._close("UML:Association.connection")
    w._close("UML:Association")


def emit_ea_xmi(out_path: Path, *,
                acronym: str, profile_uri: str, model_name: str,
                model_definition: str, main_package_name: str,
                main_package_definition: str,
                roots: list[UmlClass], ctx: BuildContext, model: Model) -> None:
    """Write an Enterprise Architect XMI 1.1 export to out_path."""
    closure = compute_uml_closure(roots, ctx, model)
    # Collect primitives actually referenced
    used_primitives: set[str] = set()
    for cls in closure.classes:
        for prop in cls.properties:
            if prop.primitive:
                used_primitives.add(prop.primitive)
    for dt in closure.datatypes:
        for prop in dt.properties:
            if prop.primitive:
                used_primitives.add(prop.primitive)

    w = _EaXmiWriter()
    w.lines.append('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>')
    # XMI root
    from datetime import datetime as _dt, timezone as _tz
    timestamp = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
    w._open("XMI", {
        "xmi.version": "1.1",
        "xmlns:UML": "omg.org/UML1.3",
        "timestamp": timestamp,
    })
    # Header
    w._open("XMI.header", {})
    w._open("XMI.documentation", {})
    w._leaf("XMI.exporter", text="uml_to_schema.py (EA emitter)")
    w._leaf("XMI.exporterVersion", text="1.0")
    w._close("XMI.documentation")
    w._close("XMI.header")
    # Content
    w._open("XMI.content", {})
    model_eaid = _ea_id("MX_EAID", acronym, model_name)
    w._open("UML:Model", {"name": model_name, "xmi.id": model_eaid})
    w._open("UML:Namespace.ownedElement", {})
    # EARootClass placeholder (EA expects this)
    w._leaf("UML:Class", attrs={
        "name": "EARootClass",
        "xmi.id": "EAID_11111111_5487_4080_A7F4_41526CB0AA00",
        "isRoot": "true", "isLeaf": "false", "isAbstract": "false",
    })
    # Main package
    package_eaid = _ea_id("EAPK", acronym, main_package_name)
    w._open("UML:Package", {
        "name": main_package_name,
        "xmi.id": package_eaid,
        "isRoot": "false", "isLeaf": "false", "isAbstract": "false",
        "visibility": "public",
    })
    pkg_tvs = []
    if main_package_definition:
        pkg_tvs.append(("documentation", main_package_definition))
    pkg_tvs.extend([
        ("isSpecification", "false"),
        ("ea_stype", "Public"),
        ("ea_eleType", "package"),
        ("version", "1.0"),
        ("ea_guid", _ea_guid("pkg", acronym, main_package_name)),
    ])
    _ea_emit_tagged_values(w, pkg_tvs)
    w._open("UML:Namespace.ownedElement", {})
    # Emit DataTypes and Enumerations BEFORE Classes so the attribute type
    # idrefs that classes carry resolve forward when EA does a single-pass import.
    for dt in closure.datatypes:
        _ea_emit_class_element(w, dt, package_eaid, closure, model, acronym)
    for en in closure.enumerations:
        _ea_emit_class_element(w, en, package_eaid, closure, model, acronym)
    for cls in closure.classes:
        _ea_emit_class_element(w, cls, package_eaid, closure, model, acronym)
    w._close("UML:Namespace.ownedElement")  # package's namespace
    w._close("UML:Package")
    # Primitive type stubs (at model root)
    _ea_emit_primitive_stubs(w, used_primitives)
    # Associations at model root
    for entry in closure.associations:
        _ea_emit_association(w, entry, model, acronym)
    w._close("UML:Namespace.ownedElement")  # model's namespace
    w._close("UML:Model")
    w._close("XMI.content")
    w._close("XMI")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(w.output(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 2: drive UML emit from a ucmism2m JSON configuration
#
# The ucmism2m v1.1 schema (ucmism2m/configuration/ucmis_mapping_configuration.
# schema.v1.1.json) describes:
#   - target model metadata (acronym, URI, packages, version)
#   - target classes via mappingType = map | new | merge
#   - per-attribute renaming via fromSourceAttributeName (v1.1)
#   - target associations with optional sourceAssociationName (v1.1 allows
#     brand-new associations whose subjectClass + objectClass are required
#     when sourceAssociationName is absent)
#
# This block reads such a config and renders a synthetic Model containing the
# target classes (with renamed attributes and merged source attributes), then
# passes it through compute_uml_closure / emit_uml.
# ---------------------------------------------------------------------------

PROFILE_PRIMITIVE_NAMES = {"String", "Integer", "Boolean", "Real"}


def _resolve_dataType_ref(
    name: str,
    source_model: Model,
    target_classes_by_name: dict[str, UmlClass],
) -> tuple[Optional[str], Optional[str]]:
    """Given a config dataType string (e.g. 'String', 'InternationalString',
    'ControlledVocabularyEntry', or a target class name like 'Concept'),
    return (type_id, primitive) where exactly one is non-None. type_id is
    set when the name resolves to a source DataType/Enumeration or a sibling
    target class; primitive is set for UML primitives."""
    if name in PROFILE_PRIMITIVE_NAMES:
        # uml_to_schema's PRIMITIVE_FRAGMENTS maps "String"->"string" etc.;
        # we need the JSON-schema-side name (which is what Property.primitive
        # carries throughout the codebase).
        return None, PRIMITIVE_FRAGMENTS[name]
    # Sibling target class? (other classes in the same config)
    if name in target_classes_by_name:
        return target_classes_by_name[name].id, None
    # Source DataType / Enumeration / Class by name?
    ids = source_model.name_to_id.get(name, [])
    if ids:
        return ids[0], None
    return None, "string"  # unknown → string fallback


def _ucmis_lower_to_int(s: str) -> int:
    if s == "*":
        return -1
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _ucmis_upper_to_int(s: str) -> int:
    if s == "*":
        return -1
    try:
        return int(s)
    except (TypeError, ValueError):
        return 1


def _ucmis_multiplicity(mult: Optional[dict]) -> Optional[tuple[int, int]]:
    """Convert {'lower':'1','upper':'*'} to (1, -1). Returns None if absent."""
    if not mult:
        return None
    return _ucmis_lower_to_int(mult.get("lower", "1")), _ucmis_upper_to_int(mult.get("upper", "1"))


def _find_source_property(src_cls: UmlClass, attr_name: str,
                          model: Model) -> Optional[Property]:
    """Find a Property on a source class (including inherited) by attribute
    name. Returns None if not found."""
    for p in collect_inherited_properties(src_cls.id, model):
        if p.name == attr_name and not p.is_assoc_end:
            return p
    return None


def _render_map_class(
    class_spec: dict,
    source_model: Model,
    target_classes_by_name: dict[str, UmlClass],
    acronym: str,
) -> UmlClass:
    """Render a mappingType='map' class spec into a synthetic UmlClass."""
    src_name = class_spec["sourceClass"]
    src_ids = source_model.name_to_id.get(src_name, [])
    if not src_ids:
        raise SystemExit(f"map: source class '{src_name}' not found in source model")
    src_cls = source_model.elements[src_ids[0]]
    target_name = class_spec["targetClass"]
    tgt_id = _profile_xmi_id(acronym, target_name)

    # Pull source doc; allow override via definitionFrom (which for 'map' means
    # the same source class, but kept for symmetry with merge).
    doc_src = src_cls
    if "definitionFrom" in class_spec:
        dids = source_model.name_to_id.get(class_spec["definitionFrom"], [])
        if dids:
            doc_src = source_model.elements[dids[0]]

    # Build attribute list:
    #   - if config lists attributes: use ONLY those (matches DDSC example pattern
    #     where each map spells out the attributes it wants to carry forward)
    #   - else: include every (non-association) inherited source property
    props: list[Property] = []
    config_attrs = class_spec.get("attribute", [])
    if config_attrs:
        for attr in config_attrs:
            target_attr_name = attr["name"]
            source_attr_name = attr.get("fromSourceAttributeName", target_attr_name)
            src_prop = _find_source_property(src_cls, source_attr_name, source_model)
            # Multiplicity override or default to source
            mult = _ucmis_multiplicity(attr.get("multiplicity"))
            if src_prop is None:
                # No source attribute; treat the attribute as new — config must
                # supply dataType (per v1.1 we don't enforce this for 'map' but
                # warn). Falls back to string.
                dtype_name = attr.get("dataType", "String")
                tid, prim = _resolve_dataType_ref(
                    dtype_name, source_model, target_classes_by_name
                )
                lower, upper = mult or (0, 1)
                props.append(Property(
                    id=f"{tgt_id}-{target_attr_name}",
                    name=target_attr_name,
                    type_id=tid, primitive=prim,
                    lower=lower, upper=upper,
                    doc=attr.get("definition"),
                    is_assoc_end=False, aggregation=None,
                ))
                continue
            # Use source's type unless dataType override is present
            tid = src_prop.type_id
            prim = src_prop.primitive
            if "dataType" in attr:
                tid, prim = _resolve_dataType_ref(
                    attr["dataType"], source_model, target_classes_by_name
                )
            lower, upper = mult or (src_prop.lower, src_prop.upper)
            doc = attr.get("definition") or src_prop.doc
            props.append(Property(
                id=f"{tgt_id}-{target_attr_name}",
                name=target_attr_name, type_id=tid, primitive=prim,
                lower=lower, upper=upper, doc=doc,
                is_assoc_end=False, aggregation=src_prop.aggregation,
            ))
    else:
        # No attribute list in config: copy all source props
        for p in collect_inherited_properties(src_cls.id, source_model):
            if p.is_assoc_end or not p.name:
                continue
            props.append(Property(
                id=f"{tgt_id}-{p.name}",
                name=p.name, type_id=p.type_id, primitive=p.primitive,
                lower=p.lower, upper=p.upper, doc=p.doc,
                is_assoc_end=False, aggregation=p.aggregation,
            ))

    return UmlClass(
        id=tgt_id, name=target_name, package="", doc=doc_src.doc,
        is_abstract=class_spec.get("isAbstract", src_cls.is_abstract),
        parents=[], properties=props,
        kind="class", literals=[],
        is_datatype=class_spec.get("isDataType", False),
    )


def _render_merge_class(
    class_spec: dict,
    source_model: Model,
    target_classes_by_name: dict[str, UmlClass],
    acronym: str,
) -> UmlClass:
    """Render a mappingType='merge' class spec (two source classes -> one)."""
    src_names = class_spec["sourceClass"]
    src_ids_a = source_model.name_to_id.get(src_names[0], [])
    src_ids_b = source_model.name_to_id.get(src_names[1], [])
    if not src_ids_a or not src_ids_b:
        raise SystemExit(f"merge: source class(es) {src_names} not found")
    src_a = source_model.elements[src_ids_a[0]]
    src_b = source_model.elements[src_ids_b[0]]
    target_name = class_spec["targetClass"]
    tgt_id = _profile_xmi_id(acronym, target_name)

    # Use definitionFrom (one of the two source names) for the class doc.
    doc_src = src_a
    dfrom = class_spec.get("definitionFrom")
    if dfrom == src_b.name:
        doc_src = src_b

    props: list[Property] = []
    for attr in class_spec.get("attribute", []):
        target_attr_name = attr["name"]
        source_attr_name = attr.get("fromSourceAttributeName", target_attr_name)
        from_src = attr.get("fromSourceClass")
        candidates = [src_a, src_b]
        if from_src:
            candidates = [c for c in candidates if c.name == from_src]
        src_prop = None
        for c in candidates:
            src_prop = _find_source_property(c, source_attr_name, source_model)
            if src_prop:
                break
        if src_prop is None:
            tid, prim = _resolve_dataType_ref(
                attr.get("dataType", "String"), source_model, target_classes_by_name
            )
            lower, upper = _ucmis_multiplicity(attr.get("multiplicity")) or (0, 1)
            props.append(Property(
                id=f"{tgt_id}-{target_attr_name}",
                name=target_attr_name, type_id=tid, primitive=prim,
                lower=lower, upper=upper, doc=attr.get("definition"),
                is_assoc_end=False, aggregation=None,
            ))
            continue
        tid = src_prop.type_id; prim = src_prop.primitive
        if "dataType" in attr:
            tid, prim = _resolve_dataType_ref(
                attr["dataType"], source_model, target_classes_by_name
            )
        mult = _ucmis_multiplicity(attr.get("multiplicity"))
        lower, upper = mult or (src_prop.lower, src_prop.upper)
        doc = attr.get("definition") or src_prop.doc
        props.append(Property(
            id=f"{tgt_id}-{target_attr_name}",
            name=target_attr_name, type_id=tid, primitive=prim,
            lower=lower, upper=upper, doc=doc,
            is_assoc_end=False, aggregation=src_prop.aggregation,
        ))
    return UmlClass(
        id=tgt_id, name=target_name, package="", doc=doc_src.doc,
        is_abstract=class_spec.get("isAbstract", False),
        parents=[], properties=props,
        kind="class", literals=[],
        is_datatype=class_spec.get("isDataType", False),
    )


def _render_new_class(
    class_spec: dict,
    source_model: Model,
    target_classes_by_name: dict[str, UmlClass],
    acronym: str,
) -> UmlClass:
    """Render a mappingType='new' class spec into a synthetic UmlClass."""
    target_name = class_spec["targetClass"]
    tgt_id = _profile_xmi_id(acronym, target_name)
    props: list[Property] = []
    for attr in class_spec.get("attribute", []):
        tid, prim = _resolve_dataType_ref(
            attr["dataType"], source_model, target_classes_by_name
        )
        lower, upper = _ucmis_multiplicity(attr.get("multiplicity")) or (0, 1)
        props.append(Property(
            id=f"{tgt_id}-{attr['name']}",
            name=attr["name"], type_id=tid, primitive=prim,
            lower=lower, upper=upper, doc=attr.get("definition"),
            is_assoc_end=False, aggregation=None,
        ))
    return UmlClass(
        id=tgt_id, name=target_name, package="", doc=class_spec.get("definition"),
        is_abstract=class_spec.get("isAbstract", False),
        parents=[], properties=props,
        kind="class", literals=[],
        is_datatype=class_spec.get("isDataType", False),
    )


def _parse_assoc_triple(name: str) -> Optional[tuple[str, str, str]]:
    m = re.match(r"^([A-Z][A-Za-z]*)_([a-z][A-Za-z]*)_([A-Z][A-Za-z]*)$", name)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _add_association_property(
    assoc_spec: dict,
    target_classes_by_name: dict[str, UmlClass],
    source_model: Model,
    acronym: str,
) -> None:
    """Add a synthetic assoc-end Property to the relevant target class so the
    closure walker registers the association edge. Both ends must be among the
    target classes for the edge to be emitted as a UML Association."""
    target_name = assoc_spec.get("targetAssociationName") or assoc_spec.get("sourceAssociationName")
    if not target_name:
        return
    triple = _parse_assoc_triple(target_name)
    if not triple:
        return
    subj_name, role, obj_name = triple

    # Allow explicit override via subjectClass/objectClass
    subj_name = assoc_spec.get("subjectClass", subj_name)
    obj_name = assoc_spec.get("objectClass", obj_name)

    subj_cls = target_classes_by_name.get(subj_name)
    obj_cls = target_classes_by_name.get(obj_name)
    if subj_cls is None or obj_cls is None:
        # Edge cannot be emitted as a UML Association (one end is foreign).
        # Warn so the user knows the cross-profile reference is silent here.
        print(
            f"WARN: association '{target_name}' skipped — "
            f"{'subject' if subj_cls is None else 'object'} class "
            f"'{subj_name if subj_cls is None else obj_name}' is not in this "
            "profile's class set (it likely lives in a lower-level profile "
            "that this UML is meant to compose with).",
            file=sys.stderr,
        )
        return

    # Multiplicity: prefer config override, else default 0..*
    obj_mult = _ucmis_multiplicity(assoc_spec.get("objectClassMultiplicity")) or (0, -1)
    aggregation = assoc_spec.get("objectClassAggregationKind")
    if aggregation == "none":
        aggregation = None

    # Synthetic Property on the subject class. Disambiguate the prop_id by
    # object name so a single subject can carry multiple associations sharing
    # a role name but pointing at different target classes (e.g.
    # Dataset_creator_Person and Dataset_creator_Organization).
    prop_id = f"{subj_cls.id}-{role}_{obj_cls.name}"
    if any(p.id == prop_id for p in subj_cls.properties):
        return
    subj_cls.properties.append(Property(
        id=prop_id, name=role,
        type_id=obj_cls.id, primitive=None,
        lower=obj_mult[0], upper=obj_mult[1],
        doc=assoc_spec.get("definition"),
        is_assoc_end=True, aggregation=aggregation,
    ))


# ---------------------------------------------------------------------------
# PlantUML emit
#
# Produces one `.pu` per class (a context diagram focused on that class) plus
# an `index.pu` overview of the whole profile. Intended to back the HTML
# model browser by feeding the per-class diagrams to plantuml.jar for SVG
# rendering. Reuses compute_uml_closure to find the included classes,
# datatypes, enumerations and associations.
# ---------------------------------------------------------------------------

# Colors mirror UCMIS-M2T's puUtilityDoc.mtl convention (main item highlighted,
# context elements muted, datatypes warm).
_PUML_COLORS = {
    "main_bg":       "#FEFECE",
    "main_border":   "#404040",
    "context_bg":    "#EEEEEE",
    "super_bg":      "#D6EAF8",  # supertypes light blue
    "sub_bg":        "#D5F5E3",  # subtypes light green
    "datatype_bg":   "#FAE5D3",
    "enum_bg":       "#F9E79F",
}

# Overview class-box fill keyed by the profile that introduced the class
# (the originating profile, as recorded in the cross-profile registry). Lets the
# composed model browser show at a glance where each class comes from. Keys are
# the registry's profile umlNames. The HTML legend is built from the same map.
_PROFILE_COLORS = {
    "CDIFCore":            "#D6EAF8",  # blue
    "CDIFDiscovery":       "#D5F5E3",  # green
    "CDIFDataDescription": "#FCF3CF",  # yellow
    "CDIFDataStructure":   "#FAE5D3",  # peach
    "CDIFCodelist":        "#E8DAEF",  # purple
}
_PROFILE_COLOR_DEFAULT = "#FEFECE"
# Display order for the legend; any profile not listed is appended afterwards.
_PROFILE_ORDER = ["CDIFCore", "CDIFDiscovery", "CDIFDataDescription",
                  "CDIFDataStructure", "CDIFCodelist"]
# Origins hidden by the overview "Hide inherited" toggle.
_INHERITED_ORIGINS = {"CDIFCore", "CDIFDiscovery"}


def _overview_keep_local(name: str, origin: Optional[dict[str, str]],
                         profile_name: str) -> bool:
    """Keep predicate for the simplified overview: drop only Core/Discovery
    classes that are *inherited* into this profile (a profile never hides its
    own classes, so Core/Discovery's own pages keep everything)."""
    o = (origin or {}).get(name, profile_name)
    return o == profile_name or o not in _INHERITED_ORIGINS


def _puml_safe(name: str) -> str:
    """Convert a UML name into a token usable as a PlantUML alias."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _puml_multiplicity(lower: int, upper: int) -> str:
    if lower == upper:
        return f"{lower}" if upper != -1 else "*"
    lo = str(lower)
    hi = "*" if upper == -1 else str(upper)
    return f"{lo}..{hi}"


def _puml_attr_type(prop: Property, model: Model) -> str:
    if prop.primitive:
        return prop.primitive
    if prop.type_id:
        target = model.elements.get(prop.type_id)
        if target is not None:
            return target.name
    return "String"


# Default overview layout. Individual overview diagrams are auto-fitted after
# rendering (see _fit_overview_aspect): the box font / ranksep / nodesep are
# adjusted per diagram so wide graphs (which curved-edge Graphviz stretches to
# 4-7:1) are pulled back toward a readable aspect ratio. Detail and datatype
# diagrams keep these defaults.
_PUML_RANKSEP_DEFAULT = 250
_PUML_NODESEP_DEFAULT = 20
_PUML_FONT_DEFAULT = 36


def _puml_header(title: str, *, ranksep: int = _PUML_RANKSEP_DEFAULT,
                 nodesep: int = _PUML_NODESEP_DEFAULT,
                 font: int = _PUML_FONT_DEFAULT) -> list[str]:
    return [
        "@startuml",
        f"title {title}",
        "hide circle",
        "hide empty members",
        "skinparam shadowing false",
        "skinparam ArrowThickness 1.2",
        # Layout: large ranksep + small nodesep pushes Graphviz toward a roughly
        # square bounding box (lines are left as the PlantUML default spline /
        # curved -- ortho Manhattan routing was tried and judged too angular).
        f"skinparam ranksep {ranksep}",
        f"skinparam nodesep {nodesep}",
        # Bigger fonts so class boxes (and the title / association labels) fill
        # more of the bounding box -- diagrams looked too sparse at the
        # default size. ~3x the original 12pt; boxes auto-scale to the text.
        f"skinparam defaultFontSize {font}",
        "skinparam class {",
        "  ArrowColor #404040",
        "  BorderColor #404040",
        f"  FontSize {font}",
        f"  AttributeFontSize {max(font - 6, 8)}",
        "}",
        "",
    ]


def _puml_class_box(cls: UmlClass, *, alias: str, bg: str,
                    show_attrs: bool, model: Model,
                    hide_prop_names: Optional[set[str]] = None) -> list[str]:
    stereotype = ""
    if cls.kind == "datatype":
        stereotype = " <<dataType>>"
    elif cls.kind == "enumeration":
        stereotype = " <<enumeration>>"
    keyword = "abstract class" if cls.is_abstract else "class"
    if cls.kind == "enumeration":
        keyword = "enum"
    head = f'{keyword} "{cls.name}" as {alias}{stereotype} {bg}'
    out = [head]
    if cls.kind == "enumeration" and cls.literals:
        out.append("{")
        for lit in cls.literals:
            out.append(f"  {lit}")
        out.append("}")
        return out
    if show_attrs:
        attrs = [p for p in cls.properties if not p.is_assoc_end
                 and not (hide_prop_names and p.name in hide_prop_names)]
        if attrs:
            out[-1] = head + " {"
            for p in attrs:
                t = _puml_attr_type(p, model)
                mult = _puml_multiplicity(p.lower, p.upper)
                out.append(f"  +{p.name} : {t} [{mult}]")
            out.append("}")
    return out


def _associations_for(cls: UmlClass, closure: UmlClosure) -> list[tuple]:
    """Return association tuples touching cls."""
    return [a for a in closure.associations if a[1] == cls.id or a[2] == cls.id]


def _ancestor_ids(cls: UmlClass, model: Model) -> set[str]:
    """Transitive set of generalization-parent class ids of cls."""
    out: set[str] = set()
    stack = list(cls.parents)
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.add(pid)
        parent = model.elements.get(pid)
        if parent is not None:
            stack.extend(parent.parents)
    return out


def _inherited_assoc_keys(cls: UmlClass, closure: UmlClosure,
                          model: Model) -> set[tuple[str, str]]:
    """(role, target-id) pairs of outgoing associations owned by an ancestor.

    The closure records an outgoing association for every class using its full
    (inherited) property set — and a bare `map` copies the source class's
    inherited attributes onto the target too — so a subclass ends up carrying
    its own copy of each inherited association. An outgoing edge whose
    (role, target) matches an ancestor's is therefore inherited; dropping it
    from the subclass's context diagram / table leaves only the associations
    specific to that class (inherited ones show on the parent's page, reachable
    via the generalization arrow). Per-variant overrides that reuse a role with
    a *different* target (e.g. WideDataStructure `has` MeasureComponent vs the
    generic DataStructure `has` DataStructureComponent) keep distinct keys and
    are preserved."""
    ancestors = _ancestor_ids(cls, model)
    return {(a[3], a[2]) for a in closure.associations if a[1] in ancestors}


def _inherited_prop_names(cls: UmlClass, model: Model) -> set[str]:
    """Names of properties (attributes or association ends) declared on any
    ancestor of cls. A bare `map` copies a source class's inherited attributes
    onto the target, so a subclass lists them as if its own; matching by name
    against ancestors identifies the inherited ones so a context diagram / page
    can show only the members specific to the class. (Association ends are
    additionally keyed by target via _inherited_assoc_keys so a per-variant
    override that reuses a role name with a different target is preserved.)"""
    names: set[str] = set()
    for aid in _ancestor_ids(cls, model):
        a = model.elements.get(aid)
        if a is not None:
            names |= {p.name for p in a.properties}
    return names


def _aggregation_arrow(agg: Optional[str], direction: str = "out") -> str:
    # direction "out" = main class points to other; "in" = other points to main
    if agg == "composite":
        return "*--" if direction == "out" else "--*"
    if agg == "shared":
        return "o--" if direction == "out" else "--o"
    return "-->" if direction == "out" else "<--"


def _emit_class_diagram(cls: UmlClass, closure: UmlClosure, model: Model,
                        out_dir: Path) -> Path:
    """Write a context diagram for one class as <ClassName>.pu."""
    lines = _puml_header(cls.name)
    main_alias = _puml_safe(cls.name)
    related_aliases: dict[str, str] = {}  # id -> alias

    def add_related(target: UmlClass, role: str) -> str:
        if target.id == cls.id:
            return main_alias
        if target.id in related_aliases:
            return related_aliases[target.id]
        alias = _puml_safe(target.name)
        if alias == main_alias:
            alias = main_alias + "_x"
        related_aliases[target.id] = alias
        if role == "super":
            bg = _PUML_COLORS["super_bg"]
        elif role == "sub":
            bg = _PUML_COLORS["sub_bg"]
        elif target.kind == "datatype":
            bg = _PUML_COLORS["datatype_bg"]
        elif target.kind == "enumeration":
            bg = _PUML_COLORS["enum_bg"]
        else:
            bg = _PUML_COLORS["context_bg"]
        lines.extend(_puml_class_box(target, alias=alias, bg=bg,
                                      show_attrs=False, model=model))
        return alias

    # Members inherited from an ancestor are shown on the ancestor's page, not
    # duplicated here (only members specific to this class belong on its
    # context diagram). Association ends are additionally keyed by target.
    inherited_names = _inherited_prop_names(cls, model)
    inherited_keys = _inherited_assoc_keys(cls, closure, model)

    # Main class with attributes (own only)
    lines.extend(_puml_class_box(cls, alias=main_alias,
                                  bg=_PUML_COLORS["main_bg"],
                                  show_attrs=True, model=model,
                                  hide_prop_names=inherited_names))

    # Supertypes (direct only)
    for pid in cls.parents:
        parent = model.elements.get(pid)
        if parent is None:
            continue
        alias = add_related(parent, "super")
        lines.append(f"{alias} <|-- {main_alias}")

    # Subtypes (direct only, scan included class set)
    for other in closure.classes:
        if cls.id in other.parents:
            alias = add_related(other, "sub")
            lines.append(f"{main_alias} <|-- {alias}")

    # Associations
    assoc_edges: set[tuple[str, str]] = set()  # (other_id, role) already drawn
    for assoc in _associations_for(cls, closure):
        _pid, src_id, tgt_id, role, lo, up, agg = assoc
        if src_id == cls.id:
            # Skip outgoing associations inherited from an ancestor — they are
            # shown on the ancestor's diagram. Only the class's own associations
            # belong on its context diagram.
            if (role, tgt_id) in inherited_keys:
                continue
            other_id = tgt_id
            direction = "out"
        else:
            other_id = src_id
            direction = "in"
        other = model.elements.get(other_id)
        if other is None:
            continue
        assoc_edges.add((other_id, role))
        alias = add_related(other, "context")
        # Always render source --> target (forward arrow), matching the overview,
        # so the arrowhead lands on the association target. For an incoming
        # association the main class IS the target, so the other (source) class
        # goes on the left; the multiplicity stays at the target end.
        arrow = _aggregation_arrow(agg, "out")
        mult = _puml_multiplicity(lo, up)
        if direction == "out":
            lines.append(f'{main_alias} {arrow} "{mult}" {alias} : {role}')
        else:
            lines.append(f'{alias} {arrow} "{mult}" {main_alias} : {role}')

    # Referenced types on attributes. A class-typed attribute (e.g. the union
    # types DefinedTerm/Identifier/Reference/AdditionalProperty) is drawn as an
    # association so the class shows as a linked box; datatypes/enumerations are
    # drawn as a ..> dependency. One edge per attribute (the box is de-duped by
    # add_related), so multiple attributes of the same class each get a link.
    for p in cls.properties:
        if p.is_assoc_end or not p.type_id:
            continue
        if p.name in inherited_names:
            continue  # inherited attribute reference — shown on the parent
        target = model.elements.get(p.type_id)
        if target is None:
            continue
        if target.kind == "class":
            # Skip if this class+role is already drawn as a real association
            # (a property can be merged in as both an assoc-end and an attribute).
            if (target.id, p.name) in assoc_edges:
                continue
            alias = add_related(target, "context")
            mult = _puml_multiplicity(p.lower, p.upper)
            lines.append(f'{main_alias} --> "{mult}" {alias} : {p.name}')
        else:
            if target.id in related_aliases:
                continue
            alias = add_related(target, "context")
            lines.append(f"{main_alias} ..> {alias} : {p.name}")

    lines.append("")
    lines.append("@enduml")

    out_path = out_dir / f"{_puml_safe(cls.name)}.pu"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _emit_overview_diagram(closure: UmlClosure, model: Model,
                           profile_name: str, out_path: Path, *,
                           origin: Optional[dict[str, str]] = None,
                           out_path_local: Optional[Path] = None) -> None:
    """Write an overview index.pu showing all profile classes + inheritance +
    associations (no attributes). Class boxes are filled by their originating
    profile (`origin`: class name -> profile umlName); classes absent from the
    map are treated as belonging to this profile.

    If `out_path_local` is given, also write a simplified overview that omits
    classes whose origin is in `_INHERITED_ORIGINS` (Core/Discovery) and any
    edge that would touch an omitted class."""
    origin = origin or {}

    # Edges a subclass inherited from an ancestor are drawn on the ancestor
    # only, not duplicated on every subclass (matches the per-class diagrams).
    inherited_edges: set[tuple[str, str, str]] = set()
    for cls in closure.classes:
        for role, tgt in _inherited_assoc_keys(cls, closure, model):
            inherited_edges.add((cls.id, role, tgt))

    def color_for(name: str) -> str:
        return _PROFILE_COLORS.get(origin.get(name, profile_name),
                                   _PROFILE_COLOR_DEFAULT)

    def build(keep) -> str:
        lines = _puml_header(f"{profile_name} — overview")
        aliases: dict[str, str] = {}
        for cls in closure.classes:
            if not keep(cls.name):
                continue
            alias = _puml_safe(cls.name)
            aliases[cls.id] = alias
            lines.extend(_puml_class_box(cls, alias=alias,
                                          bg=color_for(cls.name),
                                          show_attrs=False, model=model))
        # Inheritance edges between included classes
        for cls in closure.classes:
            if cls.id not in aliases:
                continue
            for pid in cls.parents:
                if pid in aliases:
                    lines.append(f"{aliases[pid]} <|-- {aliases[cls.id]}")
        # Associations (both ends must be included)
        for assoc in closure.associations:
            _pid, src_id, tgt_id, role, lo, up, agg = assoc
            if src_id not in aliases or tgt_id not in aliases:
                continue
            if (src_id, role, tgt_id) in inherited_edges:
                continue  # inherited from an ancestor — drawn on the parent only
            arrow = _aggregation_arrow(agg, "out")
            mult = _puml_multiplicity(lo, up)
            lines.append(f'{aliases[src_id]} {arrow} "{mult}" {aliases[tgt_id]} : {role}')
        lines.append("")
        lines.append("@enduml")
        return "\n".join(lines)

    out_path.write_text(build(lambda _n: True), encoding="utf-8")
    if out_path_local is not None:
        out_path_local.write_text(
            build(lambda n: _overview_keep_local(n, origin, profile_name)),
            encoding="utf-8")


# Overview aspect-ratio auto-fit -----------------------------------------------
#
# Graphviz with curved (spline) edges stretches wider as a class graph grows;
# the stretch is driven by graph *topology*, not class count (a 16-class graph
# can render 4.5:1 while a 39-class one renders 0.8:1). So rather than guess
# layout from the class count, we render each overview at the default settings,
# measure the actual width:height, and -- only if it falls outside a readable
# band -- step the box font / ranksep / nodesep along a ladder and re-render
# until it lands in band (or as close as the ladder reaches). In-band diagrams
# are left byte-identical (no re-render, no diff).
_ASPECT_LOW = 0.55
_ASPECT_HIGH = 2.0
# Each rung is (ranksep, nodesep, font). Wide graphs: shrink font + nodesep,
# grow ranksep to trade width for height. Tall graphs: the reverse.
_FIT_LADDER_WIDE = [(320, 16, 30), (360, 14, 26), (400, 12, 22),
                    (450, 10, 18), (550, 8, 16)]
_FIT_LADDER_TALL = [(200, 30, 36), (160, 40, 36), (120, 55, 36), (90, 75, 36)]


def _svg_aspect(svg_path: Path) -> Optional[float]:
    """Return width/height of a rendered PlantUML SVG, or None."""
    try:
        head = svg_path.read_text(encoding="utf-8")[:2000]
    except OSError:
        return None
    m = re.search(r'style="width:(\d+)px;height:(\d+)px', head)
    if not m:
        m = re.search(r'width="(\d+)px"\s+height="(\d+)px"', head)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w / h) if h else None


def _apply_puml_layout(pu_text: str, ranksep: int, nodesep: int, font: int) -> str:
    """Rewrite the ranksep / nodesep / font skinparam lines in a .pu."""
    t = re.sub(r'(?m)^skinparam ranksep \d+', f'skinparam ranksep {ranksep}', pu_text)
    t = re.sub(r'(?m)^skinparam nodesep \d+', f'skinparam nodesep {nodesep}', t)
    t = re.sub(r'(?m)^skinparam defaultFontSize \d+',
               f'skinparam defaultFontSize {font}', t)
    t = re.sub(r'(?m)^(\s*)FontSize \d+', rf'\g<1>FontSize {font}', t)
    t = re.sub(r'(?m)^(\s*)AttributeFontSize \d+',
               rf'\g<1>AttributeFontSize {max(font - 6, 8)}', t)
    return t


def _fit_overview_aspect(pu_path: Path, plantuml_jar: Path, java_exe: str) -> bool:
    """If the rendered overview is too wide/tall, step its layout along a ladder
    and re-render in place until the aspect ratio is in [_ASPECT_LOW,
    _ASPECT_HIGH] (or as close as the ladder reaches). Returns True if the .pu
    was rewritten."""
    import subprocess

    def render() -> Optional[float]:
        r = subprocess.run([java_exe, "-jar", str(plantuml_jar), "-tsvg", str(pu_path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return _svg_aspect(pu_path.with_suffix(".svg"))

    svg = pu_path.with_suffix(".svg")
    aspect = _svg_aspect(svg) if svg.exists() else render()
    if aspect is None or _ASPECT_LOW <= aspect <= _ASPECT_HIGH:
        return False

    original = pu_path.read_text(encoding="utf-8")
    ladder = _FIT_LADDER_WIDE if aspect > _ASPECT_HIGH else _FIT_LADDER_TALL

    def dist(a: float) -> float:
        if a < _ASPECT_LOW:
            return _ASPECT_LOW - a
        if a > _ASPECT_HIGH:
            return a - _ASPECT_HIGH
        return 0.0

    best = (dist(aspect), original)  # (distance-to-band, pu_text)
    for ranksep, nodesep, font in ladder:
        pu_path.write_text(_apply_puml_layout(original, ranksep, nodesep, font),
                           encoding="utf-8")
        a = render()
        if a is None:
            continue
        if _ASPECT_LOW <= a <= _ASPECT_HIGH:
            return True  # in band; current .pu/.svg on disk are the keepers
        d = dist(a)
        if d < best[0]:
            best = (d, pu_path.read_text(encoding="utf-8"))

    # No rung landed in band -> write the closest one and render it.
    pu_path.write_text(best[1], encoding="utf-8")
    render()
    return best[1] != original


def render_puml_to_svg(puml_dir: Path, plantuml_jar: Path,
                        java_exe: str = "java") -> int:
    """Render each .pu in puml_dir to .svg via plantuml.jar.

    A .pu is (re)rendered when its .svg is missing OR its content changed since
    the .svg was last produced. "Changed" is decided by a content hash recorded
    in _render_cache.json (relative-pu-path -> sha256 of the .pu), NOT by mtime:
    an mtime check can silently keep a stale diagram when a .pu is rewritten but
    the render is skipped/fails, which is exactly how the committed SVGs once
    drifted from their sources. Cache entries are updated only for files that
    rendered successfully, so a failed render is retried on the next run.
    Returns the count of .svg files successfully rendered."""
    import subprocess, hashlib, json
    pu_files = list(puml_dir.rglob("*.pu"))
    cache_path = puml_dir / "_render_cache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    # Cache schema: {rel: {"pu_hash": <sha256>, "svg_mtime": <float>}}
    # Legacy entries (bare hash string) are treated as unfresh so the next run
    # produces the richer entry.
    cur_hash: dict[str, str] = {}
    to_render: list[Path] = []
    for pu in pu_files:
        rel = pu.relative_to(puml_dir).as_posix()
        h = _sha(pu)
        cur_hash[rel] = h
        svg = pu.with_suffix(".svg")
        entry = cache.get(rel)
        # Freshness: svg exists AND cached pu-hash matches current pu content
        # AND svg on disk has the mtime we recorded when we last rendered it.
        # The svg_mtime check catches the case where a git pull/checkout has
        # restored an older committed .svg under a newer .pu (or vice versa):
        # the restored file's mtime is the checkout time, not the mtime we
        # recorded, so we re-render.
        fresh = (
            svg.exists()
            and isinstance(entry, dict)
            and entry.get("pu_hash") == h
            and entry.get("svg_mtime") == svg.stat().st_mtime
        )
        if not fresh:
            to_render.append(pu)

    rendered: list[Path] = []
    if to_render:
        # PlantUML can take multiple input files; group by directory to keep
        # output side-by-side with input.
        by_dir: dict[Path, list[Path]] = {}
        for pu in to_render:
            by_dir.setdefault(pu.parent, []).append(pu)
        for d, files in by_dir.items():
            cmd = [java_exe, "-jar", str(plantuml_jar), "-tsvg"] + [str(f) for f in files]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                # Leave these files out of the cache so they retry next run.
                print(f"WARN: plantuml render failed in {d}: {result.stderr[:200]}",
                      file=sys.stderr)
                continue
            for f in files:
                if f.with_suffix(".svg").exists():
                    rendered.append(f)

    # Auto-fit overview aspect ratios. Only index.pu / index.local.pu are
    # fitted (the pages a reader lands on); detail/datatype diagrams are small
    # and keep the defaults. A fit rewrites the .pu, so refresh its hash so the
    # cache reflects the fitted content.
    for pu in pu_files:
        if pu.name in ("index.pu", "index.local.pu"):
            if _fit_overview_aspect(pu, plantuml_jar, java_exe):
                rel = pu.relative_to(puml_dir).as_posix()
                cur_hash[rel] = hashlib.sha256(pu.read_bytes()).hexdigest()
                if pu not in rendered:
                    rendered.append(pu)

    # Record (pu_hash, svg_mtime) for each successfully rendered .pu; prune
    # entries for .pu files that no longer exist. Skipped (up-to-date) entries
    # are preserved.
    for f in rendered:
        rel = f.relative_to(puml_dir).as_posix()
        svg = f.with_suffix(".svg")
        if svg.exists():
            cache[rel] = {"pu_hash": cur_hash[rel], "svg_mtime": svg.stat().st_mtime}
    cache = {k: v for k, v in cache.items() if k in cur_hash}
    try:
        cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True) + "\n",
                              encoding="utf-8")
    except OSError as e:
        print(f"WARN: could not write {cache_path}: {e}", file=sys.stderr)
    return len(rendered)


def emit_puml(out_dir: Path, *,
              profile_name: str,
              roots: list[UmlClass],
              ctx: BuildContext,
              model: Model,
              origin: Optional[dict[str, str]] = None) -> dict:
    """Emit PlantUML files for a profile.

    Layout:
      <out_dir>/index.pu              — overview of all classes
      <out_dir>/index.local.pu        — overview with inherited Core/Discovery hidden
      <out_dir>/Classes/<Name>.pu     — per-class context diagram
      <out_dir>/DataTypes/<Name>.pu   — per-datatype usage diagram

    `origin` maps class name -> originating profile umlName (cross-profile
    registry); used to color overview boxes and build the simplified overview.

    Returns a manifest dict with counts and paths.
    """
    closure = compute_uml_closure(roots, ctx, model)
    out_dir.mkdir(parents=True, exist_ok=True)
    classes_dir = out_dir / "Classes"
    classes_dir.mkdir(exist_ok=True)
    datatypes_dir = out_dir / "DataTypes"
    datatypes_dir.mkdir(exist_ok=True)

    # A simplified overview is only meaningful when some classes would be hidden
    # (Core/Discovery-origin) AND some would remain — otherwise (Core, Discovery,
    # Codelist) skip it and clear any stale copy from a previous run.
    kept = sum(1 for c in closure.classes
               if _overview_keep_local(c.name, origin, profile_name))
    local_pu = out_dir / "index.local.pu"
    if 0 < kept < len(closure.classes):
        _emit_overview_diagram(closure, model, profile_name, out_dir / "index.pu",
                               origin=origin, out_path_local=local_pu)
    else:
        _emit_overview_diagram(closure, model, profile_name, out_dir / "index.pu",
                               origin=origin)
        for stale in (local_pu, local_pu.with_suffix(".svg")):
            if stale.exists():
                stale.unlink()

    class_paths = []
    for cls in closure.classes:
        class_paths.append(_emit_class_diagram(cls, closure, model, classes_dir))

    # DataType usage diagrams: which classes/attributes use this datatype
    dt_paths = []
    for dt in closure.datatypes + closure.enumerations:
        lines = _puml_header(dt.name)
        dt_alias = _puml_safe(dt.name)
        bg = (_PUML_COLORS["enum_bg"] if dt.kind == "enumeration"
              else _PUML_COLORS["datatype_bg"])
        lines.extend(_puml_class_box(dt, alias=dt_alias, bg=bg,
                                      show_attrs=True, model=model))
        users: set[str] = set()
        for cls in closure.classes:
            for p in cls.properties:
                if p.type_id == dt.id and not p.is_assoc_end:
                    user_alias = _puml_safe(cls.name)
                    if cls.id not in users:
                        users.add(cls.id)
                        lines.extend(_puml_class_box(cls, alias=user_alias,
                                                      bg=_PUML_COLORS["context_bg"],
                                                      show_attrs=False, model=model))
                    lines.append(f"{user_alias} ..> {dt_alias} : {p.name}")
        lines.append("")
        lines.append("@enduml")
        path = datatypes_dir / f"{dt_alias}.pu"
        path.write_text("\n".join(lines), encoding="utf-8")
        dt_paths.append(path)

    return {
        "profile": profile_name,
        "out_dir": str(out_dir),
        "classes": len(class_paths),
        "datatypes": len(dt_paths),
        "overview": str(out_dir / "index.pu"),
    }


# ---------------------------------------------------------------------------
# HTML model browser
#
# Produces a static HTML site with one page per class + per datatype, plus a
# profile-level index. Embeds the .pu diagram as a rendered SVG when
# plantuml.jar is available (env var PLANTUML_JAR), otherwise falls back to
# a <pre> block of the .pu source.
# ---------------------------------------------------------------------------

_HTML_STYLE_CSS = """\
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
       margin: 0; padding: 0; color: #222; background: #fafafa; }
header { background: #2c3e50; color: white; padding: 1rem 2rem;
         box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
header a { color: white; text-decoration: none; }
header h1 { margin: 0; font-size: 1.4rem; font-weight: 500; }
nav.breadcrumb { font-size: 0.9rem; opacity: 0.85; margin-top: 0.3rem; }
nav.breadcrumb a { color: #cfe2f3; }
main { max-width: 1100px; margin: 2rem auto; padding: 0 2rem; }
h1.classname { font-size: 1.9rem; margin: 0 0 0.5rem 0; }
.fqn { color: #666; font-size: 0.9rem; margin-bottom: 1rem; font-family: monospace; }
.stereotype { display: inline-block; background: #eef; color: #336;
              padding: 0.1rem 0.5rem; border-radius: 3px;
              font-size: 0.8rem; margin-left: 0.5rem; vertical-align: middle; }
h2 { font-size: 1.2rem; color: #2c3e50; border-bottom: 1px solid #ddd;
     padding-bottom: 0.3rem; margin-top: 2rem; }
.definition { font-size: 1.05rem; line-height: 1.5; color: #444;
              background: white; padding: 1rem; border-left: 3px solid #2c3e50; }
.union-note { font-size: 0.95rem; line-height: 1.5; color: #5a4a1a;
              background: #fdf6e3; padding: 0.8rem 1rem; margin-top: 0.6rem;
              border-left: 3px solid #d4a017; }
.union-note code { background: #f3e9c9; padding: 0 0.2rem; border-radius: 2px; }
table { width: 100%; border-collapse: collapse; background: white;
        box-shadow: 0 1px 2px rgba(0,0,0,0.06); }
th, td { padding: 0.6rem 0.8rem; text-align: left;
         border-bottom: 1px solid #eee; vertical-align: top; }
th { background: #f4f4f4; font-weight: 600; font-size: 0.9rem; color: #555; }
td.name { font-family: monospace; font-weight: 600; white-space: nowrap; }
td.type { font-family: monospace; color: #06c; }
td.mult { font-family: monospace; color: #999; white-space: nowrap; }
td.doc { color: #555; font-size: 0.95rem; }
.rst-doc h4 { margin: 0.4rem 0 0.2rem; font-size: 1rem; color: #333;
              border-bottom: 1px solid #ddd; padding-bottom: 0.1rem; }
.rst-doc h5 { margin: 0.6rem 0 0.15rem; font-size: 0.95rem; color: #06c;
              font-weight: 600; }
.rst-doc h6 { margin: 0.4rem 0 0.05rem; font-size: 0.85rem;
              color: #666; font-weight: 600;
              text-transform: uppercase; letter-spacing: 0.03em; }
.rst-doc p  { margin: 0.15rem 0 0.4rem; }
td.doc .rst-doc h4 { font-size: 0.9rem; }
td.doc .rst-doc h5 { font-size: 0.85rem; }
td.doc .rst-doc h6 { font-size: 0.72rem; }
td.doc .rst-doc p  { font-size: 0.9rem; }
a.classlink { color: #06c; text-decoration: none; }
a.classlink:hover { text-decoration: underline; }
.diagram { background: white; border: 1px solid #eee; position: relative; }
.diagram-viewport { overflow: auto; }
.diagram-stage { text-align: center; padding: 1rem; }
.diagram-stage object, .diagram-stage img, .diagram-stage svg { max-width: 100%; }
.diagram.is-interactive .diagram-viewport { height: 70vh; min-height: 320px;
    overflow: hidden; background: #fff; cursor: grab; touch-action: none;
    user-select: none; -webkit-user-select: none; -moz-user-select: none;
    /* position:relative makes the viewport the containing block for the absolute
       stage, and contain:paint forces the (will-change/transform composited)
       stage to be clipped to this box and confined to its own stacking context —
       otherwise panning the graph up lets it paint over the text above it. */
    position: relative; contain: paint; }
.diagram.is-interactive .diagram-viewport.grabbing { cursor: grabbing; }
.diagram.is-interactive .diagram-stage { position: absolute; top: 0; left: 0;
    padding: 0; transform-origin: 0 0; will-change: transform; }
.diagram.is-interactive .diagram-stage object,
.diagram.is-interactive .diagram-stage svg { max-width: none; display: block;
    pointer-events: none; }
.diagram.is-interactive .diagram-stage svg g.entity.clickable { pointer-events: auto;
    cursor: pointer; }
.diagram.is-interactive .diagram-stage svg g.entity.clickable:hover { opacity: 0.78; }
.diagram-toolbar { position: absolute; top: 8px; right: 8px; display: flex;
    gap: 4px; z-index: 5; }
.diagram-toolbar button { font: 600 13px/1 -apple-system, "Segoe UI", sans-serif;
    color: #fff; background: #2c3e50; border: none; border-radius: 4px;
    padding: 6px 9px; cursor: pointer; opacity: 0.85;
    box-shadow: 0 1px 2px rgba(0,0,0,0.2); }
.diagram-toolbar button:hover { opacity: 1; }
.diagram-toast { position: absolute; bottom: 8px; right: 8px;
    background: rgba(44,62,80,0.92); color: #fff; padding: 6px 10px;
    border-radius: 4px; font-size: 12px; opacity: 0; transition: opacity 0.2s;
    pointer-events: none; z-index: 6; }
.diagram-toast.show { opacity: 1; }
.diagram pre { text-align: left; background: #f8f8f8; padding: 1rem;
               overflow-x: auto; font-size: 0.85rem; }
.overview-toggle { font: 600 13px/1 -apple-system, "Segoe UI", sans-serif;
    color: #fff; background: #2c3e50; border: none; border-radius: 4px;
    padding: 7px 12px; cursor: pointer; margin: 0 0 0.6rem; }
.overview-toggle:hover { opacity: 0.9; }
.diagram-legend { display: flex; flex-wrap: wrap; gap: 0.4rem 1.1rem;
    margin: 0 0 0.8rem; font-size: 0.85rem; color: #444; }
.legend-item { display: inline-flex; align-items: center; gap: 0.35rem; }
.legend-swatch { width: 14px; height: 14px; border: 1px solid #404040;
    border-radius: 2px; display: inline-block; flex: none; }
.overview-section { position: relative; }
.overview-controls { position: sticky; top: 0; z-index: 6; background: #fafafa;
    padding: 0.5rem 0 0.3rem; }
.overview-controls .overview-toggle { margin: 0 0 0.5rem; }
.overview-controls .diagram-legend { margin: 0; }
/* Keep the diagram (whose interactive stage uses transform/will-change, i.e. a
   compositing layer) in its own stacking context BELOW the sticky controls, so
   panning/scrolling never paints the graph over the toggle + legend. */
.overview-section .diagram { z-index: 1; }
.empty { color: #999; font-style: italic; }
ul.classlist { list-style: none; padding: 0;
               display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
               gap: 0.5rem; }
ul.classlist li { background: white; padding: 0.6rem 0.8rem;
                  border: 1px solid #eee; border-radius: 4px; }
ul.classlist a { color: #06c; text-decoration: none; font-family: monospace;
                 font-weight: 600; }
ul.classlist .kind { color: #999; font-size: 0.8rem; margin-left: 0.5rem; }
.parent-chain { font-family: monospace; color: #555; }
.parent-chain .focus { color: #c00; font-weight: bold; }
.stereotype.inherited { background: #e8f5e9; color: #2e7d32; }
.stereotype.inherited a.classlink { color: #1b5e20; }
"""


# Client-side enhancement for the diagram blocks emitted by _html_render_diagram.
# Progressive: with JS the static <object> is upgraded to an inline SVG with
# zoom (buttons + wheel), pan (drag), fit/reset, and copy-to-clipboard (PNG
# raster + SVG vector). Without JS (or under file:// where fetch is blocked) the
# static diagram still renders. Served as _static/diagram.js, linked by the shell.
_HTML_DIAGRAM_JS = r"""
"use strict";
(function () {
  var MIN = 0.1, MAX = 8;
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  function naturalSize(svg) {
    var w = 0, h = 0;
    try {
      if (svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width) {
        w = svg.viewBox.baseVal.width; h = svg.viewBox.baseVal.height;
      }
    } catch (e) {}
    if (!w) { w = parseFloat(svg.getAttribute("width")) || 600; }
    if (!h) { h = parseFloat(svg.getAttribute("height")) || 400; }
    return { w: w, h: h };
  }

  function serialize(svg) {
    var clone = svg.cloneNode(true);
    if (!clone.getAttribute("xmlns")) clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    if (!clone.getAttribute("xmlns:xlink")) clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
    return new XMLSerializer().serializeToString(clone);
  }

  function flash(box, msg) {
    var t = box._toast; if (!t) return;
    t.textContent = msg; t.classList.add("show");
    clearTimeout(t._h); t._h = setTimeout(function () { t.classList.remove("show"); }, 1600);
  }

  // Build {className: href} from the class links already on the page (attribute /
  // association / inheritance tables, or the overview class list). These carry
  // the correct same-profile / cross-profile relative paths, so we reuse them.
  function buildHrefMap() {
    var map = {}, as = document.querySelectorAll("a[href]");
    for (var i = 0; i < as.length; i++) {
      var href = as[i].getAttribute("href");
      if (!href || !/\.html($|[?#])/.test(href)) continue;
      if (!/(^|\/)(Classes|DataTypes)\//.test(href)) continue;
      var name = as[i].textContent.replace(/[↗\s]+$/g, "").trim();
      if (name && !(name in map)) map[name] = href;
    }
    return map;
  }

  // Tag each class box (PlantUML emits <g class="entity" data-qualified-name=..>)
  // that has a known page so clicks can navigate. The focus class has no self
  // link, so it stays non-clickable.
  function makeClickable(svg) {
    var map = buildHrefMap();
    var ents = svg.querySelectorAll("g.entity[data-qualified-name]");
    for (var i = 0; i < ents.length; i++) {
      var href = map[ents[i].getAttribute("data-qualified-name")];
      if (!href) continue;
      ents[i].setAttribute("data-href", href);
      ents[i].classList.add("clickable");
    }
  }

  function copySVG(box, svg) {
    var markup = serialize(svg);
    var fail = function () { flash(box, "Copy failed"); };
    if (navigator.clipboard && window.ClipboardItem) {
      try {
        navigator.clipboard.write([new ClipboardItem({
          "image/svg+xml": new Blob([markup], { type: "image/svg+xml" }),
          "text/plain": new Blob([markup], { type: "text/plain" })
        })]).then(function () { flash(box, "SVG copied to clipboard"); }, function () {
          navigator.clipboard.writeText(markup).then(
            function () { flash(box, "SVG copied (as text)"); }, fail);
        });
        return;
      } catch (e) {}
    }
    if (navigator.clipboard) {
      navigator.clipboard.writeText(markup).then(
        function () { flash(box, "SVG copied (as text)"); }, fail);
    } else { fail(); }
  }

  function copyPNG(box, svg, nat) {
    var fail = function (m) { flash(box, m || "Copy failed"); };
    if (!(navigator.clipboard && window.ClipboardItem)) {
      fail("Clipboard images not supported here"); return;
    }
    var markup = serialize(svg);
    var ratio = clamp((window.devicePixelRatio || 1) * 2, 2, 4);
    var img = new Image();
    var url = URL.createObjectURL(new Blob([markup], { type: "image/svg+xml;charset=utf-8" }));
    img.onload = function () {
      var c = document.createElement("canvas");
      c.width = Math.round(nat.w * ratio); c.height = Math.round(nat.h * ratio);
      var cx = c.getContext("2d");
      cx.fillStyle = "#ffffff"; cx.fillRect(0, 0, c.width, c.height);
      cx.drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      if (!c.toBlob) { fail(); return; }
      c.toBlob(function (png) {
        if (!png) { fail(); return; }
        navigator.clipboard.write([new ClipboardItem({ "image/png": png })]).then(
          function () { flash(box, "PNG copied to clipboard"); },
          function () { fail("Browser blocked image clipboard"); });
      }, "image/png");
    };
    img.onerror = function () { URL.revokeObjectURL(url); fail(); };
    img.src = url;
  }

  function enhance(box, viewport, stage, svg, nat) {
    box.classList.add("is-interactive");
    var scale = 1, tx = 0, ty = 0;
    var viewKey = "cdifDiagramView:" + location.pathname + ":" + (box.getAttribute("data-svg") || "");
    function apply() {
      stage.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + scale + ")";
    }
    function saveView() {
      try { sessionStorage.setItem(viewKey, JSON.stringify({ s: scale, x: tx, y: ty })); } catch (e) {}
    }
    function restoreOrFit() {
      var v = null;
      try { v = JSON.parse(sessionStorage.getItem(viewKey) || "null"); } catch (e) {}
      if (v && v.s) { scale = v.s; tx = v.x; ty = v.y; apply(); } else { fit(); }
    }
    function fit() {
      var vw = viewport.clientWidth, vh = viewport.clientHeight;
      if (!vw || !vh) { requestAnimationFrame(fit); return; }
      scale = clamp(Math.min(vw / nat.w, vh / nat.h), MIN, MAX);
      tx = (vw - nat.w * scale) / 2;
      ty = (vh - nat.h * scale) / 2;
      apply();
    }
    function zoomAt(cx, cy, factor) {
      var ns = clamp(scale * factor, MIN, MAX), k = ns / scale;
      tx = cx - k * (cx - tx); ty = cy - k * (cy - ty); scale = ns; apply();
    }
    function center() { return { x: viewport.clientWidth / 2, y: viewport.clientHeight / 2 }; }

    viewport.addEventListener("wheel", function (e) {
      e.preventDefault();
      var r = viewport.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 1 / 1.12);
    }, { passive: false });

    function navigateAt(x, y) {
      var el = document.elementFromPoint(x, y);
      var g = el && el.closest ? el.closest("g.entity.clickable") : null;
      if (!g) return;
      var href = g.getAttribute("data-href");
      if (!href) return;
      saveView();
      window.location.href = href;
    }
    var dragging = false, lx = 0, ly = 0, downX = 0, downY = 0, moved = false;
    viewport.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      dragging = true; moved = false;
      lx = downX = e.clientX; ly = downY = e.clientY;
      viewport.classList.add("grabbing");
      try { viewport.setPointerCapture(e.pointerId); } catch (x) {}
    });
    viewport.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      tx += e.clientX - lx; ty += e.clientY - ly; lx = e.clientX; ly = e.clientY;
      if (Math.abs(e.clientX - downX) + Math.abs(e.clientY - downY) > 5) moved = true;
      apply();
    });
    viewport.addEventListener("pointerup", function (e) {
      var wasTap = dragging && !moved;
      dragging = false; viewport.classList.remove("grabbing");
      if (wasTap) navigateAt(e.clientX, e.clientY);
    });
    viewport.addEventListener("pointerleave", function () {
      dragging = false; viewport.classList.remove("grabbing");
    });
    viewport.addEventListener("pointercancel", function () {
      dragging = false; viewport.classList.remove("grabbing");
    });
    viewport.addEventListener("dblclick", function () { fit(); });

    var bar = document.createElement("div");
    bar.className = "diagram-toolbar";
    function btn(label, title, fn) {
      var b = document.createElement("button");
      b.type = "button"; b.textContent = label; b.title = title;
      b.addEventListener("click", function (e) { e.preventDefault(); fn(); });
      bar.appendChild(b);
    }
    btn("←", "Back to the previous diagram (restores its zoom/pan)", function () { history.back(); });
    btn("−", "Zoom out", function () { var c = center(); zoomAt(c.x, c.y, 1 / 1.25); });
    btn("+", "Zoom in", function () { var c = center(); zoomAt(c.x, c.y, 1.25); });
    btn("Fit", "Reset / fit to view (or double-click)", fit);
    btn("PNG", "Copy diagram to clipboard as PNG", function () { copyPNG(box, svg, nat); });
    btn("SVG", "Copy diagram to clipboard as SVG", function () { copySVG(box, svg); });
    box.appendChild(bar);

    var toast = document.createElement("div");
    toast.className = "diagram-toast";
    box.appendChild(toast);
    box._toast = toast;

    makeClickable(svg);
    window.addEventListener("pagehide", saveView);
    restoreOrFit();
  }

  function setup(box) {
    if (!box || box._ready) return;   // idempotent: also guards lazy reveal
    box._ready = true;
    var viewport = box.querySelector(".diagram-viewport");
    var stage = box.querySelector(".diagram-stage");
    var src = box.getAttribute("data-svg");
    if (!viewport || !stage || !src) return;
    var url;
    try { url = new URL(src, window.location.href).href; } catch (e) { return; }
    fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    }).then(function (text) {
      var doc = new DOMParser().parseFromString(text, "image/svg+xml");
      var svg = doc.documentElement;
      if (!svg || svg.nodeName.toLowerCase() !== "svg") throw new Error("not svg");
      svg = document.importNode(svg, true);
      var nat = naturalSize(svg);
      svg.setAttribute("width", nat.w);
      svg.setAttribute("height", nat.h);
      stage.innerHTML = "";
      stage.appendChild(svg);
      enhance(box, viewport, stage, svg, nat);
    }).catch(function () { /* keep the static <object> fallback */ });
  }

  function isShown(el) { return !!el && el.offsetParent !== null; }

  // Overview full/simplified toggle (profile index only). The two diagrams are
  // emitted as #overview-full and #overview-local (the latter hidden); the shown
  // one is set up lazily the first time it becomes visible so its fit() runs with
  // a non-zero viewport. The choice is persisted per page in sessionStorage so
  // drilling into a class and using the diagram Back button returns to the same
  // (e.g. simplified) view instead of resetting to the full model.
  function initToggle() {
    var btn = document.querySelector("[data-overview-toggle]");
    var full = document.getElementById("overview-full");
    var local = document.getElementById("overview-local");
    if (!btn || !full || !local) return;
    var key = "cdifOverviewSimplified:" + location.pathname;
    function apply(simplified) {
      full.hidden = simplified;
      local.hidden = !simplified;
      btn.textContent = simplified
        ? "Show full model (with inherited)"
        : "Hide inherited (Core/Discovery)";
      var shown = simplified ? local : full;
      if (shown && !shown._ready) setup(shown);
    }
    var simplified = false;
    try { simplified = sessionStorage.getItem(key) === "1"; } catch (e) {}
    apply(simplified);
    btn.addEventListener("click", function () {
      simplified = !simplified;
      try { sessionStorage.setItem(key, simplified ? "1" : "0"); } catch (e) {}
      apply(simplified);
    });
  }

  function init() {
    // Apply any persisted simplified/full choice first (it sets up the visible
    // diagram), then set up any remaining visible diagrams (class pages, etc.).
    initToggle();
    var boxes = document.querySelectorAll(".diagram[data-diagram]");
    for (var i = 0; i < boxes.length; i++) {
      if (isShown(boxes[i])) setup(boxes[i]);
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }
})();
"""


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# RST underline chars used by rst_augment.compose_documentation, mapped to
# heading levels for the local HTML browser. The class page already uses
# <h1>-<h3>, so start our RST doc at <h4>.
_RST_UNDERLINES = {"=": "h4", "-": "h5", ".": "h6"}


def _rst_body_to_html(text: Optional[str]) -> str:
    """Convert an rst_augment-composed body into HTML section blocks.

    Recognizes the three underline conventions used by the augmenter
    (`===`, `---`, `...`) and turns each heading into <h4>/<h5>/<h6>.
    Consecutive non-blank content lines are grouped into <p> blocks. This
    is intentionally minimal — a real RST parser (docutils) is overkill
    for the fixed shape rst_augment emits. Bodies that do NOT look like
    RST-augmented content (missing the `Documentation\\n===` header) are
    escaped and rendered as a single paragraph so legacy plain-text
    definitions still display.
    """
    if not text:
        return ""
    stripped = text.lstrip()
    if not (stripped.startswith("Documentation\n===") or stripped.startswith("Documentation\r\n===")):
        return f'<p>{_html_escape(text).replace(chr(10), "<br>")}</p>'

    lines = text.splitlines()
    out: list[str] = []
    para: list[str] = []

    def flush():
        if para:
            out.append("<p>" + " ".join(_html_escape(p) for p in para) + "</p>")
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        nxt = lines[i + 1].rstrip() if i + 1 < len(lines) else ""
        # Heading: non-empty text line followed by an underline of a single
        # RST marker char at least as long as the text.
        if (line and nxt and len(set(nxt)) == 1 and nxt[0] in _RST_UNDERLINES
                and len(nxt) >= len(line.strip())):
            flush()
            tag = _RST_UNDERLINES[nxt[0]]
            out.append(f"<{tag}>{_html_escape(line.strip())}</{tag}>")
            i += 2
            continue
        if line.strip():
            para.append(line.strip())
        else:
            flush()
        i += 1
    flush()
    return f'<div class="rst-doc">{"".join(out)}</div>'


def _html_link_class(cls_name: str, kind: str,
                     current_profile: str = "",
                     cross_profile_registry: Optional[dict[str, str]] = None,
                     force_local: bool = False) -> str:
    """Return an <a> linking to another class/datatype page.

    Same-profile target: ../{Classes|DataTypes}/Name.html
    Cross-profile target: ../../{OtherProfile}/{Classes|DataTypes}/Name.html
    Unknown target: plain text (no <a>).

    `force_local=True` overrides the cross-profile registry and always emits a
    local link. Use when the caller knows the class is in the current closure
    (e.g. `cls.id in included_ids`); this avoids the registry's last-wins
    behaviour mis-routing a locally-declared class to a sibling profile that
    happens to redeclare the same name (e.g. Concept / ConceptScheme defined
    independently in cdifCodelist and cdifConceptScheme).
    """
    folder = "DataTypes" if kind in ("datatype", "enumeration") else "Classes"
    other_profile = None if force_local else (cross_profile_registry or {}).get(cls_name)
    if other_profile and other_profile != current_profile:
        href = f"../../{other_profile}/{folder}/{_puml_safe(cls_name)}.html"
        return (f'<a class="classlink" href="{href}" title="in {other_profile}">'
                f'{_html_escape(cls_name)} &#x2197;</a>')
    href = f"../{folder}/{_puml_safe(cls_name)}.html"
    return f'<a class="classlink" href="{href}">{_html_escape(cls_name)}</a>'


def _html_render_diagram(pu_path: Path, *, div_id: Optional[str] = None,
                         hidden: bool = False) -> str:
    """Return the HTML for embedding a diagram. If a sibling .svg exists
    (rendered upstream), embed it via <object>; otherwise drop the .pu source
    in a <pre>. `div_id`/`hidden` support the overview full/simplified toggle."""
    svg_path = pu_path.with_suffix(".svg")
    id_attr = f' id="{div_id}"' if div_id else ""
    hidden_attr = " hidden" if hidden else ""
    if svg_path.exists():
        name = _html_escape(svg_path.name)
        return (f'<div class="diagram" data-diagram data-svg="{name}"{id_attr}{hidden_attr}>'
                f'<div class="diagram-viewport"><div class="diagram-stage">'
                f'<object data="{name}" type="image/svg+xml"></object>'
                f'</div></div></div>')
    return (f'<div class="diagram"{id_attr}{hidden_attr}>'
            f'<pre>{_html_escape(pu_path.read_text(encoding="utf-8"))}</pre>'
            f'<p class="empty">(SVG render unavailable — install plantuml.jar '
            f'and rerun <code>plantuml -tsvg {pu_path.name}</code> in this directory)</p></div>')


def _html_page_shell(title: str, profile_name: str,
                     breadcrumb: list[tuple[str, str]],
                     body: str, depth: int = 2) -> str:
    """Wrap body in the standard HTML shell. depth = number of `..` segments
    needed to reach the profile root (Classes/X.html is 1, index.html is 0)."""
    prefix = "../" * depth
    crumb_html = " &raquo; ".join(
        f'<a href="{prefix}{href}">{_html_escape(label)}</a>' if href else _html_escape(label)
        for label, href in breadcrumb
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html_escape(title)} &mdash; {_html_escape(profile_name)}</title>
  <link rel="stylesheet" href="{prefix}_static/style.css">
  <script defer src="{prefix}_static/diagram.js"></script>
</head>
<body>
<header>
  <h1><a href="{prefix}index.html">{_html_escape(profile_name)}</a></h1>
  <nav class="breadcrumb">{crumb_html}</nav>
</header>
<main>
{body}
</main>
</body>
</html>
"""


def _html_attr_rows(cls: UmlClass, model: Model,
                    included_ids: set[str],
                    current_profile: str = "",
                    registry: Optional[dict[str, str]] = None) -> str:
    inherited_names = _inherited_prop_names(cls, model)
    rows = []
    for p in cls.properties:
        if p.is_assoc_end:
            continue
        if p.name in inherited_names:
            continue  # inherited attribute — shown on the parent's page
        if p.primitive:
            type_html = f'<span>{_html_escape(p.primitive)}</span>'
        elif p.type_id and p.type_id in model.elements:
            t = model.elements[p.type_id]
            if t.id in included_ids:
                type_html = _html_link_class(t.name, t.kind, current_profile, registry,
                                             force_local=True)
            elif registry and t.name in registry:
                type_html = _html_link_class(t.name, t.kind, current_profile, registry)
            else:
                type_html = _html_escape(t.name)
        else:
            type_html = "<span>?</span>"
        mult = _puml_multiplicity(p.lower, p.upper)
        doc = _rst_body_to_html(clean_definition(p.doc))
        rows.append(
            f'<tr><td class="name">{_html_escape(p.name)}</td>'
            f'<td class="type">{type_html}</td>'
            f'<td class="mult">[{mult}]</td>'
            f'<td class="doc">{doc}</td></tr>'
        )
    if not rows:
        return '<p class="empty">No attributes.</p>'
    return ('<table><thead><tr><th>Name</th><th>Type</th>'
            '<th>Multiplicity</th><th>Description</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _html_assoc_rows(cls: UmlClass, closure: UmlClosure,
                     model: Model, included_ids: set[str],
                     current_profile: str = "",
                     registry: Optional[dict[str, str]] = None) -> str:
    inherited_keys = _inherited_assoc_keys(cls, closure, model)
    rows = []
    for assoc in closure.associations:
        _pid, src_id, tgt_id, role, lo, up, agg = assoc
        if cls.id == src_id:
            # Skip outgoing associations inherited from an ancestor — listed on
            # the parent's page; only own associations belong here.
            if (role, tgt_id) in inherited_keys:
                continue
            other_id, direction = tgt_id, "out"
        elif cls.id == tgt_id:
            other_id, direction = src_id, "in"
        else:
            continue
        other = model.elements.get(other_id)
        if other is None:
            continue
        if other.id in included_ids:
            target_html = _html_link_class(other.name, other.kind, current_profile, registry,
                                           force_local=True)
        elif registry and other.name in registry:
            target_html = _html_link_class(other.name, other.kind, current_profile, registry)
        else:
            target_html = _html_escape(other.name)
        mult = _puml_multiplicity(lo, up)
        agg_label = {"composite": "composition", "shared": "aggregation"}.get(agg or "", "association")
        arrow = "&rarr;" if direction == "out" else "&larr;"
        rows.append(
            f'<tr><td class="name">{_html_escape(role)}</td>'
            f'<td>{arrow} {target_html}</td>'
            f'<td class="mult">[{mult}]</td>'
            f'<td class="doc">{_html_escape(agg_label)}</td></tr>'
        )
    if not rows:
        return '<p class="empty">No associations.</p>'
    return ('<table><thead><tr><th>Role</th><th>Target</th>'
            '<th>Multiplicity</th><th>Kind</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _html_inheritance(cls: UmlClass, closure: UmlClosure,
                      model: Model, included_ids: set[str],
                      current_profile: str = "",
                      registry: Optional[dict[str, str]] = None) -> str:
    """Render a simple supertype/subtype list."""
    parts = []
    if cls.parents:
        parents = []
        for pid in cls.parents:
            p = model.elements.get(pid)
            if p is None:
                continue
            if p.id in included_ids:
                parents.append(_html_link_class(p.name, p.kind, current_profile, registry,
                                                force_local=True))
            elif registry and p.name in registry:
                parents.append(_html_link_class(p.name, p.kind, current_profile, registry))
            else:
                parents.append(_html_escape(p.name))
        parts.append(f'<p><strong>Generalizes:</strong> {", ".join(parents)}</p>')
    subs = []
    for other in closure.classes:
        if cls.id in other.parents:
            # subs come from closure.classes which is the local set
            subs.append(_html_link_class(other.name, other.kind, current_profile, registry,
                                         force_local=True))
    if subs:
        parts.append(f'<p><strong>Specialized by:</strong> {", ".join(subs)}</p>')
    if not parts:
        return '<p class="empty">No generalization relationships.</p>'
    return "\n".join(parts)


# Classes that JSON-shape unions reduce to (see ucmism2m union-type policy /
# retype_to_union_canonical.py). On their detail page we note that a plain
# string and/or an @id object reference may be substituted for the full object
# in a JSON instance.
_UNION_SUBSTITUTION_NOTES = {
    "DefinedTerm": ("In a JSON instance, a property typed as <code>DefinedTerm</code> may also be "
                    "supplied as a plain string (the term's label) or as an object reference "
                    "(<code>{\"@id\": ...}</code>) instead of a full schema:DefinedTerm object."),
    "Identifier": ("In a JSON instance, a property typed as <code>Identifier</code> may also be "
                   "supplied as a plain string (the bare identifier value) or as an object reference "
                   "(<code>{\"@id\": ...}</code>) instead of a full schema:PropertyValue object."),
    "Reference": ("In a JSON instance, a property typed as <code>Reference</code> may also be "
                  "supplied as a plain string (a URL) or as an object reference "
                  "(<code>{\"@id\": ...}</code>) instead of a full cdif:Reference object."),
    "AdditionalProperty": ("In a JSON instance, a property typed as <code>AdditionalProperty</code> may also "
                           "be supplied as an object reference (<code>{\"@id\": ...}</code>); a plain string "
                           "is not sufficient because both the property name and its value are required."),
}


def _html_class_page(cls: UmlClass, closure: UmlClosure, model: Model,
                     profile_name: str, profile_dir: Path,
                     puml_dir: Optional[Path],
                     registry: Optional[dict[str, str]] = None,
                     inherited: bool = False) -> None:
    included_ids = closure.class_ids | closure.type_ids
    folder = "Classes" if cls.kind == "class" else "DataTypes"
    stereotype = ""
    if cls.kind == "datatype":
        stereotype = '<span class="stereotype">&laquo;dataType&raquo;</span>'
    elif cls.kind == "enumeration":
        stereotype = '<span class="stereotype">&laquo;enumeration&raquo;</span>'
    elif cls.is_abstract:
        stereotype = '<span class="stereotype">&laquo;abstract&raquo;</span>'
    if inherited and registry:
        origin = registry.get(cls.name)
        if origin and origin != profile_name:
            stereotype += (f' <span class="stereotype inherited" '
                           f'title="Composed from {_html_escape(origin)}">'
                           f'inherited from <a class="classlink" href="../../{origin}/'
                           f'{folder}/{_puml_safe(cls.name)}.html">{_html_escape(origin)}</a>'
                           f'</span>')

    diagram_html = ""
    if puml_dir is not None:
        pu_subdir = "Classes" if cls.kind == "class" else "DataTypes"
        pu_path = puml_dir / pu_subdir / f"{_puml_safe(cls.name)}.pu"
        if pu_path.exists():
            # Copy/symlink the diagram next to the HTML so relative paths work
            target_dir = profile_dir / folder
            target_pu = target_dir / pu_path.name
            if not target_pu.exists() or target_pu.read_text(encoding="utf-8") != pu_path.read_text(encoding="utf-8"):
                target_pu.write_text(pu_path.read_text(encoding="utf-8"), encoding="utf-8")
            # Also copy SVG if present
            svg_path = pu_path.with_suffix(".svg")
            if svg_path.exists():
                (target_dir / svg_path.name).write_bytes(svg_path.read_bytes())
            diagram_html = _html_render_diagram(target_pu)

    definition_html = (f'<div class="definition">{_rst_body_to_html(clean_definition(cls.doc))}</div>'
                       if cls.doc else '<p class="empty">No definition.</p>')

    union_sub = _UNION_SUBSTITUTION_NOTES.get(cls.name)
    union_note_html = (f'<div class="union-note"><strong>JSON serialization:</strong> {union_sub}</div>'
                       if union_sub else "")

    if cls.kind == "enumeration":
        body_extra = "<h2>Literals</h2>"
        if cls.literals:
            body_extra += ("<ul>" + "".join(
                f"<li><code>{_html_escape(lit)}</code></li>" for lit in cls.literals) + "</ul>")
        else:
            body_extra += '<p class="empty">No literals.</p>'
    else:
        attrs_html = _html_attr_rows(cls, model, included_ids, profile_name, registry)
        assocs_html = _html_assoc_rows(cls, closure, model, included_ids, profile_name, registry)
        inherit_html = _html_inheritance(cls, closure, model, included_ids, profile_name, registry)
        body_extra = (
            f'<h2>Inheritance</h2>{inherit_html}'
            f'<h2>Attributes</h2>{attrs_html}'
            f'<h2>Associations</h2>{assocs_html}'
        )

    body = (
        f'<h1 class="classname">{_html_escape(cls.name)}{stereotype}</h1>'
        f'<p class="fqn">{_html_escape(profile_name)}::{folder}::{_html_escape(cls.name)}</p>'
        f'<h2>Definition</h2>{definition_html}'
        f'{union_note_html}'
        f'<h2>Diagram</h2>{diagram_html or "<p class=\"empty\">No diagram available.</p>"}'
        f'{body_extra}'
    )
    breadcrumb = [
        ("Profiles", "../index.html"),
        (profile_name, "index.html"),
        (folder, f"{folder}/index.html"),
        (cls.name, ""),
    ]
    html = _html_page_shell(cls.name, profile_name, breadcrumb, body, depth=2)
    out_path = profile_dir / folder / f"{_puml_safe(cls.name)}.html"
    out_path.write_text(html, encoding="utf-8")


def _html_folder_index(folder: str, items: list[UmlClass],
                       profile_name: str, profile_dir: Path,
                       inherited_class_names: Optional[set[str]] = None) -> None:
    """Index page for Classes/ or DataTypes/."""
    inh = inherited_class_names or set()
    lis = []
    for cls in sorted(items, key=lambda c: c.name):
        kind_label = f' <span class="kind">{cls.kind}</span>' if cls.kind != "class" else ""
        inh_label = ' <span class="kind">inherited</span>' if cls.name in inh else ""
        lis.append(f'<li><a href="{_puml_safe(cls.name)}.html">'
                   f'{_html_escape(cls.name)}</a>{kind_label}{inh_label}</li>')
    body = (f'<h1 class="classname">{folder}</h1>'
            f'<p>{len(items)} item(s) in <code>{_html_escape(profile_name)}</code></p>'
            f'<ul class="classlist">{"".join(lis)}</ul>')
    breadcrumb = [
        ("Profiles", "../index.html"),
        (profile_name, "index.html"),
        (folder, ""),
    ]
    html = _html_page_shell(folder, profile_name, breadcrumb, body, depth=2)
    (profile_dir / folder / "index.html").write_text(html, encoding="utf-8")


def _html_profile_index(profile_name: str, closure: UmlClosure,
                        profile_dir: Path, puml_dir: Optional[Path],
                        inherited_class_names: Optional[set[str]] = None,
                        composes_chain: Optional[list[str]] = None,
                        cross_profile_registry: Optional[dict[str, str]] = None) -> None:
    registry = cross_profile_registry or {}

    def _copy_overview(stem: str) -> Optional[Path]:
        """Copy <stem>.pu/.svg from puml_dir next to the index and return the
        local .pu path (so the diagram is self-contained), or None if absent.
        If the source is gone, remove any stale copy next to the index."""
        dst_pu = profile_dir / f"{stem}.pu"
        src_pu = puml_dir / f"{stem}.pu" if puml_dir is not None else None
        if src_pu is None or not src_pu.exists():
            for stale in (dst_pu, dst_pu.with_suffix(".svg")):
                if stale.exists():
                    stale.unlink()
            return None
        if not dst_pu.exists() or dst_pu.read_text(encoding="utf-8") != src_pu.read_text(encoding="utf-8"):
            dst_pu.write_text(src_pu.read_text(encoding="utf-8"), encoding="utf-8")
        src_svg = src_pu.with_suffix(".svg")
        if src_svg.exists():
            (profile_dir / f"{stem}.svg").write_bytes(src_svg.read_bytes())
        return dst_pu

    overview_html = ""
    full_pu = _copy_overview("index")
    if full_pu is not None:
        # The simplified overview is emitted only when it is meaningful (see
        # emit_puml); its presence is the signal to offer the toggle.
        local_pu = _copy_overview("index.local")
        # Color legend: profiles actually present among the overview classes.
        present: dict[str, int] = {}
        for c in closure.classes:
            prof = registry.get(c.name, profile_name)
            present[prof] = present.get(prof, 0) + 1
        order = _PROFILE_ORDER + [p for p in sorted(present) if p not in _PROFILE_ORDER]
        legend_items = "".join(
            f'<span class="legend-item"><span class="legend-swatch" '
            f'style="background:{_PROFILE_COLORS.get(p, _PROFILE_COLOR_DEFAULT)}"></span>'
            f'{_html_escape(p)} ({present[p]})</span>'
            for p in order if p in present)
        legend_html = f'<div class="diagram-legend">{legend_items}</div>'
        toggle_html = ""
        local_html = ""
        if local_pu is not None:
            toggle_html = ('<button type="button" class="overview-toggle" '
                           'data-overview-toggle>Hide inherited (Core/Discovery)</button>')
            local_html = _html_render_diagram(local_pu, div_id="overview-local", hidden=True)
        full_html = _html_render_diagram(full_pu, div_id="overview-full")
        # Controls in a sticky bar so the toggle + legend stay visible while the
        # (tall, interactive) diagram is on screen — e.g. after the diagram Back
        # button restores scroll to the diagram. The bar un-pins once you scroll
        # past the overview section into the class lists.
        controls_html = (f'<div class="overview-controls">{toggle_html}'
                         f'{legend_html}</div>')
        overview_html = (f'<div class="overview-section">{controls_html}'
                         f'{full_html}{local_html}</div>')

    inh = inherited_class_names or set()

    def _badge(name: str) -> str:
        return ' <span class="kind">inherited</span>' if name in inh else ""

    cls_lis = "".join(
        f'<li><a href="Classes/{_puml_safe(c.name)}.html">{_html_escape(c.name)}</a>{_badge(c.name)}</li>'
        for c in sorted(closure.classes, key=lambda c: c.name))
    dt_items = closure.datatypes + closure.enumerations
    dt_lis = "".join(
        f'<li><a href="DataTypes/{_puml_safe(d.name)}.html">{_html_escape(d.name)}</a>'
        f' <span class="kind">{d.kind}</span>{_badge(d.name)}</li>'
        for d in sorted(dt_items, key=lambda c: c.name))

    # Composition note: if this profile composes any bases, surface that prominently.
    compose_html = ""
    if composes_chain:
        # Each entry is a path like "ddi-cdi2cdifDiscovery_mapping.json"; map to a
        # profile name by stripping the prefix/suffix.
        def _profile_from_path(p: str) -> str:
            stem = p.rsplit("/", 1)[-1].removeprefix("ddi-cdi2").removesuffix("_mapping.json")
            # CamelCase the slug: cdifCore -> CDIFCore (heuristic for our naming)
            if stem.startswith("cdif"):
                stem = "CDIF" + stem[4:]
            return stem
        names = [_profile_from_path(p) for p in composes_chain]
        links = ", ".join(
            f'<a class="classlink" href="../{n}/index.html">{_html_escape(n)}</a>'
            for n in names
        )
        compose_html = (
            f'<div class="definition" style="border-left-color:#2e7d32">'
            f'<strong>Composes:</strong> this profile inherits classes and '
            f'associations from {links}. Inherited entries are marked below '
            f'and on each per-class page; the cross-profile link on the badge '
            f'jumps to the canonical definition in the originating profile. '
            f'The full graph shown here matches the composed JSON Schema '
            f'(resolvedSchema.json) for this profile.</div>'
        )

    body = (
        f'<h1 class="classname">{_html_escape(profile_name)}</h1>'
        f'<p>{len(closure.classes)} classes, {len(dt_items)} datatypes/enumerations'
        + (f' ({sum(1 for c in closure.classes if c.name in inh)} class(es) inherited)' if inh else "")
        + '.</p>'
        f'{compose_html}'
        f'<h2>Model overview</h2>{overview_html or "<p class=\"empty\">No overview diagram.</p>"}'
        f'<h2>Classes</h2><ul class="classlist">{cls_lis}</ul>'
        f'<h2>DataTypes &amp; Enumerations</h2><ul class="classlist">{dt_lis}</ul>'
    )
    breadcrumb = [("Profiles", "index.html"), (profile_name, "")]
    html = _html_page_shell(profile_name, profile_name, breadcrumb, body, depth=1)
    (profile_dir / "index.html").write_text(html, encoding="utf-8")


def _html_root_index(out_dir: Path, profiles: list[tuple[str, int, int]]) -> None:
    """Top-level index listing all profiles, split into 'Composite Profiles' and
    'Profiles' (modules) sections. `profiles` items are (name, n_classes, n_datatypes).

    Categorization is read from `<out_dir>/_categories.json`, a {umlName: kind}
    map written by the build orchestrator (build-docs.ps1) where kind is one of
    'composite' or 'module'. Unknown / missing entries default to 'module'.
    """
    import json as _json
    cat_path = out_dir / "_categories.json"
    categories = {}
    if cat_path.exists():
        try:
            categories = _json.loads(cat_path.read_text(encoding="utf-8"))
        except Exception:
            categories = {}

    def _li(name: str, nc: int, nd: int) -> str:
        return (f'<li><a href="{name}/index.html"><strong>{_html_escape(name)}</strong></a>'
                f' &mdash; {nc} classes, {nd} datatypes</li>')

    composite = sorted([p for p in profiles if categories.get(p[0]) == "composite"])
    modules   = sorted([p for p in profiles if categories.get(p[0]) != "composite"])

    composite_html = ("".join(_li(*p) for p in composite)
                      if composite else '<li class="empty">(none)</li>')
    module_html    = ("".join(_li(*p) for p in modules)
                      if modules else '<li class="empty">(none)</li>')

    body = (
        '<h1 class="classname">CDIF JSON schema diagrams</h1>'
        '<p>Generated from the CDIF profile UML models.</p>'
        '<h2>Metadata document schema</h2>'
        '<p>Schema for instance documents, composed from profile modules.</p>'
        f'<ul class="classlist">{composite_html}</ul>'
        '<h2>Profiles</h2>'
        '<p>Each profile supports specific functionality. Profiles are assembled to create the Instance document schema.</p>'
        f'<ul class="classlist">{module_html}</ul>'
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CDIF JSON schema diagrams</title>
  <link rel="stylesheet" href="_static/style.css">
</head>
<body>
<header>
  <h1>CDIF JSON schema diagrams</h1>
</header>
<main>{body}</main>
</body>
</html>
"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def emit_html(out_dir: Path, *,
              profile_name: str,
              roots: list[UmlClass],
              ctx: BuildContext,
              model: Model,
              puml_dir: Optional[Path] = None,
              cross_profile_registry: Optional[dict[str, str]] = None,
              inherited_class_names: Optional[set[str]] = None,
              composes_chain: Optional[list[str]] = None) -> dict:
    """Emit an HTML model browser for one profile.

    Layout under <out_dir>:
      _static/style.css            (shared CSS, written once)
      index.html                   (top-level profile index — overwritten each call)
      <profile>/index.html         (per-profile overview)
      <profile>/Classes/*.html
      <profile>/DataTypes/*.html

    puml_dir: optional directory holding the .pu (and possibly .svg) files
    produced by emit_puml. Diagrams are copied next to their HTML so the
    pages are self-contained.
    """
    closure = compute_uml_closure(roots, ctx, model)
    out_dir.mkdir(parents=True, exist_ok=True)
    static_dir = out_dir / "_static"
    static_dir.mkdir(exist_ok=True)
    (static_dir / "style.css").write_text(_HTML_STYLE_CSS, encoding="utf-8")
    (static_dir / "diagram.js").write_text(_HTML_DIAGRAM_JS, encoding="utf-8")

    profile_dir = out_dir / profile_name
    (profile_dir / "Classes").mkdir(parents=True, exist_ok=True)
    (profile_dir / "DataTypes").mkdir(parents=True, exist_ok=True)

    # Sweep stale per-class artifacts. emit_html is incremental — without this,
    # classes/datatypes that were in the closure on a previous run but are no
    # longer referenced would linger as orphaned .html/.pu/.svg files (unreached
    # by any index but still on disk and bookmark-resolvable). Sweep only the
    # known per-class file extensions; index.html etc. are overwritten below.
    for sub in ("Classes", "DataTypes"):
        for stale in (profile_dir / sub).iterdir():
            if stale.is_file() and stale.suffix in {".html", ".pu", ".svg"} and stale.name != "index.html":
                stale.unlink()

    inherited = inherited_class_names or set()
    for cls in closure.classes:
        _html_class_page(cls, closure, model, profile_name, profile_dir,
                         puml_dir, cross_profile_registry,
                         inherited=cls.name in inherited)
    for dt in closure.datatypes + closure.enumerations:
        _html_class_page(dt, closure, model, profile_name, profile_dir,
                         puml_dir, cross_profile_registry,
                         inherited=dt.name in inherited)

    _html_folder_index("Classes", closure.classes, profile_name, profile_dir,
                       inherited_class_names=inherited)
    _html_folder_index("DataTypes",
                       closure.datatypes + closure.enumerations,
                       profile_name, profile_dir,
                       inherited_class_names=inherited)
    _html_profile_index(profile_name, closure, profile_dir, puml_dir,
                        inherited_class_names=inherited,
                        composes_chain=composes_chain or [],
                        cross_profile_registry=cross_profile_registry)

    # Refresh the root index by scanning sibling profile dirs. Skip sibling
    # build-intermediate dirs (_static for CSS, _plantuml for .pu/.svg sources).
    profiles = []
    for sub in sorted(out_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        classes_dir = sub / "Classes"
        dtypes_dir = sub / "DataTypes"
        nc = len(list(classes_dir.glob("*.html"))) - 1 if classes_dir.exists() else 0
        nd = len(list(dtypes_dir.glob("*.html"))) - 1 if dtypes_dir.exists() else 0
        profiles.append((sub.name, max(nc, 0), max(nd, 0)))
    _html_root_index(out_dir, profiles)

    return {
        "profile": profile_name,
        "out_dir": str(profile_dir),
        "classes": len(closure.classes),
        "datatypes": len(closure.datatypes) + len(closure.enumerations),
    }


def _load_with_composition(config_path: Path,
                           _seen: Optional[set] = None) -> tuple[dict, list[str]]:
    """Load a ucmism2m config and recursively merge in any base configs named
    in transformation.targetModel.composes. Returns (effective_cfg, inherited_classnames).

    Composition (v1.1):
      - For each base path, recursively expand (so chains like
        DataStructure -> DataDescription -> Discovery -> Core work).
      - Classes: same targetClass union attribute lists (local wins on name
        conflict). Generalizations are unioned. The local class's other
        fields (sourceClass, definition, isAbstract, etc.) win.
      - Associations: added when targetAssociationName isn't already present.
      - Packages: added when name isn't already present.

    `inherited_classnames` is the list of class names that came in from a base
    config (i.e. were not declared in this config's own mapping.class). The
    HTML emitter uses it to badge "inherited from <profile>" on those entries.
    """
    if _seen is None:
        _seen = set()
    config_path = config_path.resolve()
    if config_path in _seen:
        raise RuntimeError(f"Circular composes: {config_path}")
    _seen.add(config_path)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    compose_list = (cfg.get("transformation", {})
                       .get("targetModel", {})
                       .get("composes", [])) or []
    if not compose_list:
        return cfg, []

    # Local classes (this profile's own) — keep their names so we know what's
    # inherited vs locally introduced.
    own_class_names = {c.get("targetClass") for c in cfg.get("mapping", {}).get("class", []) or []
                       if c.get("targetClass")}

    # Build effective cfg starting from a deep copy of the local one
    effective = copy.deepcopy(cfg)
    classes_by_name: "OrderedDict[str, dict]" = OrderedDict()
    for c in effective.get("mapping", {}).get("class", []) or []:
        tn = c.get("targetClass")
        if tn:
            classes_by_name[tn] = c
    associations_by_name: "OrderedDict[str, dict]" = OrderedDict()
    for a in effective.get("mapping", {}).get("association", []) or []:
        an = a.get("targetAssociationName") or a.get("sourceAssociationName")
        if an:
            associations_by_name[an] = a
    packages_by_name: "OrderedDict[str, dict]" = OrderedDict()
    for p in effective.get("package", []) or []:
        if p.get("name"):
            packages_by_name[p["name"]] = p

    inherited: list[str] = []

    # Effective datatype substitutions / excludes start from this config
    effective_subs: dict = dict(effective.get("transformation", {})
                                         .get("datatypeSubstitutions", {}) or {})
    effective_excludes: set = set(effective.get("transformation", {})
                                            .get("excludeDatatypes", []) or [])

    for base_rel in compose_list:
        base_path = (config_path.parent / base_rel).resolve()
        base_cfg, base_inherited = _load_with_composition(base_path, _seen)

        # Substitutions: base first, local overrides on key conflict
        base_subs = (base_cfg.get("transformation", {})
                              .get("datatypeSubstitutions", {})) or {}
        merged_subs = dict(base_subs)
        merged_subs.update(effective_subs)
        effective_subs = merged_subs
        # Excludes: union
        effective_excludes |= set(
            (base_cfg.get("transformation", {}).get("excludeDatatypes", [])) or []
        )

        # Merge classes
        for bc in base_cfg.get("mapping", {}).get("class", []) or []:
            tn = bc.get("targetClass")
            if not tn:
                continue
            if tn in classes_by_name:
                # MERGE: union attributes (local wins on name conflict) +
                # union generalizations
                local = classes_by_name[tn]
                local_attr_names = {a.get("name") for a in (local.get("attribute") or [])}
                merged_attrs = list(local.get("attribute") or [])
                for ba in bc.get("attribute") or []:
                    if ba.get("name") not in local_attr_names:
                        merged_attrs.append(ba)
                local["attribute"] = merged_attrs
                local_gen = set(local.get("generalization") or [])
                base_gen = set(bc.get("generalization") or [])
                if local_gen | base_gen:
                    local["generalization"] = sorted(local_gen | base_gen)
                # Carry isAbstract from base if local doesn't set it
                if "isAbstract" not in local and bc.get("isAbstract"):
                    local["isAbstract"] = bc["isAbstract"]
                # Carry isDataType from base if local doesn't set it (so e.g.
                # Core's Identifier/Reference DataType decision cascades into a
                # profile that declares its own local stub of the same name).
                if "isDataType" not in local and bc.get("isDataType"):
                    local["isDataType"] = bc["isDataType"]
            else:
                classes_by_name[tn] = copy.deepcopy(bc)
                if tn not in own_class_names:
                    inherited.append(tn)

        # Merge associations (first-wins by targetAssociationName)
        for ba in base_cfg.get("mapping", {}).get("association", []) or []:
            an = ba.get("targetAssociationName") or ba.get("sourceAssociationName")
            if an and an not in associations_by_name:
                associations_by_name[an] = copy.deepcopy(ba)

        # Merge packages (first-wins by name) — note: parent will still point
        # at the base's mainPackage name, which won't match this profile's main
        # package. We rewrite the parent so packages nest cleanly.
        local_main = effective["transformation"]["targetModel"]["mainPackage"]
        base_main = base_cfg["transformation"]["targetModel"]["mainPackage"]
        for bp in base_cfg.get("package", []) or []:
            name = bp.get("name")
            if not name or name in packages_by_name:
                continue
            np = copy.deepcopy(bp)
            if np.get("parent") == base_main:
                np["parent"] = local_main
            packages_by_name[name] = np

        # Cascade inherited list from base
        for bn in base_inherited:
            if bn not in own_class_names and bn not in inherited:
                inherited.append(bn)

    # Reassemble
    effective.setdefault("mapping", {})["class"] = list(classes_by_name.values())
    effective["mapping"]["association"] = list(associations_by_name.values())
    effective["package"] = list(packages_by_name.values())
    # Carry the merged substitutions/excludes back onto the effective cfg
    if effective_subs:
        effective.setdefault("transformation", {})["datatypeSubstitutions"] = effective_subs
    if effective_excludes:
        effective.setdefault("transformation", {})["excludeDatatypes"] = sorted(effective_excludes)
    return effective, inherited


_UML_PRIMITIVE_NAMES = {"String", "Integer", "Boolean", "Real"}


def _apply_datatype_substitutions(
    target_classes_by_name: dict[str, "UmlClass"],
    source_model: "Model",
    substitutions: dict[str, str],
    excludes: set[str],
) -> tuple[int, int]:
    """For every synthetic target class's property:
      * if the property's type points at a source DataType named in `excludes`,
        drop the property entirely;
      * if the property's type points at a source DataType named in `substitutions`,
        replace the type with the substitution (a UML primitive or a local
        target class id).
    Returns (substitution_count, drop_count).
    """
    if not substitutions and not excludes:
        return 0, 0

    # Pre-compute lookups: source DataType name -> (replacement_target_id, replacement_primitive)
    name_to_replacement: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for src_name, repl in substitutions.items():
        if repl in _UML_PRIMITIVE_NAMES:
            # Map e.g. "String" -> JSON-schema-style "string"
            prim = PRIMITIVE_FRAGMENTS.get(repl, "string")
            name_to_replacement[src_name] = (None, prim)
        elif repl in target_classes_by_name:
            name_to_replacement[src_name] = (target_classes_by_name[repl].id, None)
        else:
            # Replacement target not present in this profile's class set —
            # leave the property typed as the source DataType (no-op).
            pass

    subs_count = 0
    drop_count = 0
    for cls in target_classes_by_name.values():
        new_props: list[Property] = []
        for p in cls.properties:
            if p.type_id is None or p.is_assoc_end:
                new_props.append(p)
                continue
            target = source_model.elements.get(p.type_id)
            if target is None:
                new_props.append(p)
                continue
            if target.name in excludes:
                drop_count += 1
                continue
            repl = name_to_replacement.get(target.name)
            if repl is not None:
                tid, prim = repl
                new_props.append(Property(
                    id=p.id, name=p.name,
                    type_id=tid, primitive=prim,
                    lower=p.lower, upper=p.upper,
                    doc=p.doc, is_assoc_end=p.is_assoc_end,
                    aggregation=p.aggregation,
                ))
                subs_count += 1
                continue
            new_props.append(p)
        cls.properties = new_props
    return subs_count, drop_count


def emit_uml_from_config(
    config_path: Path,
    source_model: Model,
    out_path: Path,
    ctx_overrides: Optional[dict] = None,
    output_format: str = "canonical",
) -> None:
    """Read a ucmism2m JSON config and emit a profile UML.

    The config drives:
      - target model metadata (acronym, URI, packages)
      - class set (map / new / merge)
      - per-attribute renames + multiplicity overrides
      - association edges (renamed source associations and brand-new ones)
      - v1.1 composes: recursive merge from base profile configs
    """
    cfg, inherited_class_names = _load_with_composition(config_path)

    # Rewrite `definition` fields into RST-formatted documentation bodies,
    # merging in source-vocabulary text (schema.org, SKOS, PROV, DCAT,
    # DCTerms, DQV) and CDIF descriptions from the profile's
    # resolvedSchema.json. No-ops if the vocab bundle isn't installed under
    # tools/vocabularies/ yet.
    try:
        from rst_augment import maybe_augment
        maybe_augment(cfg, config_path)
    except ImportError:
        pass

    tm = cfg["transformation"]["targetModel"]
    acronym = tm["acronym"]
    profile_uri = tm["uri"]
    model_name = tm.get("mainPackage", acronym)
    main_pkg = tm["mainPackage"]

    # Pre-pass: register every target class id by name so forward dataType
    # references resolve correctly. Without this, a class declared earlier in
    # the config that references a target type declared later (e.g. a "stub"
    # appended at the end of the array) would fall through _resolve_dataType_ref
    # to the source DDI-CDI namespace and pull in unrelated DataType cascades.
    # Stubs are overwritten with the fully rendered class in the main pass below.
    target_classes_by_name: dict[str, UmlClass] = {}
    for spec in cfg.get("mapping", {}).get("class", []):
        target_name = spec.get("targetClass")
        if not target_name:
            continue
        tgt_id = _profile_xmi_id(acronym, target_name)
        target_classes_by_name[target_name] = UmlClass(
            id=tgt_id, name=target_name, package="", doc=None,
            is_abstract=False, parents=[], properties=[],
            kind="class", literals=[],
        )

    # Main pass: render synthetic target classes (overwrites the stubs)
    target_class_specs: list[tuple[dict, str]] = []  # (spec, mapping_type) preserves order
    for spec in cfg.get("mapping", {}).get("class", []):
        mt = spec["mappingType"]
        if mt == "map":
            cls = _render_map_class(spec, source_model, target_classes_by_name, acronym)
        elif mt == "merge":
            cls = _render_merge_class(spec, source_model, target_classes_by_name, acronym)
        elif mt == "new":
            cls = _render_new_class(spec, source_model, target_classes_by_name, acronym)
        else:
            continue
        target_classes_by_name[cls.name] = cls
        target_class_specs.append((spec, mt))

    # Resolve generalization names → parent ids (v1.1). Done after every
    # target class is rendered so forward refs (e.g. Person before Agent) work.
    for spec, _mt in target_class_specs:
        gens = spec.get("generalization", []) or []
        if not gens:
            continue
        target_cls = target_classes_by_name.get(spec["targetClass"])
        if target_cls is None:
            continue
        for parent_name in gens:
            parent_cls = target_classes_by_name.get(parent_name)
            if parent_cls is None:
                print(
                    f"WARN: generalization '{spec['targetClass']}' -> "
                    f"'{parent_name}' skipped — parent class is not defined "
                    "in this configuration.",
                    file=sys.stderr,
                )
                continue
            if parent_cls.id not in target_cls.parents:
                target_cls.parents.append(parent_cls.id)

    # Apply v1.1 datatype substitutions and excludes BEFORE associations are
    # rendered so substituted attrs are settled. Substitutions/excludes are
    # already merged through the composition chain by _load_with_composition.
    subs = cfg.get("transformation", {}).get("datatypeSubstitutions", {}) or {}
    excludes = set(cfg.get("transformation", {}).get("excludeDatatypes", []) or [])
    n_subs, n_drops = _apply_datatype_substitutions(
        target_classes_by_name, source_model, subs, excludes
    )
    if n_subs or n_drops:
        print(f"datatype substitutions: {n_subs} retyped, {n_drops} dropped",
              file=sys.stderr)

    # Render associations as synthetic assoc-end Properties on the relevant
    # target class(es)
    for assoc in cfg.get("mapping", {}).get("association", []):
        _add_association_property(assoc, target_classes_by_name,
                                  source_model, acronym)

    # Drop properties explicitly excluded for a given owner class
    # (transformation.excludeProperties: ["ClassName.propertyName", ...]).
    # Applied to the target class before the closure/emit so the diagram, the
    # XMI owned attributes, and the XMI association elements all omit them
    # consistently (used e.g. to drop source-map associations that are not part
    # of the CDIF profile, such as RepresentedVariable.takesSubstantiveConceptsFrom).
    exclude_props = set(cfg.get("transformation", {}).get("excludeProperties", []) or [])
    for spec in exclude_props:
        cname, _, pname = spec.partition(".")
        tcls = target_classes_by_name.get(cname)
        if tcls is not None:
            kept = [p for p in tcls.properties if p.name != pname]
            if len(kept) != len(tcls.properties):
                tcls.properties = kept
                print(f"excludeProperties: dropped {spec}", file=sys.stderr)

    # Build a *merged* Model containing the source elements + the synthetic
    # target classes. The closure walker uses this when chasing property
    # type_ids (most of which point at source DataTypes).
    merged_elements: dict[str, UmlClass] = dict(source_model.elements)
    merged_name_to_id: dict[str, list[str]] = {k: list(v) for k, v in source_model.name_to_id.items()}
    for cls in target_classes_by_name.values():
        merged_elements[cls.id] = cls
        merged_name_to_id.setdefault(cls.name, []).insert(0, cls.id)
    merged_model = Model(elements=merged_elements, name_to_id=merged_name_to_id)

    # Build a BuildContext: inline EVERY target class, since they are all
    # part of the profile model (and the closure walker uses inline_class_names
    # to decide whether to recurse into a referenced class's properties).
    inline_names = set(target_classes_by_name.keys())
    if ctx_overrides:
        inline_names |= set(ctx_overrides.get("inline", set()))

    ctx = BuildContext(
        model=merged_model,
        prefix="cdif",
        shared_bb_relpath="",
        shared_defs=set(),
        inline_class_names=inline_names,
        inline_or_ref_class_names=set(),
        reference_class_names=set(),
        exclude_class_names=set((ctx_overrides or {}).get("exclude_class", set())),
        exclude_property_specs=exclude_props,
        inline_datatypes=False,
        strict_required=False,
        external_class_refs={},
        local_defs=OrderedDict(),
        expanding=set(),
        datatype_substitutions=subs,
        exclude_datatypes=excludes,
        target_classes_by_name=target_classes_by_name,
    )

    # The roots are the synthetic target classes
    roots = list(target_classes_by_name.values())

    if output_format == "ea":
        emit_ea_xmi(
            out_path,
            acronym=acronym,
            profile_uri=profile_uri,
            model_name=model_name,
            model_definition=tm.get("definition", ""),
            main_package_name=main_pkg,
            main_package_definition=tm.get("mainPackageDefinition", ""),
            roots=roots,
            ctx=ctx,
            model=merged_model,
        )
    elif output_format == "puml":
        origin = (ctx_overrides or {}).get("cross_profile_registry") if ctx_overrides else None
        manifest = emit_puml(
            out_path,
            profile_name=model_name,
            roots=roots,
            ctx=ctx,
            model=merged_model,
            origin=origin,
        )
        print(
            f"PlantUML: {manifest['classes']} classes, "
            f"{manifest['datatypes']} datatypes in {out_path}",
            file=sys.stderr,
        )
    elif output_format == "html":
        puml_dir = (ctx_overrides or {}).get("puml_dir") if ctx_overrides else None
        registry = (ctx_overrides or {}).get("cross_profile_registry") if ctx_overrides else None
        # Names of classes inherited (via composes) so the HTML browser can
        # badge them; also pass the composes chain for the profile index note.
        composes_chain = (cfg.get("transformation", {})
                            .get("targetModel", {})
                            .get("composes", [])) or []
        manifest = emit_html(
            out_path,
            profile_name=model_name,
            roots=roots,
            ctx=ctx,
            model=merged_model,
            puml_dir=puml_dir,
            cross_profile_registry=registry,
            inherited_class_names=set(inherited_class_names),
            composes_chain=composes_chain,
        )
        print(
            f"HTML: {manifest['classes']} classes, "
            f"{manifest['datatypes']} datatypes in {manifest['out_dir']}",
            file=sys.stderr,
        )
    else:
        model_identification = {
            "prefix": ctx.prefix or "cdif",
            "majorVersion": tm.get("majorVersion"),
            "minorVersion": tm.get("minorVersion"),
            "title": tm.get("modelTitle"),
            "language": tm.get("language"),
            "uri": profile_uri,
        }
        emit_uml(
            out_path,
            acronym=acronym,
            profile_uri=profile_uri,
            model_name=model_name,
            model_definition=tm.get("definition", ""),
            main_package_name=main_pkg,
            main_package_definition=tm.get("mainPackageDefinition", ""),
            classes_package_name="Classes",
            classes_package_definition="Target classes of the CDIF profile.",
            roots=roots,
            ctx=ctx,
            model=merged_model,
            model_identification=model_identification,
        )


if __name__ == "__main__":
    sys.exit(main())
