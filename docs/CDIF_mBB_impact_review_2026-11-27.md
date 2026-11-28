# Impact of the CDIF 1.1 review reports on the metadata building blocks

Updated 27 November 2026. Checked against `metadataBuildingBlocks` (mBB) `main` at `5138a2d720`.

**Update, 28 November 2026:** H1 and H2 are fixed and on mBB `main` (`ca06b80c11`), and H3 turned out to be intended. Sections 1, 6 and 8 reflect this.

## Scope and method

- **Documents:** the six `.docx` files by Joachim Wackerow in this folder. Their text is unchanged since the first review, and `CDIF__Some_analyses_of_the_current_stage.zip` holds the same six files. `cdif_challenges.pdf` and `ea_relationship_matrix.pdf` were **not** reviewed.
- **The reports assess the published 1.1 artifacts.** mBB `main` is at 2.0 pre-release and has moved on, so each finding below was re-tested.
- **Schema findings:** a valid mBB example was changed to reproduce the finding and re-validated.
- **SHACL findings:** a small graph was run against the rule bundle built from the block's `$ref` graph. Controls were run where a "no result" could mean the shape never fired.
- References: "Schema A1" means item A1 of the JSON Schema review; "SHACL" the SHACL report; "IG" the guide-consistency report; "D06" a decision-sheet item.

## Bottom line

1. The two largest validation holes (files inside archives, and data structure subtypes) are now fixed; smaller ones remain open.
2. JSON Schema and SHACL still disagree on a handful of rules, in both directions.
3. Much of the reported breakage is already fixed on `main` (section 7).
4. The largest remaining items are vocabulary and modelling decisions that need the CDIF authors, not code.

**Correction to the first review:** it listed the namespace-sniffing shapes as broken. They had already been fixed. It also described other items as "plausibly live" without testing them; those are tested here.

## 1. Validation holes (invalid records pass)

| # | Finding, confirmed on `main` | Where | Suggested fix | Report |
|---|---|---|---|---|
| H4 | A `potentialAction` and a spatial coverage Place with no `@type` pass | `webAPI`, `spatialExtent` blocks | Require `@type`, as done for `temporalCoverage`; survey existing records first | Schema A5 |
| H5 | A codelist root and a codelist concept with no `@type` pass the schema. SHACL now checks untyped concepts; the untyped root was not tested against SHACL | `cdifCodelist/schema.yaml` | Require `skos:ConceptScheme` / `skos:Concept` | Schema 2.3 |
| H6 | `schema:creator: {}` passes | `cdifCore/schema.yaml` | Require `@list` | Schema B11 |
| H7 | `schema:dateModified: "yesterday"` passes the schema | `cdifCore/schema.yaml` | A date or date-time pattern that agrees with SHACL (see S1) | Schema 1.3 |
| H8 | SHACL never reports an archive (`schema:Collection`) distribution without files: the shape selects only nodes that already have `schema:hasPart`. The schema does enforce this | `cdifManifest/rules.shacl`, `CDIFArchiveHasPartShape` | Select `schema:Collection` distributions instead | SHACL 1.2 |
| H9 | A variable typed only `schema:PropertyValueSpecification`, with no name, is not checked by SHACL (the same node typed `PropertyValue` gets three findings) | `variableMeasured/rules.shacl` | Target both types | SHACL 1.7 |
| H10 | A language value carrying both `@type` and `@language` (illegal JSON-LD) passes | `LanguageTaggedValue` definitions | `additionalProperties: false` | Schema 1.3 |

### Closed after this review

| # | What changed | Left open |
|---|---|---|
| H1 | Archived files now get the tabular or data-cube rules their `@type` claims; "delimited and fixed width at once" fails the schema and SHACL | SHACL has no datatype checks, so a numeric delimiter or locator fails only the schema |
| H2 | `cdi:DataStructure` is now abstract (author decision): it may not appear, alone or beside a layout type. SHACL holds each layout to its component kinds | A Dimensional structure now requires a dimension (author decision), which rejects measure-only records. Structures consisting only of keys can no longer be written. The Croissant importer and the NeXus converter have been updated to match, and the XAS-CDIF release examples on `updates` pass. Two geochemistry examples still fail, which will matter when that repository moves its version pin (not rechecked since 27 November) |

**H3 withdrawn.** An empty `schema:variableMeasured` is deliberate: a record may use the data description profile and describe no variables, in which case no conformance is declared (commit `ce49f83baa`, made for existing ADA records). The first version of this review was wrong to list it as a hole. Whether to keep that behaviour is a choice for the authors (Schema A6).

## 2. Schema and SHACL disagree

| # | Case | Schema | SHACL |
|---|---|---|---|
| S1 | `dateModified: "2020"` | accepts | rejects |
| S2 | Grant with only a description | rejects | accepts |
| S3 | Organization with only `@id` and `@type` | rejects | accepts |
| S4 | `time:numericPosition` missing / written as `xsd:decimal` in RDF | missing is rejected | missing accepted; decimal rejected (message says "MUST have a numeric value") |
| S5 | Temporal intervals, after this week's fixes: one calendar bound mixed with one numeric bound; `TimePosition` without `time:hasTRS` | rejects | accepts |
| S6 | Free-text `temporalCoverage` string | accepts | warns |

Related behaviour that needs a choice:

- **S7.** A catalog record without `schema:about` is not recognised as a record. It is checked as a dataset and reported as missing identifier, date and so on, never as missing `about` (SHACL 1.2).
- **S8.** `schema:subjectOf` on a `skos:ConceptScheme` is a violation under the core and composite rules, although the SKOS profiles expect it there. It matters only when a scheme carrying a catalog record is validated with those rules (SHACL 3.2).
- **S9.** The Codelist, Concept Scheme and XAS core conformance checks have no severity, so they are **violations**. The other profiles' checks are warnings, which is the repository's stated policy (SHACL 3.5).
- **S10.** Dataset-level members of a collection are held to every core mandatory property, while the schema asks little of them. The repository documents this as deliberate. Files inside a distribution are already excluded.
- **S11.** One bad `cdif:physicalDataType` is reported twice by two overlapping shapes (SHACL 3.4).

## 3. Rules that reject reasonable records

| # | Rejected today | Report |
|---|---|---|
| T1 | A Long structure whose components are all given by `@id` | Schema A9 |
| T2 | An extra non-schema.org `@type` on a Dataset, such as `dcat:Dataset` | Schema B7 |
| T3 | A plain array of Persons as `schema:creator` (only `@list` is accepted) | Schema 2.3 |
| T4 | Catalog record `additionalType` written as the string `"dcat:CatalogRecord"` | Schema 1.3 |
| T5 | A language-tagged `skos:prefLabel` on a codelist, although its description allows it | Schema B1 |
| T6 | An array `@context` on a codelist or concept scheme | Schema B2 |

T2, T3 and T6 may be intended. If so, the descriptions should say so.

## 4. Descriptions that contradict the constraints

- Data description says it adds a `cdif:physicalDataType` requirement; the schema does not require it (Schema B3).
- Data description still announces `fileSize` and `fileSizeUofM`, which are not defined (B4).
- Discovery says it composes `cdifCore`; it does not, and `{}` is a valid discovery record (B6). Composites supply core, so the fix is the wording.
- `DimensionGroup` is now defined in the data structure schema, but **nothing references it**, so no record can use it (IG; D06).

## 5. Housekeeping

| Item | Count |
|---|---|
| Spelling errors in schema descriptions: becasue, defintion, orginator, prsent, resoruce, servcie, uset | 7 |
| `default: "missing"` placeholders | 3 |
| `minItems: 0` | 4 |
| `sh:minCount 0` | 17, in 6 files |
| Property shapes that constrain nothing (3 on data type blocks, 8 in `instrument`) | 11 |
| Shapes with several untagged `sh:message` values | 3 |
| `sh:prefixes` given as anonymous lists | 19 |
| Shape IRI namespaces in use | 5 |

The SPARQL branches matching names such as `"example defined term"` look like test code in production, but four blocks' basic examples are validated only through them. Do not delete them without replacing that coverage.

## 6. Decisions for the CDIF authors

| Topic | State on `main` |
|---|---|
| Empty variable list (Schema A6) | Allowed on purpose, without a conformance claim |
| Dimensional structure (D30) | A dimension is now required; should identifier components be excluded? |
| `cdif:` namespace | Terms are still defined nowhere (`https://w3id.org/cdif/Key` returns 404). `cdif` and `cdi` are not required in any `@context` |
| Terms placed in `cdi:` | Whether names such as `cdi:has_DataStructureComponent` exist in DDI-CDI 1.0 was **not** checked |
| Primary key (D02) | One key class now, but two properties: `cdif:hasPrimaryKey` on the dataset and `cdif:has_PrimaryKey` on the structure |
| Long structure (D30) | Declares a primary key but no foreign key |
| `DimensionGroup` (D06) | Wire it in or remove it |
| Unit of measure | Both `cdif:` and `cdi:simpleUnitOfMeasure` |
| Name and description | `cdif:name` beside `schema:name`; `cdi:description` beside `cdif:descriptiveText` |
| Variable concept | `cdif:uses` and `cdif:uses_Concept` |
| File size; checksum | `schema:contentSize` and `schema:size`; `spdx:checksum` and `cdi:fingerprint` |
| SKOS concept | Codelist concepts and concept scheme concepts require different properties |
| Catalog record | Typed `schema:Dataset`, so harvesters may count it as a second dataset |

The UML-model documents mostly concern the model, not the blocks. Their effect on mBB runs through these decisions: the model can follow only one side where guide and schema disagree (D01).

## 7. Reported for 1.1 and resolved on `main`

| Area | Resolved |
|---|---|
| Schema | Locator mappings require `cdi:locator`; untyped WebAPI distributions and variables rejected; a data structure record must attach a structure; `cdif:qualifies` accepts a measure component and rejects unknown types; no string defaults on arrays; unprefixed row and column counts gone |
| Vocabulary | Manifest and data description both use `cdi:` tabular terms; `prov:reagent` and `bios:` gone; `dqv:isMeasurementOf` and `time:intervalStartedBy` spelled correctly; components use `cdif:isDefinedBy_Variable`; foreign keys name their target with `cdif:references_PrimaryKey` |
| SHACL conformance | Manifest and data description checks fire; each profile has a conformance shape |
| SHACL coverage | Untyped codelist concepts checked; foreign key shape added; codelist has language uniqueness; DefinedTerms under `additionalType` checked |
| SHACL false alarms | Action-result downloads no longer need `contentUrl`; `"int"` and concept values accepted for physical data type; files inside a distribution not held to dataset rules; missing `url` on a CreativeWork and missing spatial geometry are warnings |
| Fixed earlier | Namespace-sniffing shapes; minimum name lengths and the Person/Organization name messages |
| Fixed 25–27 November | E-mail pattern; the dormant `temporalCoverage` rule; the `ProperInterval` contradiction; required `@type` on intervals and instants; non-standard `sh:filterShape` |

These reach published artifacts only at the next release. The 1.1 files that the reports assessed are unchanged.

## 8. Suggested order of work

1. **Sync the release repositories** with the H1 and H2 fixes.
2. **S1–S4 and S9**: small, clear schema/SHACL alignments.
3. **H4–H10, section 4 and section 5**: low-risk tidy-up. Check existing records before tightening anything.
4. **Section 6**: take to the CDIF authors before the 2.0 release.

## Not checked

Schema items A2, B8 and B10; the SHACL report's notes on `dcat:hadRole`, `schema:propertyID`, contact-point severity and creator lists; findings that concern only the implementation guides or the UML model; and the published 1.1 artifacts themselves.
