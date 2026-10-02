# cdifjsonxmi

Experiment: generate Canonical XMI (UML 2.5 / XMI 2.5.1) from CDIF building-block JSON
Schemas, regenerate the schema from that XMI with the production tool
`metadataBuildingBlocks/tools/uml_to_schema.py`, and check whether the round trip is
isomorphic. The XMI follows the conventions of `../xmiModels/cdifmodels/cdifmodels.xmi`.

```bash
pip install pyyaml
python roundtrip.py <metadataBuildingBlocks>/_sources/schemaorgProperties/person
```

`roundtrip.py` runs `bblock_to_xmi.py` (schema.yaml → XMI), then `uml_to_schema.py`
(XMI → schema.yaml), and diffs the result against the original. It calls `uml_to_schema.py`
with `--prefix <class package> --strict-required --schema-only` plus the opt-in round-trip
flags `--xsd-formats --iri-reference-type IriReference --comment-directives --verbatim-docs`
(documented in metadataBuildingBlocks `agents.md`). Output goes to `roundtrip/<bblock>/`:
the XMI, `regenerated/schema.yaml`, and `diff.txt`.

`diff.txt` has two sections. *Exact* compares the schemas as JSON values, ignoring key
order. *Semantic* first rewrites both into a form with the same meaning: file `$ref`s
resolved to absolute paths, local `#/$defs/` references inlined, and `required` / choice
`anyOf`s gathered from `allOf`. The script exits 1 if any semantic difference remains.
It assumes `metadataBuildingBlocks` is a sibling of this repo. Pass `--uml-to-schema PATH`
otherwise.

`bblock_to_xmi.py` can also be run on its own to produce just the XMI. It fails on any
construct outside the mapping below, instead of approximating it.

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
| `anyOf: [{required: [a]}, {required: [b, c]}]` | class directive `:choiceConstraints:` then `- ``a | b & c``` |
| property `p:name` (same prefix as the class) | attribute `name` |
| property `q:name` (another prefix) | attribute `q_name`, as in `cdifmodels.xmi` (e.g. `cdi_identifier`) |
| `string` / `integer` / `boolean` / `number` | UML primitive `String` / `Integer` / `Boolean` / `Real` |
| `string` + `format: uri` / `date` / `date-time` | `XMLSchemaDataTypes.XsdAnyUri` / `XsdDate` / `XsdDateTime` |
| `anyOf: [string, {"@id": string}]` (the JSON-LD IRI-reference pattern) | `common.IriReference` (**new DataType**, not in `cdifmodels.xmi`) |
| `$ref: ../X/schema.yaml` (another building block) | attribute typed by a stub element named from X's `bblock.json`, with class directive `:buildingBlock: ``<path under _sources>``` |
| `$ref` to a building block that is a Class | attribute directive `:inlineOrByReference: ``inline```, because the schema embeds the node and doesn't accept `{"@id"}` (tag name from OGC UML-to-JSON rules) |
| `anyOf: [{$ref: ...}, {type: string}]` | attribute directive `:alsoAcceptsString:` |
| inline `type: object` with an `@type` const | nested element named from the const (e.g. `ContactPoint`) |
| `type: array`, `items: X` | type of X, upper bound `*` |
| in `required` | lower bound `1` (else `0`) |
| `description` | `ownedComment` body after the `**CDIF** / Definition` header, kept verbatim |
| `default` on `@type` | dropped, with a warning |

## Results

Both building blocks validate the same way against the original and regenerated schemas.
The remaining differences are annotations.

| | identifier | person |
|---|---|---|
| exercises | scalars, URI, IRI reference, choice constraint | plus `$ref` to a DataType BB (or string), embed-only `$ref` to a Class BB, inline nested object, array of IRI references |
| semantic differences | `@id` added; `title` added | `@id` description added; `title` added; `@type` `default` on `contactPoint` lost |
| examples + edge cases validating the same | 2 + 4 | 2 + 11 |

- `uml_to_schema.py` adds `@id` (with a description) to every class node, and to a root
  emitted from a DataType, such as Identifier. Neither original sets
  `additionalProperties: false`, so this changes no validation result.
- Person's edge cases cover: name only; identifier as a string or as a PropertyValue;
  neither name nor identifier; affiliation as `{"@id"}` only (rejected by both) or embedded;
  contactPoint without email or without `@type`; `sameAs` with `{"@id"}` items or not an
  array; wrong `@type`. `$ref`s were resolved from disk, including organization's own
  references.

Without the opt-in flags, the identifier round trip had 14 differences and rejected both
of its own examples: XSD types and `IriReference` became JSON-LD node objects, `@type`
became `schema:Identifier`, the value-or-url constraint was dropped, and descriptions
were reflowed.

The flags don't change `uml_to_schema.py`'s default behaviour. Its default outputs were
byte-identical before and after each change, checked on five schema runs over the EA
DDI-CDI XMI and `cdifmodels.xmi` (single-class, multi-class with `--emit-uml`,
`--strict-required --inline-datatypes`) and one `--config` run emitting canonical and EA XMI.

## Not handled yet by bblock_to_xmi.py

Local `#/$defs` references (e.g. organization's `additionalType`), `@type` with an `enum`
of allowed types (organization), `oneOf`/`anyOf` unions other than the patterns above,
single-value-or-array unions, `enum`, `const` values, `minItems`/`maxItems` other than on
`@type`, and `pattern`. Each building block becomes its own XMI. Referenced blocks appear
only as stubs, so there's no merged model of several blocks yet.

The generated XMI hasn't been imported into Enterprise Architect.
