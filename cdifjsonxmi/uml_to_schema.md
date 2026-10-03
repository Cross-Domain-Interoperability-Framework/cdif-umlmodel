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
- `--id-reference-type NAME`: attributes typed by DataType `NAME` (e.g. `IdReference`) become the node reference `{"@id": string}` with no other keys.
- `--comment-directives`: reads directive lines at the end of comments and removes them from descriptions. The full list, with what each becomes, is the comment above `_DIRECTIVE_NAMES` in `uml_to_schema.py`; `README.md` (Forward mapping) shows which JSON Schema construct produces each. In short:
  - **Class / datatype:** `:rdfType:` (one, `|` any of, `&` all of), `:allowedTypes:`, `:typeDefault:`, `:typeDescription:`, `:typeOptional:`, `:typeSchema:`, `:idDescription:`, `:noId:`, `:title:`, `:prefix:`, `:choiceConstraints:`, `:constraint:`, `:contextSchema:`, `:buildingBlock:`, `:union:` (the datatype is an `anyOf` / `oneOf` over its attributes), `:exportedDef:` (written as a `$defs` entry of that name), `:rootAlias:` / `:rootDescription:` (the block root is a `$ref` to this element's `$defs` entry), `:extends:` (the element is an `allOf` of the general block's `$ref` and its own properties; `:untypedRoot:` drops the root's `type: object`), `:closed:` (`additionalProperties: false`), `:hasId:` / `:idRequired:` (`@id` on a datatype / required), and `:objectUnion:` (a union that also says `type: object`).
  - **Attribute:** `:inlineOrByReference:`, `:alsoAcceptsString:`, `:jsonName:`, `:default:`, `:itemsDefault:`, `:minItems:`, `:descriptionOnItems:`, `:itemsDescription:`, `:keywords:`, `:arrayKeywords:`, `:idRefDescription:`, `:schemaRef:` (written as that `$ref`), `:valueDescription:`, `:alternativeOverrides:` (per-use annotations on a union's alternatives), `:valueSchema:` (the value schema verbatim), `:orReference:` (the value, or a plain string and/or an `{"@id"}` reference, in the listed order), `:referenceFirst:` (an IRI reference with `{"@id"}` first). `$ref`s written `bb:<path under sources>` in `:constraint:` and `:valueSchema:` are made relative to the output building block.
  - With the flag, `@type` and `@id` are written only as the model records them (never invented), an empty object stays `{"type": "object"}`, the title comes from `:title:` unless `--title` is given, and a class defined in the XMI is never swapped for a sibling building block of the same name.
- `--verbatim-docs`: keep the Definition text exactly as written. Without the flag, `clean_definition` collapses whitespace.
- `--linked`: `--xmi` is one file of a linked set written by `bblock_to_xmi.py --linked` (see `README.md`, Linked XMI).
  - The loader (`load_linked_xmi`) follows `<type href="other.xmi#id">` into the other files, transitively, and merges them into one model. It also follows generalization `href`s, so attributes inherited from a shared base in `cdifSharedTypes.xmi` are merged back.
  - A type whose id belongs to another building block (`cdif.bbr.metadata.<path>.<Name>`) becomes a `$ref` to `<sources-dir>/<path>/schema.yaml`, relative to the output BB.
  - An `href` to a missing file leaves a placeholder datatype, so it still becomes that `$ref`, with a warning.
  - Without `--linked`, an `href` to anything but a UML primitive still falls back to `type: string`.

**Source XMI:** DDI-CDI XMI exports live outside this repo at the user's working location. Two are in use:
- `C:/Users/smrTu/OneDrive/Documents/GithubC/CDIF/cdif-umlmodel/ddi-cdi_ea15.2026.March.xml` — Enterprise Architect native XMI 1.1 export of the 2026-03 DDI-CDI model (current source of truth).
- `C:/Users/smrTu/OneDrive/Documents/GithubC/CDIF/to-canonical-xmi/ddi-cdi_canonical-unique-names.xmi` — older canonical XMI 2.5.1 export.

Pull a fresh copy when the model updates; `uml_to_schema.py` auto-detects which format it is.

**Requirements:** Python 3.10+ with `pyyaml`.

