# cdif-umlmodel
This repository holds the UML models that define CDIF, and serve as the source for the syntactic implementations of each profile. 

The EAmodelFiles contains the EnterpriseArchitect renditions of UML models for CDIF.  Conventions will be developed to identify authoritative files that can be updated to update model documentation.

The xmiModels folder contains Canonical XMI exports of the CDIF UML models (UML 2.5 / XMI 2.5.1), with diagrams where available.

The tools folder contains the code and supporting documents for validating CDIF JSON-LD metadata instances: JSON schemas, ttl-encoded SHACL rules, and the frame and validate code with the JSON-LD framing document that transforms JSON into the expanded hierarchy expected by the JSON schemas. These files are mirrored daily from the [validation](https://github.com/Cross-Domain-Interoperability-Framework/validation) repository, which is the source of truth; edit them there, not here. See [tools/readme.md](tools/readme.md) for usage.

The cdifjsonxmi folder contains the XMI to JSON Schema generator (`uml_to_schema.py`) that produces the metadataBuildingBlocks building-block schemas and the UML profile models, plus an experiment that generates XMI from building-block JSON Schemas and checks the round trip. See [cdifjsonxmi/README.md](cdifjsonxmi/README.md).

The html files and supporting images for the https://cross-domain-interoperability-framework.github.io/cdif-umlmodel/ web pages are on the gh-pages branch in this repository.