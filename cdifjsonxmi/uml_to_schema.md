# uml_to_schema.py

Moved here from `metadataBuildingBlocks/tools/` (its history is in that repo, up to commit
e77d36377). It still reads and writes metadataBuildingBlocks building blocks. The examples
below run from the metadataBuildingBlocks root, so `--out-dir` points into its `_sources/`.

**Building-block sources.** Sibling-BB class lookup, the shared-datatypes fallback and
`:buildingBlock:` references read `metadataBuildingBlocks/_sources`. The default assumes that
repo is a sibling of cdif-umlmodel. Pass `--sources-dir PATH` otherwise.

**Companion modules.** With `--config`, the generator rewrites definitions into RST
documentation through `rst_augment.py`, which uses `definition_lookup.py`,
`rst_documentation.py` and the cached vocabularies in `vocabularies/`. All of them sit in this
folder. If `rst_augment` can't be imported, the step is skipped silently.

**Other users.** `ucmism2m/script/build-docs.ps1` runs this script, and
`ucmism2m/script/audit_schema_vs_uml.py` and `_audit_release_schemas.py` import it.

Generates a CDIF building-block `schema.yaml` (and, optionally, the surrounding `bblock.json` / `context.jsonld` / `rules.shacl` / `examples.yaml` skeletons) from a DDI-CDI / UCMIS class model. Used to bootstrap and refresh the `_sources/ddiProperties/ddicdi*` BBs.

**XMI format auto-detection.** `parse_xmi()` peeks at the XMI root and dispatches:
- **canonical XMI 2.5.1** (OMG namespaces, `uml:Model`, `packagedElement` / `ownedAttribute` / navigable-end association ends) → `_parse_canonical_xmi()`;
- **Enterprise Architect native XMI 1.1** (`xmi.version="1.1"`, `xmlns:UML="omg.org/UML1.3"`, `UML:Class` distinguished by `ea_stype` tagged value, top-level `UML:Generalization` / `UML:Association` with `UML:AssociationEnd` children) → `parse_ea_xmi()`.

Both parsers emit the same internal `Model` / `UmlClass` / `Property` structures, so everything downstream (def generation, inline-or-ref, multiplicity, generalization walk) is format-agnostic.

**Usage:**
```bash
# Single-class BB
python ../cdif-umlmodel/cdifjsonxmi/uml_to_schema.py \
  --xmi C:/path/to/ddi-cdi_ea15.2026.March.xml \
  --class EnumerationDomain \
  --bb-name ddicdiEnumerationDomain \
  --out-dir _sources/ddiProperties/

# Multi-class BB (root anyOf over multiple concrete classes)
python ../cdif-umlmodel/cdifjsonxmi/uml_to_schema.py \
  --xmi C:/path/to/ddi-cdi_ea15.2026.March.xml \
  --class DataStructure,DimensionalDataStructure,LongDataStructure,WideDataStructure \
  --bb-name ddicdiDataStructure \
  --out-dir _sources/ddiProperties/

# Just the schema.yaml, skip bblock.json/context.jsonld/rules.shacl/examples.yaml stubs
python ../cdif-umlmodel/cdifjsonxmi/uml_to_schema.py ... --schema-only
```

**Encoded conventions:**
- Walks UML generalization (subclass shadows parent on name collision); collects own + inherited attributes.
- Multiplicity: `0..1` / `1..1` → single value; `*` upper → array-only with `minItems` if `lower>=1`.
- `uml:DataType` targets → `$ref` to `../ddicdiDataTypes/schema.yaml#/$defs/<Name>` if the name is in that BB's `$defs`, else inlined locally.
- `uml:Class` targets → inline-or-ref by default (`anyOf [class def, id-reference]`); class def comes from a sibling BB whose root is that class, else inlined locally. `--reference X,Y` forces id-ref-only; `--inline X,Y` forces inline-only.
- `uml:Enumeration` → `enum` literal list.
- Multi-class BB root: `anyOf` over local `$defs/<Class>` entries; each class gets its own Node `$def`.
- Role-name recovery for unnamed canonical-XMI association ends from the `<Source>_<role>_<Target>` association id pattern.
- Duplicate role-name properties (UCMIS overload, e.g. `CodeList.has → Code` AND `CodeList.has → CodePosition`) are merged via flat `anyOf` of distinct targets plus a single `id-reference` fallback.
- Sibling-BB lookup recognizes three root shapes: single-class `@type.contains.const`; multi-class `@type.anyOf` of `contains.const` branches; multi-root `anyOf` of `$ref` to local `$defs`. Also derives a class name from the BB directory name (`ddicdi<ClassName>`) so abstract parents like `ValueDomain` whose subclasses share a BB resolve to that BB.

**Opt-in JSON-LD conventions** (schema emit only, all off by default, so existing outputs are unchanged). These were added for the JSON Schema → XMI → JSON Schema round-trip experiment in `cdif-umlmodel/cdifjsonxmi/`:
- `--xsd-formats`: `XsdAnyUri` / `XsdDate` / `XsdDateTime` / `XsdLanguage`-typed attributes become `{type: string, format: uri|date|date-time}` (no format for `XsdLanguage`). Without the flag they become JSON-LD node `$defs`.
- `--iri-reference-type NAME`: attributes typed by DataType `NAME` (e.g. `IriReference`) become `anyOf [string, {"@id": string}]`.
- `--comment-directives`: reads directive lines at the end of comments and removes them from descriptions (parser: `split_comment_directives`).
  - On a class or datatype:
    - `:rdfType: ``p:T``` sets the `@type` const (default `prefix:ClassName`). On a datatype it also makes `@type` required.
    - `:allowedTypes: ``p:T | p:U``` restricts the `@type` items to `enum: [p:T, p:U]`.
    - `:choiceConstraints:` followed by `- ``a | b & c``` lines adds `allOf: [{anyOf: [{required: [a]}, {required: [b, c]}]}]`.
    - `:buildingBlock: ``schemaorgProperties/identifier``` means the type is defined by that BB (path under `_sources/`). References to it become a `$ref` relative to the output BB dir, taking precedence over sibling-BB discovery and local inlining.
  - On an attribute:
    - `:inlineOrByReference: ``inline```: a class-typed value is the embedded node only.
    - `:inlineOrByReference: ``byReference```: an id-reference only.
    - Absent: the default `anyOf [node, id-reference]`.
    - `:alsoAcceptsString:` wraps the value as `anyOf [<type>, {type: string}]`.
- `--verbatim-docs`: keep the Definition text exactly as written. Without the flag, `clean_definition` collapses whitespace.

**Source XMI:** DDI-CDI XMI exports live outside this repo at the user's working location. Two are in use:
- `C:/Users/smrTu/OneDrive/Documents/GithubC/CDIF/cdif-umlmodel/ddi-cdi_ea15.2026.March.xml` — Enterprise Architect native XMI 1.1 export of the 2026-03 DDI-CDI model (current source of truth).
- `C:/Users/smrTu/OneDrive/Documents/GithubC/CDIF/to-canonical-xmi/ddi-cdi_canonical-unique-names.xmi` — older canonical XMI 2.5.1 export.

Pull a fresh copy when the model updates; `uml_to_schema.py` auto-detects which format it is.

**Requirements:** Python 3.10+ with `pyyaml`.

