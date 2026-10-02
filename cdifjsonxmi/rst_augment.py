"""Rewrite a ucmism2m mapping config's `definition` fields to RST-formatted
documentation bodies before the UML emitter runs.

For each class / attribute / association that has a `sourceUri` field, the
RST body is composed from:

  - **Original section**: `sourceUri` lookup in the source-vocab dictionary
    (schema.org / SKOS / PROV / DCAT / DCTerms / DQV).
  - **CDIF Definition**: description text from the profile's resolvedSchema.json,
    matched by property name (with prefix stripped).
  - **CDIF Scope**: the config's own `definition` field IF it says something
    that isn't already in the JSON schema description (otherwise omitted).
  - **CDIF Reference**: optional `cdifbookUri` field from the config.

The modified config dict is a shallow rewrite: only `definition` fields
change, in memory. The rest of the pipeline is untouched.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

# tools/ is on sys.path — see uml_to_schema when it imports us
from definition_lookup import load_definition_lookup, resolve as vocab_resolve
from rst_documentation import compose_documentation


# ---------- CDIF description lookup from resolvedSchema.json ----------

# JSON-LD prefix map: property names in resolvedSchema.json usually carry a
# prefix (`skos:prefLabel`, `schema:identifier`) that the UML config strips
# down to a local name (`prefLabel`, `identifier`). Support looking up either
# form.
_KNOWN_PREFIXES = ("skos:", "schema:", "cdi:", "cdif:", "dcterms:", "dcat:",
                   "prov:", "dqv:", "geo:", "time:", "owl:", "rdf:", "rdfs:")


def _local_name(key: str) -> str:
    for p in _KNOWN_PREFIXES:
        if key.startswith(p):
            return key[len(p):]
    return key


def _index_properties(node: Any, into: dict[str, str]) -> None:
    """Recursively walk a JSON schema. For every 'properties' subdict, record
    <local_name> -> description. Later occurrences overwrite earlier ones
    (deepest wins — usually the profile-specific description).

    Used as a fallback when no class-scoped lookup is available. Class-scoped
    lookups (via `_class_scoped_properties`) are preferred when the class
    corresponds to a top-level or $def entry."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for k, v in props.items():
                if not isinstance(v, dict):
                    continue
                desc = v.get("description")
                if isinstance(desc, str) and desc.strip():
                    into.setdefault(_local_name(k), desc.strip())
                    into.setdefault(k, desc.strip())
        for v in node.values():
            _index_properties(v, into)
    elif isinstance(node, list):
        for it in node:
            _index_properties(it, into)


def _class_scoped_properties(schema: Any) -> dict[str, dict[str, str]]:
    """Return a `{class_name: {local_name: description}}` map.

    Root-level `properties` belong to the profile's root class (recorded under
    the special key `""` — the caller resolves that to the root-class name).
    Each `$defs.<ClassName>.properties` block belongs to that class."""
    out: dict[str, dict[str, str]] = {}
    if not isinstance(schema, dict):
        return out
    # Root
    props = schema.get("properties")
    if isinstance(props, dict):
        out[""] = {}
        for k, v in props.items():
            if isinstance(v, dict):
                desc = v.get("description")
                if isinstance(desc, str) and desc.strip():
                    out[""][_local_name(k)] = desc.strip()
                    out[""][k] = desc.strip()
    # $defs
    for name, spec in (schema.get("$defs") or {}).items():
        if not isinstance(spec, dict):
            continue
        # A $def MAY be a wrapper around another $ref — resolve one hop
        target = spec
        if "allOf" in target:
            # look for a $ref sibling to allOf; skip for now — walk properties as declared
            pass
        dprops = target.get("properties")
        if not isinstance(dprops, dict):
            continue
        # register under both the raw $def name and its Cdif-stripped variant
        for cls_key in (name, name[4:] if name.startswith("Cdif") else name):
            out.setdefault(cls_key, {})
            for k, v in dprops.items():
                if isinstance(v, dict):
                    desc = v.get("description")
                    if isinstance(desc, str) and desc.strip():
                        out[cls_key].setdefault(_local_name(k), desc.strip())
                        out[cls_key].setdefault(k, desc.strip())
    return out


def build_cdif_lookup(resolved_schema_path: Path) -> tuple[Optional[str], dict[str, str], dict[str, str], dict[str, dict[str, str]]]:
    """Return (root_description, class_defs_by_name, attr_desc_by_name_flat,
    class_scoped_attr_desc).

    The flat `attr_desc_by_name` is kept for backwards compatibility (used
    for association-role lookups where the class context is ambiguous).
    `class_scoped_attr_desc` is preferred for attribute lookups because it
    disambiguates root-level `skos:prefLabel` (on the profile root class)
    from `skos:prefLabel` nested inside a $def (on a member class)."""
    if not resolved_schema_path.exists():
        return None, {}, {}, {}
    schema = json.loads(resolved_schema_path.read_text(encoding="utf-8"))
    root_desc = schema.get("description")
    class_defs: dict[str, str] = {}
    for name, spec in (schema.get("$defs") or {}).items():
        if isinstance(spec, dict):
            desc = spec.get("description") or spec.get("title")
            if isinstance(desc, str) and desc.strip():
                class_defs[name] = desc.strip()
                # Also index without the CDIF prefix (e.g. CdifCodelistConcept -> CodelistConcept, Concept)
                if name.startswith("Cdif") or name.startswith("cdif"):
                    class_defs[name[4:]] = desc.strip()
    attr_desc: dict[str, str] = {}
    _index_properties(schema, attr_desc)
    class_scoped = _class_scoped_properties(schema)
    return root_desc, class_defs, attr_desc, class_scoped


# ---------- The rewrite ----------

def _cdif_scope_if_novel(config_definition: Optional[str],
                        cdif_definition: Optional[str]) -> Optional[str]:
    """Return the config's `definition` as a scope note IF it says something
    the JSON schema description doesn't already contain. Otherwise None."""
    if not config_definition:
        return None
    if not cdif_definition:
        return config_definition
    # Trivial similarity check: if the config's first 40 chars appear anywhere
    # in the JSON description, treat it as duplicative.
    if config_definition[:40].strip() and config_definition[:40].strip() in cdif_definition:
        return None
    return config_definition


def augment_config_with_rst(
    cfg: dict,
    *,
    vocab_lookup: dict[str, dict],
    cdif_root_description: Optional[str],
    cdif_class_defs: dict[str, str],
    cdif_attr_desc: dict[str, str],
    cdif_class_scoped: Optional[dict[str, dict[str, str]]] = None,
) -> None:
    """Rewrite cfg["mapping"]["class"][*].definition (and attributes / associations)
    into RST-formatted bodies. In-place. No return value."""
    tm = cfg.get("transformation", {}).get("targetModel", {})
    profile_uri = tm.get("uri") or ""
    profile_root_class = None
    # Convention: the FIRST class in the mapping is the profile root; its JSON
    # schema description is the top-level `description:` of resolvedSchema.
    classes = cfg.get("mapping", {}).get("class") or []
    if classes:
        profile_root_class = classes[0].get("targetClass")

    for cls in classes:
        tc = cls.get("targetClass")
        if not tc:
            continue
        source_uri = cls.get("sourceUri")
        # CDIF definition: prefer JSON schema. For the profile-root class,
        # use the schema's top-level description.
        if tc == profile_root_class and cdif_root_description:
            cdif_def = cdif_root_description
        else:
            cdif_def = cdif_class_defs.get(tc) or cdif_class_defs.get("Cdif" + tc)
        cdif_scope = _cdif_scope_if_novel(cls.get("definition"), cdif_def)
        cdif_ref = cls.get("cdifbookUri")
        if source_uri or cdif_def or cls.get("definition"):
            body = compose_documentation(
                source_uri=source_uri,
                source_lookup=vocab_lookup,
                cdif_definition=cdif_def or cls.get("definition"),
                cdif_scope=cdif_scope,
                cdif_reference=cdif_ref,
                kind="class",
            )
            cls["definition"] = body

        for attr in cls.get("attribute") or []:
            aname = attr.get("name")
            if not aname:
                continue
            source_uri = attr.get("sourceUri")
            # Class-scoped lookup preferred. For root class the class-scoped
            # key is "" (per _class_scoped_properties). Fall back to the flat
            # attr_desc map if no class-scoped hit.
            cdif_def = None
            if cdif_class_scoped:
                key = "" if (tc == profile_root_class) else tc
                scoped = cdif_class_scoped.get(key)
                if scoped:
                    cdif_def = scoped.get(aname)
                # try a Cdif-prefixed alias as a last resort
                if cdif_def is None and tc != profile_root_class:
                    for alias in (f"Cdif{tc}", f"CdifCodelist{tc}"):
                        scoped = cdif_class_scoped.get(alias)
                        if scoped and aname in scoped:
                            cdif_def = scoped[aname]
                            break
            if cdif_def is None:
                cdif_def = cdif_attr_desc.get(aname)
            cdif_scope = _cdif_scope_if_novel(attr.get("definition"), cdif_def)
            cdif_ref = attr.get("cdifbookUri")
            if source_uri or cdif_def or attr.get("definition"):
                body = compose_documentation(
                    source_uri=source_uri,
                    source_lookup=vocab_lookup,
                    cdif_definition=cdif_def or attr.get("definition"),
                    cdif_scope=cdif_scope,
                    cdif_reference=cdif_ref,
                    kind="attribute",
                    owner_class=tc,
                )
                attr["definition"] = body

    for assoc in cfg.get("mapping", {}).get("association") or []:
        tname = assoc.get("targetAssociationName") or ""
        parts = tname.split("_")
        if len(parts) < 3:
            continue
        role_name = "_".join(parts[1:-1])
        source_uri = assoc.get("sourceUri")
        cdif_def = cdif_attr_desc.get(role_name)
        cdif_scope = _cdif_scope_if_novel(assoc.get("definition"), cdif_def)
        cdif_ref = assoc.get("cdifbookUri")
        subject_class = assoc.get("subjectClass") or (parts[0] if parts else None)
        if source_uri or cdif_def or assoc.get("definition"):
            body = compose_documentation(
                source_uri=source_uri,
                source_lookup=vocab_lookup,
                cdif_definition=cdif_def or assoc.get("definition"),
                cdif_scope=cdif_scope,
                cdif_reference=cdif_ref,
                kind="association",
                owner_class=subject_class,
            )
            assoc["definition"] = body


def find_resolved_schema(config_path: Path) -> Optional[Path]:
    """Given a mapping-config path, guess the profile's resolvedSchema.json path.

    Convention: `ddi-cdi2cdif<Profile>_mapping.json` -> profile slug is `cdif<Profile>`
    (lowercased first-letter), path is
    `<mBB>/_sources/profiles/cdifProfile/<slug>/resolvedSchema.json`.
    Also handles composite configs (ddi-cdi2<Composite>_mapping.json -> cdifCompositeProfile).
    """
    config_path = config_path.resolve()
    stem = config_path.stem  # 'ddi-cdi2cdifCodelist_mapping'
    if not stem.startswith("ddi-cdi2"):
        return None
    remainder = stem[len("ddi-cdi2"):].removesuffix("_mapping")
    # Find mBB root: config file is in <mBB or ucmism2m>/configuration/,
    # mBB is a sibling of ucmism2m.
    mbb_root = config_path.parent.parent.parent / "metadataBuildingBlocks"
    if not mbb_root.exists():
        # fallback: config in mBB configuration/
        mbb_root = config_path.parent.parent
    if remainder.startswith("cdif"):
        candidate = mbb_root / "_sources/profiles/cdifProfile" / remainder / "resolvedSchema.json"
        if candidate.exists():
            return candidate
    else:
        candidate = mbb_root / "_sources/profiles/cdifCompositeProfile" / remainder / "resolvedSchema.json"
        if candidate.exists():
            return candidate
    return None


def maybe_augment(cfg: dict, config_path: Path) -> None:
    """Convenience wrapper: build the vocab + CDIF lookups and rewrite in place.
    Silently no-ops if the vocab bundle isn't available yet."""
    try:
        vocab_lookup = load_definition_lookup()
    except Exception as e:  # pragma: no cover
        print(f"  RST-augment: vocab lookup unavailable ({e}); skipping")
        return
    if not vocab_lookup:
        return
    resolved_schema_path = find_resolved_schema(config_path)
    if resolved_schema_path:
        root_desc, class_defs, attr_desc, class_scoped = build_cdif_lookup(resolved_schema_path)
    else:
        root_desc, class_defs, attr_desc, class_scoped = None, {}, {}, {}
    augment_config_with_rst(
        cfg,
        vocab_lookup=vocab_lookup,
        cdif_root_description=root_desc,
        cdif_class_defs=class_defs,
        cdif_attr_desc=attr_desc,
        cdif_class_scoped=class_scoped,
    )
