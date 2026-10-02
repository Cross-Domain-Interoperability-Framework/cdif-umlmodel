# cdifjsonxmi

XMI ↔ JSON Schema tools for CDIF building blocks.

- `uml_to_schema.py` is the production UML/XMI → building-block schema generator, moved here
  from `metadataBuildingBlocks/tools/` together with the modules it imports (`rst_augment.py`,
  `definition_lookup.py`, `rst_documentation.py`) and `vocabularies/`. See
  [uml_to_schema.md](uml_to_schema.md). It still reads and writes metadataBuildingBlocks
  `_sources/`, by default at `../../metadataBuildingBlocks/_sources` (override with
  `--sources-dir`). `ucmism2m/script/build-docs.ps1` and two ucmism2m audit scripts use it from here.
- `bblock_to_xmi.py` and `roundtrip.py` are an experiment: generate Canonical XMI (UML 2.5 /
  XMI 2.5.1) from building-block JSON Schemas, regenerate the schema with `uml_to_schema.py`,
  and check whether the round trip is isomorphic. The XMI follows the conventions of
  `../xmiModels/cdifmodels/cdifmodels.xmi`. With `--linked` they write a set of XMI files,
  one per building block, that reference each other like the schemas do (see
  [Linked XMI](#linked-xmi)).
- `reuse_report.py` finds reuse between building blocks, explicit (`$ref`) and implicit
  (the same type or shape copied into several blocks), and writes `reuse_report.md` (see
  [Reuse report](#reuse-report)).

```bash
pip install pyyaml
python roundtrip.py <metadataBuildingBlocks>/_sources/schemaorgProperties/person
python roundtrip.py --linked <bb dir> <bb dir> ...      # linked files, see below
python reuse_report.py
```

`roundtrip.py` runs `bblock_to_xmi.py` (schema.yaml → XMI), then `uml_to_schema.py`
(XMI → schema.yaml), and diffs the result against the original. It calls `uml_to_schema.py`
with `--prefix <class package> --strict-required --schema-only` plus the opt-in round-trip
flags `--xsd-formats --iri-reference-type IriReference --comment-directives --verbatim-docs`
(documented in [uml_to_schema.md](uml_to_schema.md)). Output goes to `roundtrip/<bblock>/`:
the XMI, `regenerated/schema.yaml`, and `diff.txt`.

`diff.txt` has two sections. *Exact* compares the schemas as JSON values, ignoring key
order. *Semantic* first rewrites both into a form with the same meaning: file `$ref`s
resolved to absolute paths, local `#/$defs/` references inlined, and `required` / choice
`anyOf`s gathered from `allOf`. The script exits 1 if any semantic difference remains.

`bblock_to_xmi.py` can also be run on its own to produce just the XMI. It fails on any
construct outside the mapping below, instead of approximating it.

## Linked XMI

`bblock_to_xmi.py --linked OUT_DIR <bb dir>...` writes one Canonical XMI file per building
block, composable the way the schemas are:

- **Layout:** `OUT_DIR/<path under _sources>/<dir>.xmi`, plus `OUT_DIR/cdifCommonTypes.xmi`.
  The shared file holds the XSD datatypes and `IriReference` once, and every block links to it.
- **Package:** each file holds one `uml:Package` for the block. Its `xmi:id` is the block's
  register identifier (`cdif.bbr.metadata.schemaorgProperties.person`, using the
  `identifier-prefix` from `bblocks-config.yaml`). Its `URI` is the register URI
  (`https://w3id.org/cdif/bbr/metadata/schemaorgProperties/person`).
- **Element ids:** `<register id>.<Element>`, and `<register id>.<Element>.<attribute>` for
  attributes, so they are unique across the whole set. The block's root element and its
  inline nested objects (e.g. person's `ContactPoint`) live in the block's file.
- **Element uuids:** `xmi:uuid` is uuid5 of `<register URI>#<id local to the block>`. It's
  stable across regeneration and the same wherever the element is referenced from.
- **References to other blocks:** `$ref: ../organization/schema.yaml` becomes
  `<type href="../organization/organization.xmi#cdif.bbr.metadata.schemaorgProperties.organization.Organization"/>`.
  No stub elements or `:buildingBlock:` directives are needed.

`uml_to_schema.py --linked --xmi <one file>` loads that file and, transitively, every file
its `href`s point to. A type defined by another block becomes a `$ref` to that block's
`schema.yaml`. An `href` to a block whose XMI file doesn't exist yet (e.g. organization's
`cdifConceptOrTermOrString`) still becomes that `$ref`, with a warning.

`roundtrip.py --linked <bb dir>...` writes the set under `roundtrip/linked/` and regenerates
each block from its own file. For identifier, organization and person the results are the
same as single-file mode (see [Results](#results)).

Not yet done: profile modules that refine another block's class (`cdifDiscovery` etc. on
`schema:Dataset`, planned as UML PackageMerge); composite profiles (`allOf` of modules,
planned as PackageImport); a block's vocabulary prefix when its root has no `@type`; and
importing a linked set into Enterprise Architect.

## Reuse report

`reuse_report.py` scans every `schema.yaml` under `_sources` (archive excluded) and writes
`reuse_report.md`:

- **Explicit reuse:** `$ref`s between block files, and the most-referenced blocks.
- **Typed definitions:** every object schema with an `@type` const defines that RDF type.
  Types defined in more than one block are grouped by *shape*: the schema without
  `$schema`, `description`, `title`, `$comment`, `examples` and `default`. Each type gets one
  of these recommendations:
  - `extract`: one shape and no owning block. Make one shared element.
  - `link`: some block has the type as its root. Copies should link to it.
  - `review`: several shapes, no owner. A person decides base element and restrictions.

  Each shape that differs is listed with its differences from the most common shape.
- **Untyped shapes repeated in 3 or more blocks:** for example the `{"@id"}` node reference
  and the IRI-reference union. It notes when a block already has that exact shape as its root.

Current findings (93 blocks):

- **Explicit `$ref`s:** 1,387, 965 of them into `ddicdiDataTypes`.
- **RDF types defined in more than one block:** 76 of 191.
  - 20 to extract, e.g. `cdi:TypedString`, identical in 10 blocks.
  - 11 to link.
  - 45 to review. For example, five copies of `cdi:ConceptSystem` require `minItems` on
    `has` / `isDefinedBy` / `name` and two don't, and `cdifRepresentedVariable`'s copy uses
    `cdif:` property names.
- **The `{"@id": string}` node reference:** copied inline 117 times in 40 blocks. It has
  exactly the shape of `cdifDataType/objectReference`, which only 29 `$ref`s use.

## Forward mapping (bblock_to_xmi.py)

The XMI has one element per object schema: the building block itself, every inline
nested object, and a stub for every building block it `$ref`s. Facts UML has no slot
for are written as **comment directives**, lines after the definition text, which
`uml_to_schema.py --comment-directives` reads back.

| JSON Schema | XMI |
|---|---|
| object schema that declares `@id` | `uml:Class` |
| object schema without `@id` | `uml:DataType` (a value type, which `uml_to_schema.py` gives no `@id`) |
| `@type` = array `contains: {const: p:T}`, `minItems: 1` | class directive `:rdfType: ``p:T```; the element goes in package `p` |
| `@type` items restricted to `enum: [p:T, p:U, ...]` (bare or in a one-branch `anyOf`) | class directive `:allowedTypes: ``p:T | p:U | ...``` |
| `anyOf: [{required: [a]}, {required: [b, c]}]` | class directive `:choiceConstraints:` then `- ``a | b & c``` |
| property `p:name` (same prefix as the class) | attribute `name` |
| property `q:name` (another prefix) | attribute `q_name`, as in `cdifmodels.xmi` (e.g. `cdi_identifier`) |
| `string` / `integer` / `boolean` / `number` | UML primitive `String` / `Integer` / `Boolean` / `Real` |
| `string` + `format: uri` / `date` / `date-time` | `XMLSchemaDataTypes.XsdAnyUri` / `XsdDate` / `XsdDateTime` |
| `anyOf: [string, {"@id": string}]` (the JSON-LD IRI-reference pattern) | `common.IriReference` (**new DataType**, not in `cdifmodels.xmi`) |
| `$ref: ../X/schema.yaml` (another building block) | attribute typed by a stub element with class directive `:buildingBlock: ``<path under _sources>```. The stub is named from the directory (`cdifConceptOrTermOrString` → `ConceptOrTermOrString`). Its package comes from X's `@type` or prefixed properties, else from the directory prefix (`cdif…` → `cdif`, `ddicdi…` → `cdi`). |
| `$ref: '#/$defs/X'` | followed to the definition, which is then mapped like any other property schema |
| `$ref` to a building block that is a Class | attribute directive `:inlineOrByReference: ``inline```, because the schema embeds the node and doesn't accept `{"@id"}` (tag name from OGC UML-to-JSON rules) |
| `anyOf: [{$ref: ...}, {type: string}]` | attribute directive `:alsoAcceptsString:` |
| inline `type: object` with an `@type` const | nested element named from the const (e.g. `ContactPoint`) |
| `type: array`, `items: X` | type of X, upper bound `*` |
| in `required` | lower bound `1` (else `0`) |
| `description` | `ownedComment` body after the `**CDIF** / Definition` header, kept verbatim |
| `default` on `@type` (a string or a list of strings) | class directive `:typeDefault: ``p:T``` (`|`-separated for several). `uml_to_schema.py` always writes it back as an array, `default: [p:T]`, because `@type` is an array. A bare-string default in the source therefore comes back wrapped. |

The building block's own element is named from its directory the same way as stubs.

## Results

All three building blocks validate the same way against the original and regenerated
schemas. The remaining differences are annotations.

| | identifier | person | organization |
|---|---|---|---|
| exercises | scalars, URI, IRI reference, choice constraint | plus `$ref` to a DataType BB (or string), embed-only `$ref` to a Class BB, inline nested object, array of IRI references | plus `@type` restricted to a list of subtypes, local `$defs` aliasing a union-shaped BB in another folder |
| semantic differences | `@id` added; `title` added | `@id` description added; `title` added; `contactPoint`'s `@type` default `"schema:ContactPoint"` becomes `["schema:ContactPoint"]` | `@id` description added; `title` added; `@type` default `"schema:Organization"` becomes `["schema:Organization"]` |
| examples + edge cases validating the same | 2 + 4 | 2 + 11 | 2 + 9 |

- `uml_to_schema.py` adds `@id` (with a description) to every class node, and to a root
  emitted from a DataType, such as Identifier. Neither original sets
  `additionalProperties: false`, so this changes no validation result.
- Person's edge cases cover: name only; identifier as a string or as a PropertyValue;
  neither name nor identifier; affiliation as `{"@id"}` only (rejected by both) or embedded;
  contactPoint without email or without `@type`; `sameAs` with `{"@id"}` items or not an
  array; wrong `@type`. Organization's cover: an allowed and a non-listed subtype in
  `@type`; a subtype without `schema:Organization`; `additionalType` as strings, as a
  DefinedTerm, not an array, or with a number; identifier only; neither name nor
  identifier. `$ref`s were resolved from disk, including the nested ones.

Without the opt-in flags, the identifier round trip had 14 differences and rejected both
of its own examples: XSD types and `IriReference` became JSON-LD node objects, `@type`
became `schema:Identifier`, the value-or-url constraint was dropped, and descriptions
were reflowed.

The flags don't change `uml_to_schema.py`'s default behaviour. Its default outputs were
byte-identical before and after each change, checked on five schema runs over the EA
DDI-CDI XMI and `cdifmodels.xmi` (single-class, multi-class with `--emit-uml`,
`--strict-required --inline-datatypes`) and one `--config` run emitting canonical and EA XMI.

## Not handled yet by bblock_to_xmi.py

A local `$defs` entry that defines an object inline (rather than pointing at another
building block) is mapped as an inline nested object, which is untested. Not handled:
`oneOf`/`anyOf` unions other than the patterns above,
single-value-or-array unions, `enum`, `const` values, `minItems`/`maxItems` other than on
`@type`, and `pattern`. Each building block becomes its own XMI. Referenced blocks appear
only as stubs, so there's no merged model of several blocks yet.

The generated XMI hasn't been imported into Enterprise Architect.
