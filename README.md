# cdif-umlmodel
This repository holds the UML models that define CDIF, and serve as the source for the syntactic implementations of each profile. 

The EAmodelFiles contains the EnterpriseArchitect renditions of UML models for CDIF.  Conventions will be developed to identify authoritative files that can be updated to update model documentation.

The tools folders contain the code for generating JSON schema and SHACL rules to validate metadata instance. It also contains the frame and validate code and JSON-LD framing document that can transform JSON into the conventional serialization hierarchy expected by the JSON schema. 

The generatedJSON folder contains JSON schema generated from the UML model, useful to validate CDIF JSON serialized.  The schema expects json documents in a particularly expanded hierarchy, the frame and validate code in the tools directory can effect transformation into the correct structure. 

The generatedSHACL folder contains ttl-encoded SHACL rules for validating CDIF metadata instances. 

The HTML folder contains html files and supporting images for the https://Cross-Domain-Interoperability-Framework/cdif.github.io/cdif-umlmodel  web pages.