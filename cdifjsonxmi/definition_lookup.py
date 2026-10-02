"""Definition lookup for source-vocabulary terms.

Load canonical definitions from:
  - schema.org JSON-LD (@graph)
  - PROV / DCAT / DCTerms / DQV Turtle files (cached under vocabularies/ beside this script)
  - SKOS RDF/XML

For each URI, return:
  {
    "label":         "Human-readable label",
    "definition":    "rdfs:comment text",
    "parents":       [{"@id": "...", "label": "..."}, ...],
    "children":      [{"@id": "...", "label": "..."}, ...],  # only if the graph
                                                              # ships subclass links
                                                              # (schema.org does; W3C
                                                              # vocabs usually don't).
    "ranges":        [{"@id": "...", "label": "..."}, ...],  # for properties
    "domains":       [{"@id": "...", "label": "..."}, ...],  # for properties
  }

Only terms that appear in one of the loaded graphs get a lookup entry;
unknown URIs return None.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

try:
    from rdflib import Graph, URIRef, RDF, RDFS, OWL, Namespace
    _SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
    _PROV = Namespace("http://www.w3.org/ns/prov#")
    # schema.org's canonical namespace is now https:// (both http:// and https://
    # are served, but Turtle files including GeoSPARQL use the https:// form).
    _SCHEMA = Namespace("https://schema.org/")
    # Predicates that different vocabularies use for the "definition text".
    # Try rdfs:comment first (standard); fall back to vocabulary-specific ones.
    # GeoSPARQL uses schema:description; PROV uses prov:definition; SKOS uses
    # skos:definition. Order matters — first hit wins per term.
    _DEFINITION_PREDS = (RDFS.comment, _SKOS.definition, _PROV.definition,
                         _SCHEMA.description)
except ImportError:  # pragma: no cover
    Graph = None  # type: ignore
    _DEFINITION_PREDS = ()

_VOCAB_DIR = Path(__file__).parent / "vocabularies"

SCHEMA_ORG_FILE = _VOCAB_DIR / "schema.org" / "schemaorg-all-https202607.jsonld"

# DDI-CDI canonical XMI (source of the DDI-CDI reference model). The generator
# is invoked with the same file via --xmi, but the lookup loader reads it
# independently so definitions are available even in --emit-html or other
# code paths where the XMI parse hasn't happened yet.
DDI_CDI_XMI = Path(r"C:\GithubC\ucmis.m2t\model\ddi-cdi_1-1beta_canonical-unique-names.xmi")

# Turtle/RDF cache paths and the RDF format each is stored in.
_TURTLE_SOURCES = [
    (_VOCAB_DIR / "prov" / "prov.ttl", "turtle"),
    (_VOCAB_DIR / "dcat" / "dcat.ttl", "turtle"),
    (_VOCAB_DIR / "dcterms" / "dcterms.ttl", "turtle"),
    (_VOCAB_DIR / "dqv" / "dqv.ttl", "turtle"),
    (_VOCAB_DIR / "spdx" / "spdx.ttl", "turtle"),
    (_VOCAB_DIR / "geosparql" / "geosparql.ttl", "turtle"),
    (_VOCAB_DIR / "skos" / "skos.rdf", "xml"),
]

# schema.org uses `schema:` as a JSON-LD prefix for `https://schema.org/`; we
# canonicalize on the full URI form here (Achim's tool follows the same URI form).
_SCHEMA_PREFIX_EXPANSIONS = {
    "schema:":  "https://schema.org/",
    "skos:":    "http://www.w3.org/2004/02/skos/core#",
    "dcterms:": "http://purl.org/dc/terms/",
    "dcat:":    "http://www.w3.org/ns/dcat#",
    "prov:":    "http://www.w3.org/ns/prov#",
    "dqv:":     "http://www.w3.org/ns/dqv#",
    "spdx:":    "http://spdx.org/rdf/terms#",
    "time:":    "http://www.w3.org/2006/time#",
    "geo:":       "http://www.opengis.net/ont/geosparql#",
    "geosparql:": "http://www.opengis.net/ont/geosparql#",  # alternate common prefix
    "cdi:":     "http://ddialliance.org/Specification/DDI-CDI/1.0/RDF/",
    "rdfs:":    "http://www.w3.org/2000/01/rdf-schema#",
    "rdf:":     "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "owl:":     "http://www.w3.org/2002/07/owl#",
}


def _expand(term_id: str) -> str:
    """Expand a compact prefix URI (`schema:Dataset`) to a full URI."""
    for prefix, expansion in _SCHEMA_PREFIX_EXPANSIONS.items():
        if term_id.startswith(prefix):
            return expansion + term_id[len(prefix):]
    return term_id


def _collect_refs(value) -> list[str]:
    """Turn a JSON-LD value (single obj, list of objs, or plain URI) into a
    list of full URIs."""
    out: list[str] = []
    if value is None:
        return out
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, dict) and "@id" in item:
            out.append(_expand(item["@id"]))
        elif isinstance(item, str):
            out.append(_expand(item))
    return out


def _load_schema_org(lookup: dict[str, dict], schema_org_path: Path) -> None:
    """Load schema.org from JSON-LD @graph into the lookup dict."""
    if not schema_org_path.exists():
        return
    with open(schema_org_path, encoding="utf-8") as f:
        doc = json.load(f)
    for term in doc.get("@graph", []):
        tid = term.get("@id")
        if not tid:
            continue
        full_id = _expand(tid)
        entry: dict = {
            "label": term.get("rdfs:label"),
            "definition": term.get("rdfs:comment"),
            "parents": _collect_refs(term.get("rdfs:subClassOf")),
            "ranges": _collect_refs(term.get("schema:rangeIncludes")),
            "domains": _collect_refs(term.get("schema:domainIncludes")),
            "source": "schema.org",
        }
        # Some rdfs:label values are {"@language":"en","@value":"..."} dicts
        if isinstance(entry["label"], dict):
            entry["label"] = entry["label"].get("@value")
        if isinstance(entry["definition"], dict):
            entry["definition"] = entry["definition"].get("@value")
        lookup[full_id] = entry


def _load_rdf(lookup: dict[str, dict], path: Path, fmt: str) -> None:
    """Load a W3C vocabulary from Turtle / RDF-XML using rdflib."""
    if Graph is None or not path.exists():
        return
    g = Graph()
    try:
        g.parse(str(path), format=fmt)
    except Exception as e:  # pragma: no cover
        print(f"  warning: could not parse {path.name}: {e}")
        return

    # For every subject that is a Class or a Property, extract label / comment /
    # subClassOf (for classes) and range / domain (for properties).
    # Label predicates: rdfs:label first; GeoSPARQL uses schema:name.
    label_preds = (RDFS.label, _SCHEMA.name)
    for s in set(g.subjects()):
        if not isinstance(s, URIRef):
            continue
        types = list(g.objects(s, RDF.type))
        is_class = any(t in (RDFS.Class, OWL.Class) or str(t).endswith("Class") for t in types)
        is_prop = any(str(t).endswith("Property") for t in types)
        if not (is_class or is_prop):
            continue
        label = None
        for lp in label_preds:
            label = _rdfs_literal(g, s, lp)
            if label:
                break
        comment = None
        for pred in _DEFINITION_PREDS:
            comment = _rdfs_literal(g, s, pred)
            if comment:
                break
        entry: dict = {
            "label": label,
            "definition": comment,
            "parents": [str(o) for o in g.objects(s, RDFS.subClassOf) if isinstance(o, URIRef)],
            "ranges": [str(o) for o in g.objects(s, RDFS.range) if isinstance(o, URIRef)],
            "domains": [str(o) for o in g.objects(s, RDFS.domain) if isinstance(o, URIRef)],
            "source": path.parent.name,
        }
        lookup[str(s)] = entry


def _rdfs_literal(g, subject, predicate) -> Optional[str]:
    """Return the English-preferred string literal for (subject, predicate)."""
    en_val = None
    fallback = None
    for o in g.objects(subject, predicate):
        val = str(o)
        lang = getattr(o, "language", None)
        if lang == "en":
            en_val = val
            break
        if fallback is None:
            fallback = val
    return en_val or fallback


def _load_ddi_cdi(lookup: dict[str, dict], xmi_path: Path) -> None:
    """Walk the DDI-CDI canonical XMI and populate the lookup with each class's
    <name>+<ownedComment><body>. Keyed by the compact-URI expansion
    `cdi:<ClassName>` -> full URI `http://ddialliance.org/.../RDF/<ClassName>`.

    Attributes appear as separate entries keyed `cdi:<ClassName>-<attrName>`
    (the same tail as the generator uses for `<ownedAttribute>` xmi:ids).
    The canonical XMI's structure puts <ownedComment> BEFORE <name> as
    children of <packagedElement>, so we walk each class packagedElement
    to find both."""
    if not xmi_path.exists():
        return
    try:
        import xml.etree.ElementTree as ET
    except ImportError:  # pragma: no cover
        return
    ns = {"uml": "http://www.eclipse.org/uml2/5.0.0/UML",
          "xmi": "http://www.omg.org/spec/XMI/20131001"}
    try:
        root = ET.parse(str(xmi_path)).getroot()
    except Exception as e:  # pragma: no cover
        print(f"  warning: could not parse DDI-CDI XMI {xmi_path.name}: {e}")
        return

    cdi_ns = _SCHEMA_PREFIX_EXPANSIONS["cdi:"]

    def _extract_comment_body(elem) -> Optional[str]:
        """Return the concatenated <body> text from any direct <ownedComment>
        child of `elem`, or None if there is none."""
        parts = []
        for oc in elem.findall("ownedComment"):
            for body in oc.findall("body"):
                if body.text:
                    parts.append(body.text.strip())
        return "\n\n".join(parts) if parts else None

    def _name_of(elem) -> Optional[str]:
        n = elem.find("name")
        return n.text.strip() if (n is not None and n.text) else None

    XMI_NS = "{http://www.omg.org/spec/XMI/20131001}"

    def _local(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    def _walk(elem, path: list[str]) -> None:
        # Every <packagedElement xmi:type="uml:Class"> or "uml:DataType" is a term.
        # <packagedElement xmi:type="uml:Package"> nodes contain more packagedElements
        # and need to be recursed into.
        for child in elem:
            if _local(child.tag) != "packagedElement":
                continue
            xt = child.get(f"{XMI_NS}type") or child.get("xmi:type") or ""
            name = _name_of(child)
            if xt.endswith("Package"):
                _walk(child, path + ([name] if name else []))
                continue
            if not name:
                continue
            body = _extract_comment_body(child)
            full = f"{cdi_ns}{name}"
            entry = lookup.setdefault(full, {})
            entry.setdefault("label", name)
            if body and not entry.get("definition"):
                entry["definition"] = body
            entry.setdefault("parents", [])
            entry.setdefault("ranges", [])
            entry.setdefault("domains", [])
            entry.setdefault("source", "ddi-cdi")
            # Walk owned attributes too, so `cdi:InstanceVariable-source` etc. resolve.
            for oa in child.findall("ownedAttribute"):
                aname = _name_of(oa)
                if not aname:
                    continue
                a_body = _extract_comment_body(oa)
                akey = f"{cdi_ns}{name}-{aname}"
                aentry = lookup.setdefault(akey, {})
                aentry.setdefault("label", aname)
                if a_body and not aentry.get("definition"):
                    aentry["definition"] = a_body
                aentry.setdefault("parents", [])
                aentry.setdefault("ranges", [])
                aentry.setdefault("domains", [])
                aentry.setdefault("source", "ddi-cdi")

    # xmi:XMI > uml:Model > packagedElement — need to enter Model first.
    for child in root:
        if _local(child.tag) == "Model":
            _walk(child, [])


def load_definition_lookup(
    schema_org_path: Optional[Path] = None,
    ddi_cdi_xmi: Optional[Path] = None,
    extra_rdf: Optional[list[tuple[Path, str]]] = None,
) -> dict[str, dict]:
    """Build the URI -> definition lookup dict from all cached vocabularies.

    Returns an empty dict if no source files are present (offline-safe: the
    generator will fall back to config-declared definitions)."""
    lookup: dict[str, dict] = {}
    _load_schema_org(lookup, schema_org_path or SCHEMA_ORG_FILE)
    for path, fmt in _TURTLE_SOURCES:
        _load_rdf(lookup, path, fmt)
    _load_ddi_cdi(lookup, ddi_cdi_xmi or DDI_CDI_XMI)
    for path, fmt in extra_rdf or []:
        _load_rdf(lookup, path, fmt)
    return lookup


def resolve(lookup: dict[str, dict], uri: str) -> Optional[dict]:
    """Look up a definition, expanding common prefixes first."""
    if not uri:
        return None
    full = _expand(uri)
    return lookup.get(full)


if __name__ == "__main__":  # pragma: no cover
    lut = load_definition_lookup()
    print(f"Loaded {len(lut)} terms")
    for probe in ("https://schema.org/Dataset", "http://purl.org/dc/terms/conformsTo",
                  "http://www.w3.org/ns/prov#Activity", "http://www.w3.org/ns/dcat#CatalogRecord",
                  "http://www.w3.org/2004/02/skos/core#ConceptScheme",
                  "http://ddialliance.org/Specification/DDI-CDI/1.0/RDF/InstanceVariable",
                  "http://ddialliance.org/Specification/DDI-CDI/1.0/RDF/Statistics"):
        entry = lut.get(probe)
        if entry:
            label = entry.get("label") or "(no label)"
            defn = (entry.get("definition") or "")[:80]
            print(f"  OK  {probe}  |  {label}  |  {defn}...")
        else:
            print(f"  --  {probe}  (not in lookup)")
