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

- **Layout:** `OUT_DIR/<path under _sources>/<dir>.xmi`, plus `OUT_DIR/cdifCommonTypes.xmi`
  (the XSD datatypes, `IriReference` and `IdReference`, once), `OUT_DIR/cdifSharedTypes.xmi`
  (abstract bases, below) and `OUT_DIR/cdifSharedUnions.xmi` (shared unions, below). Blocks
  link to all three.
- **Shared bases:** an RDF type defined by two or more elements of the same kind across the
  converted set (e.g. two different `schema:EntryPoint` objects in action and linkRole) gets
  one abstract base in `cdifSharedTypes.xmi`, and each of those elements specializes it by a
  cross-file generalization. The base holds the attributes identical in all of them, removed
  from the specializations; `uml_to_schema.py` merges them back by walking the generalization,
  so the round trip is unchanged. Inline specializations are named by their block
  (`ActionEntryPoint`, `LinkRoleEntryPoint`); block roots keep their names. The converter runs
  two passes over the set to find these.
- **Shared unions:** unions are named by content, not by use (`StringOrLabeledLink`). A union
  with the same alternatives in two or more blocks, all of them references, scalars or `@id` /
  IRI references, is defined once in `cdifSharedUnions.xmi` (ids `cdif.union.<Name>`), e.g.
  `IdReferenceOrPersonOrOrganization` and `StringOrIdReferenceOrDefinedTerm`. Within a block,
  uses of the same union share one element. Annotations that differ between uses (a
  description on one alternative) are kept on the attribute as `:alternativeOverrides:`.
  The shared file is imported after the blocks its unions reference and before the blocks
  using them, so a union referencing a block that itself uses a shared union is not shared
  (it would make a cycle EA can't import: attribute types to an element imported later stay
  unlinked).
- **`$defs` used across blocks:** a definition another block `$ref`s
  (`../../bioschemasProperties/cdifBioschemasProperties/schema.yaml#/$defs/ComputationalTool`)
  is an element of its block named by its key (`:exportedDef:`); the referencing attribute
  links to it and records the reference as `:schemaRef:`.
- **A block that `allOf`s other blocks' roots and adds properties** (provActivity) becomes a
  specialization of those roots, with `:extends:`: one generalization per `$ref`. The same
  holds for a nested value (cdifDataDescription's variable is both a VariableMeasured and an
  InstanceVariable) and a composite of `$ref`s only (`:noOwnSchema:`).
- **Profile modules** (`profiles/cdifProfile`: cdifDiscovery, cdifDataDescription,
  cdifManifest, cdifProvenance, cdifDataStructure) add properties to the dataset node and
  have no `@type` of their own. Each is an *aspect* class named after the module
  (`Discovery`, …; `DataStructureProfile`, since cdifDataStructure has a `DataStructure`
  definition), a Class by [`classification.yaml`](classification.yaml) so that composite
  profiles can specialize it. Their refinements of properties cdifCore defines (the
  `schema:subjectOf.dcterms:conformsTo` pin to the module's profile URI) are kept verbatim as
  constraints.
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

**Enterprise Architect.** EA's import of Canonical XMI keeps everything within a file, but
it leaves every type that `href`s another file blank, with no warning. It also replaces our
`xmi:uuid`s with GUIDs of its own, so re-importing a file duplicates its package. Its
Direct Merge only accepts EA-native XMI 1.1. `linked_to_ea.py <linked root>` therefore
converts a linked set to EA XMI 1.1, one file per block (`<linked root>-ea/…/<dir>.xml`):

- **GUIDs:** every element, attribute and package has its Canonical `xmi:uuid` as its EA GUID.
- **References to other blocks:** by that element's GUID, plus a `type` tagged value with
  its name.
- **Associations:** an association end becomes an EA connector instead of an attribute. Its
  source end is at the owner, non-navigable, `0..*`. Its target end is named after the role
  and carries the attribute's multiplicity, comment and GUID. The connector itself is
  unnamed, so diagrams show only the role name; the Canonical XMI keeps the association name.
- **Comments:** kept verbatim.

The files name "Enterprise Architect" 2.5 as exporter and declare each element referenced
from another file as an `EAStub` in `XMI.extensions`, as EA's own exports do. With a
different exporter name, EA treated them as foreign XMI 1.1: new GUIDs, blank cross-file
types, and Direct Merge refused.

Tested in EA with common types, identifier, organization and person, imported into one
package in that order:

- Our GUIDs are kept.
- Every cross-file type links to the element imported from the other file.
- Direct Merge of `person.xml` updates the package in place, with no duplicate. EA's
  baseline comparison after merging the unchanged file reported no differences.
- A reference to a block with no file (organization's `ConceptOrTermOrString`) keeps the
  type name, unlinked.

Import in dependency order: `cdifCommonTypes.xml` first, then each block before the
blocks that reference it. `linked_to_ea.py` writes that order to `import_order.txt`. Blocks
that reference each other (cdifInstanceVariable and cdifStatistics) can't both come first,
and EA drops a connector whose other end isn't imported yet. So an association whose target is in a file imported later is written in that
file's package instead, its owner declared there as an `EAStub`.

[`import_to_ea.ps1`](import_to_ea.ps1) runs the imports in the open EA project through EA's
automation interface: `-Dir <linked root>-ea -Package <name>` imports every file in
`import_order.txt` into a root package of that name, each block in a package named after its
`_sources` subdirectory (`schemaorgProperties`, …) and the shared files at the top; `-Replace` deletes an existing package
of that name first. EA must be running with the project open. The script relaunches itself
in Windows PowerShell 5.1, since EA's COM objects aren't reachable from PowerShell 7.

Not yet done: the composite profiles (`profiles/cdifCompositeProfile`), which become
classes specializing cdifCore and the modules they combine.

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
nested object and union, and (single-file mode) a stub for every building block it
`$ref`s. Facts UML has no slot for are written as **comment directives**, lines after the
definition text, which `uml_to_schema.py --comment-directives` reads back. Names and codes
go in ` ``…`` `; free text, defaults and verbatim schema fragments are JSON.

**Elements**

| JSON Schema | XMI |
|---|---|
| object schema | `uml:Class` or `uml:DataType`, decided in this order: [`classification.yaml`](classification.yaml) overrides; Achim Wackerow's kind for that name in `cdifmodels.xmi`; else Class if the schema declares `@id` or `@type`, otherwise DataType. A Class whose schema has no `@id` gets `:noId:`. |
| `anyOf` / `oneOf` of alternatives, as a property's value or as a whole block (temporalExtent) | **union DataType** (ISO 19103 «Union» style): one optional attribute per alternative, named after it (`person`, `organization`, `idReference`, `string`, …), each mapped like a property; directive `:union: ``anyOf``` or ``oneOf``. A property's union is named by its alternatives, joined with `Or` (anyOf) or `Xor` (oneOf), e.g. `PersonOrOrganization`; one of more than three alternatives (not shared) by its first use, `<Owner><Role>Choice` (`GeneratedByUsedChoice`); a whole-block union is `<Block>Choice`. An object alternative with no RDF type to name it is named after the property using the union: alternative `reagent`, element `LabProtocolReagent` (another vocabulary's prefix dropped: `dcterms_creator` -> `creator`); an empty `{type: object}` is `object`. |
| a union alternative that only requires a key it doesn't declare (generatedBy's role-keyed wrappers, `{required: [schema:instrument]}`) | DataType `<Key>Role` (`InstrumentRole`), alternative `<key>Role`, the key in `:constraint: {"required": [...]}` |
| inline `{type: string, enum: [...]}` | `uml:Enumeration` named after the property, with its literals |
| inline `type: object` | nested element named from its `@type` const (e.g. `ContactPoint`), else `<Owner><Role>`; a numeric suffix where names collide |
| inline `allOf: [{$ref: other block}, …, {type: object, properties…}]` (cdifDataCube, cdifDataDescription's variables) | nested element specializing those blocks' roots, `:extends:`, of their kind; the object member with the most properties gives the attributes, other members are `:constraint:`s |
| `allOf: [{$ref: X}, {required: […]}]` (X and further constraints) | type of X plus `:valueAllOf: [...]` |
| a value of conditions and partial schemas only (`if`/`then`, `{properties: …}` with no type, an `allOf` of those) | attribute with no type, `:valueSchema: {...}` verbatim |
| a local definition that is a union with its own description, or that refers back to itself | the element named after the definition, built once |
| `$ref` to a local definition that is an array (cdifManifest's `resourcePartArray`) | the array, inline (the definition's own description gives way to the property's) |
| an array with `contains` and no `items` | attribute with no type, upper bound `*`, `contains` in `:arrayKeywords:` |
| block root | element named from the directory (`cdifConceptOrTermOrString` → `ConceptOrTermOrString`) |
| block root that is only a `$ref` to a local `$defs` entry (skosConcept) | that definition's element, plus `:rootAlias:` (and `:rootDescription:` for the root's own description) |
| `$defs` entry referenced from another block, or recursively | element named by its key, `:exportedDef:` |

**Properties**

| JSON Schema | XMI |
|---|---|
| property `p:name` (same prefix as the class) | attribute `name` |
| property `q:name` (another prefix) | attribute `q_name`, as in `cdifmodels.xmi`, plus `:jsonName: "q:name"` |
| `string` / `integer` / `boolean` / `number` | UML primitive `String` / `Integer` / `Boolean` / `Real` |
| `string` + `format: uri` / `date` / `date-time` | `XMLSchemaDataTypes.XsdAnyUri` / `XsdDate` / `XsdDateTime` |
| `anyOf: [string, {"@id": string}]` (IRI reference), in either order | `common.IriReference` (**new DataType**); `:referenceFirst:` when `{"@id"}` comes first, `:idRefDescription:` for a description on its `@id` |
| `anyOf` of a value X (or several) plus a plain `string` and/or an `{"@id"}` reference (`[SpatialExtent, string, {"@id"}]`, `[ComputationalTool, {"@id"}]`) | typed by X, so an association when X is a Class (several values: a union of just those); `:orReference: ["value", "string", "idReference"]` gives the alternatives in order, an entry `{"idReference": {"description": …}}` where an alternative has its own description. A `$ref` to the `cdifDataType/objectReference` block counts as a reference too (`"objectReference"`); beside only scalars it is the value. Not for array alternatives. |
| `{"@id": string}` object, nothing else (node reference) | `common.IdReference` (**new DataType**); a description on its `@id` → `:idRefDescription:` |
| `$ref: ../X/schema.yaml` (another building block) | linked mode: `href` into X's file; single-file mode: stub element with `:buildingBlock:` |
| `$ref: '#/$defs/X'` | followed to the definition, then mapped like any property schema |
| `$ref: ../X/schema.yaml#/$defs/K` | the type of K's element in X's file, plus `:schemaRef:` with the reference |
| `{}` (any value) | attribute with no type |
| `type: [string, number, …]` (scalars) | `String` plus `:keywords: {"type": [...]}` |
| a union alternative's annotations that differ from the union's first use | `:alternativeOverrides: {alternative: {key: value or null}}` |
| `description` beside a `$ref` / node reference (the value's, not the property's) | `:valueDescription:` |
| a property typed by a `uml:Class` | navigable end of a `uml:Association` named `<Owner>_<role>_<Target>`; the association owns the other end, `0..*`. A `$ref` to a Class is embed-only: `:inlineOrByReference: ``inline```. |
| `anyOf: [X, string]` / `[string, X]`, X a `$ref` | type of X plus `:alsoAcceptsString:` (` ``first`` ` when string is first) |
| `type: array`, `items: X` | type of X, upper bound `*`; `minItems` the lower bound doesn't imply → `:minItems:` |
| in `required` | lower bound `1` (else `0`) |
| `description` | `ownedComment` body after the `**CDIF** / Definition` header, verbatim; on `items` → `:descriptionOnItems:`, or `:itemsDescription:` if the array has one too |
| `default` | `:default:` (on items: `:itemsDefault:`) |
| `title`, `minLength`, `maxLength`, `pattern`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` | `:keywords: {...}` (on the array itself: `:arrayKeywords:`) |
| `contains`, `minContains`, `maxContains`, `maxItems`, `uniqueItems` on an array | `:arrayKeywords: {...}`, verbatim |
| a value that is only `if` / `then` / `else` (cdifProvActivity's `prov:used` items) | attribute with no type, `:valueSchema: {...}` verbatim |
| enum literal with whitespace or control characters (`"\r\n"`) | literal name written with character references (`&#13;&#10;`), which XML parsing keeps |

**Class-level**

| JSON Schema | Class directive |
|---|---|
| `@type` containing one type / one of several / all of several | `:rdfType: ``p:T```, ``p:T | p:U``, ``p:T & p:U``; the element's package is `p`. "One of" may be written `contains: {const}`, `{anyOf}`, `{enum}` or `anyOf [{contains}]`. |
| `@type` items restricted to an enum | `:allowedTypes: ``p:T | p:U``` |
| `default` / `description` on `@type` | `:typeDefault:` (written back as an array, so a bare-string default comes back wrapped) / `:typeDescription:` |
| `@type` declared but not required | `:typeOptional:` |
| `@type` in any other form | `:typeSchema: {...}`, verbatim |
| `description` on `@id` | `:idDescription:` |
| `title` | `:title:` |
| no `@type`: prefix of the property keys | `:prefix: ``p``` |
| `anyOf: [{required: [a]}, {required: [b, c]}]` | `:choiceConstraints:` then `- ``a | b & c``` (JSON keys) |
| other `allOf` members (e.g. `if` / `then` / `else`) | `:constraint: {...}`, verbatim |
| top-level `if` / `then` / `else`, `not`, or `oneOf` beside `properties` | one `:constraint: {...}` (written back as an `allOf` member, which means the same) |
| `required` keys with no property schema | `:constraint: {"required": [...]}` |
| a property schema with no type that refines a property defined elsewhere (`schema:subjectOf: {properties: {dcterms:conformsTo: …}}`) | `:constraint: {"properties": {...}}` (written back as an `allOf` member, which means the same) |
| `@context` required | `:contextRequired:` |
| `allOf: [{$ref: other block}, …, {properties…}]` | a generalization to each block's root, `:extends:`; `:untypedRoot:` when the root has no `type: object`, `:noOwnSchema:` when there are only `$ref`s |
| `additionalProperties: false` | `:closed:` |
| `@id` declared on a DataType (by classification, e.g. objectReference) / `@id` required | `:hasId:` / `:idRequired:` |
| a union block that also says `type: object` (cdifStatistics) | `:objectUnion:` |
| an `@context` property | `:contextSchema: {...}`, verbatim |

Anything else stops the converter with an error, rather than being dropped.

`$ref`s inside verbatim JSON (`:constraint:`, `:valueSchema:`) are written as
`bb:<path under _sources>/schema.yaml[#fragment]`, local aliases of other blocks followed;
`uml_to_schema.py` makes them relative to wherever it writes the schema.

## Results

**All 58 blocks round-trip** (`roundtrip.py --linked`): `schemaorgProperties`,
`skosProperties`, `provProperties`, `qualityProperties`, `bioschemasProperties`,
`cdifDataType` and `profiles/cdifProfile`, with no semantic differences. (The 20 bare-string `@type` defaults that
used to come back wrapped as arrays are now arrays in the sources, as the always-an-array
`@type` policy requires.) Exact differences are placement only: `required` and choice
`anyOf`s at the top level or in `allOf`, and unions or nested objects written as local
`$defs`.

Earlier checks, from when the set was identifier, person and organization: the examples
and edge cases (2 + 4, 2 + 11, 2 + 9) validate the same way against the original and
regenerated schemas, with `$ref`s resolved from disk.

The set converts to 61 EA XMI 1.1 files (58 blocks plus the common types, shared types and
shared unions) with 180 associations and 43 generalizations (to the abstract shared bases,
and the extensions of other blocks); every reference resolves to an element. Import order follows the references: common types,
shared types, then each block (and the shared unions) before the blocks that reference it;
cdifStatistics' five associations to InstanceVariable are written in cdifInstanceVariable's
file (see above). The set has 39 union datatypes; 29 more value-or-reference unions became plain types and
associations (`:orReference:`, `common.IriReference`).

Without the opt-in flags, the identifier round trip had 14 differences and rejected both
of its own examples: XSD types and `IriReference` became JSON-LD node objects, `@type`
became `schema:Identifier`, the value-or-url constraint was dropped, and descriptions
were reflowed.

The flags don't change `uml_to_schema.py`'s default behaviour. Its default outputs were
byte-identical before and after each change, checked on five schema runs over the EA
DDI-CDI XMI and `cdifmodels.xmi` (single-class, multi-class with `--emit-uml`,
`--strict-required --inline-datatypes`) and one `--config` run emitting canonical and EA XMI.

## Not handled yet

- `ddiProperties`, `xasProperties` and the composite profiles are untried; `ddiProperties`
  will add inheritance.
- cdifInstanceVariable's `allOf` `$ref` to variableMeasured (beside its own properties) is
  carried as a `:constraint:`, not as a generalization, so EA shows no link between them.
- Each building block is its own XMI file; there's no merged model of several blocks.
- Elements sharing an RDF type are different shapes in schemaorgProperties, so the shared
  bases hold almost nothing yet. Identical copies elsewhere (e.g. `cdi:TypedString`, identical
  in 10 blocks) should become one shared element rather than base plus copies.
- `:constraint:`, `:typeSchema:` and `:contextSchema:` carry JSON verbatim; a UML
  `ownedRule` would be the formal home for constraints.
