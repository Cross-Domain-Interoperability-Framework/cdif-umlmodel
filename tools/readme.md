# CDIF validation tools

Code and supporting documents for validating CDIF JSON-LD instance documents.
The validation scripts, JSON-LD framing document, framed-tree JSON Schemas, and
SHACL shape sets in this directory are **mirrored from the CDIF
[`validation`](https://github.com/Cross-Domain-Interoperability-Framework/validation)
repository**, which remains the source of truth. (Tools for *generating* JSON
Schema and SHACL rules from the UML model live alongside these and are described
in the repository root `README`.)

There are three ways to validate an instance document, described below. All three
consume the same CDIF JSON-LD instance file.

## Install

```bash
pip install pyld jsonschema pyshacl rdflib requests
```

| Package | Used by |
|---------|---------|
| `pyld` | framing (workflows 1, 3) |
| `jsonschema` | JSON Schema validation (workflows 1, 3) |
| `pyshacl`, `rdflib` | SHACL validation (workflows 2, 3) |
| `requests` | fetching schemas/shapes from w3id (workflow 3, `--source w3id`) |

## Why framing is needed

JSON-LD is a **graph** format; JSON Schema validates **trees**. The
`CDIF-frame-2026.jsonld` frame reshapes a CDIF graph into the nested tree rooted
at `schema:Dataset` that the framed-tree schemas expect. `FrameAndValidate.py`
and `ConformanceValidate.py` both frame the document before running JSON Schema
validation. SHACL (workflow 2) operates on the RDF graph directly and does **not**
require framing.

- `CDIF-frame-2026.jsonld` — the JSON-LD frame (graph → tree).
- `CDIF-context-2026.jsonld` — the authoring context, for writing CDIF documents
  with unprefixed terms (not required to run validation).

## Example instances

Two ready-to-validate CDIF instance documents live in `examples/`; the commands
below use them so they run as-is:

| File | Profiles declared | Notes |
|------|-------------------|-------|
| `examples/prov-ocean-temp-example.json` | core, discovery | Discovery-level record with inline provenance content. |
| `examples/cdifComplete-example.json` | core, discovery, data description, data structure, provenance, manifest | Full "complete" record exercising every profile. |

Both validate with **no `sh:Violation`** across all three workflows.
(`prov-ocean-temp-example.json` does surface one `sh:Info` recommendation under
workflow 2 — advisory only; see the severity note there.)

---

## Workflow 1 — validate against a JSON Schema

`FrameAndValidate.py` frames the document, then validates the framed tree against
a JSON Schema you name with `--schema`.

```bash
python FrameAndValidate.py examples/cdifComplete-example.json -v \
    --schema CDIFCompleteSchema.json \
    --frame CDIF-frame-2026.jsonld
```

- `--frame` is **optional** here — the single `*-frame.jsonld` beside the script
  (`CDIF-frame-2026.jsonld`) is auto-detected. Pass it explicitly if you keep more
  than one frame in the directory.
- `--schema` should be given explicitly, because three schemas ship side by side
  (see below) and the script only auto-detects a schema when exactly one is present.
- `-v` / `--validate` runs validation; omit it (with `-o`) to just frame and save.
- `-o framed.json` writes the framed tree, useful for debugging.

Pick the schema for the profile you want to check:

| Schema | Profiles covered |
|--------|------------------|
| `CDIFDiscoverySchema.json` | discovery |
| `CDIFDataDescriptionSchema.json` | discovery + data description |
| `CDIFCompleteSchema.json` | discovery + data description + archive + provenance |

```bash
# frame only, no validation (inspect the tree the schema will see)
python FrameAndValidate.py examples/cdifComplete-example.json -o framed.json
```

---

## Workflow 2 — validate against SHACL rules

`ShaclJSONLDContext.py` validates the instance against a SHACL shapes file you
provide. It reads the JSON-LD straight into an RDF graph (no framing).

```bash
python ShaclJSONLDContext.py examples/cdifComplete-example.json ShaclValidation/CDIF-Complete-Shapes.ttl
```

Positional or named arguments both work; add `-v` for SPARQL-target diagnostics:

```bash
python ShaclJSONLDContext.py -d examples/prov-ocean-temp-example.json -s ShaclValidation/CDIF-Discovery-Shapes.ttl -v
```

Available shape sets in `ShaclValidation/`:

| Shapes file | Scope |
|-------------|-------|
| `CDIF-Discovery-Shapes.ttl` | discovery profile |
| `CDIF-DataDescription-Shapes.ttl` | data description |
| `CDIF-DataStructure-Shapes.ttl` | data structure |
| `CDIF-Provenance-Shapes.ttl` | provenance |
| `CDIF-Manifest-Shapes.ttl` | manifest |
| `CDIF-Complete-Shapes.ttl` | discovery + data description + provenance (composite) |

Exit code is `0` when the graph conforms, `1` otherwise. Per CDIF policy the CLI
reports all severities; only `sh:Violation` results indicate non-conformance.
(For example, `prov-ocean-temp-example.json` reports `Conforms: False` but the
sole result is an `sh:Info` "Recommended: include dcterms:conformsTo…" — advisory,
not a violation.)

---

## Workflow 3 — validate by resolving the `conformsTo` URIs

`ConformanceValidate.py` reads the `schema:subjectOf` catalog record, extracts
each `dcterms:conformsTo` profile URI, resolves that profile's JSON Schema **and**
SHACL rules, and validates the document against every profile it claims — one
report section per profile.

```bash
# authoritative: resolve each profile from the w3id.org/cdif redirector (needs network)
python ConformanceValidate.py examples/cdifComplete-example.json --source w3id

# offline: resolve from local files via conformance-schema-map.json
python ConformanceValidate.py examples/cdifComplete-example.json --source local
```

Two resolution sources:

- **`--source w3id`** (default) — fetches `<profile-URI>/schema` and
  `<profile-URI>/shacl` from `w3id.org/cdif`. Authoritative (validates against the
  currently deployed artifacts) but requires network access. `--cache-dir DIR`
  caches fetched artifacts between runs.
- **`--source local`** — looks each profile URI up in `conformance-schema-map.json`
  (beside the script) and validates against the local schemas / SHACL shapes it
  points at. Works offline. Note the local map is intentionally partial (some
  profiles carry SHACL only, no framed-tree schema), so its results can differ from
  `--source w3id`, which serves whatever is currently deployed.

Useful flags:

```bash
python ConformanceValidate.py instance.jsonld --source local --no-shacl   # JSON Schema only
python ConformanceValidate.py instance.jsonld --source local --no-schema  # SHACL only
python ConformanceValidate.py ./a-directory --source local --summary      # batch a directory
python ConformanceValidate.py instance.jsonld --schema-map my-map.json     # custom local map
```

The instance **must** carry a `schema:subjectOf` catalog record
(`schema:additionalType: dcat:CatalogRecord`) with at least one
`dcterms:conformsTo` URI, or there is nothing to resolve. Domain-specific
`conformsTo` claims outside the `w3id.org/cdif/` namespace are ignored for
profile resolution.

---

## File inventory

```
tools/
├── FrameAndValidate.py            workflow 1 (frame + JSON Schema)
├── ShaclJSONLDContext.py          workflow 2 (SHACL)
├── ConformanceValidate.py         workflow 3 (resolve conformsTo → schema + SHACL)
├── CDIF-frame-2026.jsonld         JSON-LD frame (graph → tree)
├── CDIF-context-2026.jsonld       authoring context (prefix-free authoring)
├── CDIFDiscoverySchema.json       framed-tree schema: discovery
├── CDIFDataDescriptionSchema.json framed-tree schema: discovery + data description
├── CDIFCompleteSchema.json        framed-tree schema: complete
├── conformance-schema-map.json    local URI → schema/SHACL map (workflow 3 --source local)
├── ShaclValidation/
│   ├── CDIF-Discovery-Shapes.ttl
│   ├── CDIF-DataDescription-Shapes.ttl
│   ├── CDIF-DataStructure-Shapes.ttl
│   ├── CDIF-Provenance-Shapes.ttl
│   ├── CDIF-Manifest-Shapes.ttl
│   └── CDIF-Complete-Shapes.ttl
└── examples/
    ├── prov-ocean-temp-example.json   discovery-level sample
    └── cdifComplete-example.json      complete-profile sample
```

## Notes

- `FrameAndValidate.py` is the normative, sync-managed script from the `validation`
  repository (`validation/tools/FrameAndValidate.py`); edit it there and re-mirror
  rather than editing the copy here.
- `FrameAndValidate.py --conformance` (content-derived conformance detection) is a
  no-op in this mirror: it depends on `detect_conformance.py` and the building-block
  `_sources` tree, which are not copied here. Use the `validation` repository for
  that workflow.
- The schemas, shapes, frame, and local map are point-in-time copies of the
  `validation` repo artifacts. Re-copy them when the upstream profiles change.
