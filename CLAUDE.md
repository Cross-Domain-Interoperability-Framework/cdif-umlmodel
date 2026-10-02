# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo holds the UML models that define CDIF (Cross-Domain Interoperability Framework). The syntactic profiles (JSON Schema, SHACL) are generated from these models. Most of the repo is model artifacts, not code:

- `EAmodelFiles/`: Enterprise Architect projects (`.qea`, `.eapx`). These are binary, so edit them in EA, not here.
- `xmiModels/`: Canonical XMI exports (UML 2.5 / XMI 2.5.1). Each sub-model has `model/` (XMI, including `*-eclipse.xmi` variants that have unique association names for Eclipse tooling) and, where present, `diagram/` (SVG/PDF).
  - `cdifmodels/cdifmodels.xmi` is generated. **Do not hand-edit it.** Its README says changes go into the generation process, not into EA.
  - `ddi-cdi_ea15.2026.March.xml` is the DDI-CDI source model. `ddi_xmi_consistency_audit.txt` lists the attributes and association roles that each CDIF `ddicdi*` class is missing compared with that source.
- `tools/`: Python validation tooling for CDIF JSON-LD instance documents.
- `docs/`: PDFs for reference.
- The HTML model documentation is published from the `gh-pages` branch, not from `main`.

The root README mentions `generatedJSON/` and `generatedSHACL/` folders and generator tools. None of these exist in the repo. The generated schemas and shapes that are actually present are the ones in `tools/`.

## tools/ is a mirror. Don't edit the mirrored files

`.github/workflows/sync-tools-from-validation.yml` runs daily, and also on demand through workflow_dispatch. It copies files from the public `Cross-Domain-Interoperability-Framework/validation` repo into `tools/`. That repo is the source of truth, and the list of mirrored files is `tools/sync_mirror_tools.sh` in the validation repo. Fix scripts, schemas, SHACL shapes and the frame upstream, then re-sync. Edits made here get overwritten.

Only `tools/examples/` and `tools/readme.md` belong to this repo. The sync never touches them.

## Running validation (from `tools/`)

```bash
pip install pyld jsonschema pyshacl rdflib requests

# 1. Frame (graph -> tree) + JSON Schema. Always pass --schema; three schemas sit side by side
python FrameAndValidate.py examples/cdifComplete-example.json -v --schema CDIFCompleteSchema.json
python FrameAndValidate.py examples/cdifComplete-example.json -o framed.json   # frame only, to inspect

# 2. SHACL on the RDF graph (no framing)
python ShaclJSONLDContext.py examples/cdifComplete-example.json ShaclValidation/CDIF-Complete-Shapes.ttl

# 3. Resolve the record's dcterms:conformsTo profile URIs -> schema + SHACL for each
python ConformanceValidate.py examples/cdifComplete-example.json --source local   # offline, via conformance-schema-map.json
python ConformanceValidate.py examples/cdifComplete-example.json --source w3id    # authoritative, needs network
```

Things that aren't obvious:
- JSON Schema validates trees, and JSON-LD is a graph. That's why `CDIF-frame-2026.jsonld` reshapes the document into a tree rooted at `schema:Dataset` before schema validation. SHACL skips this step.
- In SHACL output, only `sh:Violation` means the document fails to conform. A `Conforms: False` result that contains only `sh:Info` or `sh:Warning` is advisory. For example, `prov-ocean-temp-example.json` reports one `sh:Info`.
- Workflow 3 needs a `schema:subjectOf` catalog record (`dcat:CatalogRecord`) that has `dcterms:conformsTo` URIs. `conformance-schema-map.json` is deliberately partial: some profiles map only to SHACL. So `--source local` and `--source w3id` can give different results.
- Both examples should validate with no `sh:Violation` in all three workflows. Use this as a regression check. There is no other test suite.
