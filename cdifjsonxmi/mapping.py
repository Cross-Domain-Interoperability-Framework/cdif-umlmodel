"""Conventions used by bblock_to_xmi.py to recognize CDIF JSON Schema patterns."""

UML_PRIM = "http://www.omg.org/spec/UML/20161101/PrimitiveTypes.xmi#"
JSONLD_KEYWORDS = {"@type", "@id", "@context", "@reverse"}
PACKAGE_NAMES = {"schema": "Schema", "dcat": "DCAT", "dqv": "DQV", "skos": "SKOS", "spdx": "SPDX",
                 "time": "Time", "prov": "PROV", "common": "Common",
                 "XMLSchemaDataTypes": "XMLSchemaDataTypes"}
DEFINITION_HEADER = "**CDIF**\n\nDefinition\n==========\n\n"
CHOICE_LABEL = ":choiceConstraints:"


def attr_name(pname, class_prefix):
    """p:name -> name when p is the class's prefix, else p_name as cdifmodels.xmi does (cdi_identifier)."""
    prefix, _, local = pname.partition(":")
    return local if prefix == class_prefix else f"{prefix}_{local}"


def iri_reference_schema():
    return {"anyOf": [{"type": "string"},
                      {"type": "object", "additionalProperties": False, "required": ["@id"],
                       "properties": {"@id": {"type": "string"}}}]}


def is_iri_reference(prop):
    return {k: v for k, v in prop.items() if k != "description"} == iri_reference_schema()


def rdf_type_schema(rdf_type):
    return {"type": "array", "items": {"type": "string"}, "contains": {"const": rdf_type}, "minItems": 1}


def is_rdf_type_pattern(prop):
    const = prop.get("contains", {}).get("const")
    return const is not None and prop == rdf_type_schema(const)
