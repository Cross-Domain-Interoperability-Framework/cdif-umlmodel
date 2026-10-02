"""Compose the RST documentation body for a CDIF class / attribute / association.

Template (matches configuration/test.rst):

  Documentation
  =============

  Original
  --------

  Reference
  .........
  <sourceUri>

  Definition
  ..........
  <source-vocabulary rdfs:comment / skos:definition / prov:definition>

  Scope
  .....
  <subClassOf for classes; range / domain for properties>


  CDIF
  ----

  Definition
  ..........
  <CDIF-side description (JSON schema `description:` or config `definition:`)>

  Scope
  .....
  <UML-side subtype / supertype info; DataType note; multiplicity>

  Example
  .......
  <linked example file or short snippet — optional>

  Reference
  .........
  <cdifbook URL — optional>

Sections whose content is empty are omitted; the CDIF section is always
emitted (falling back to a "TBD" placeholder if all four fields are empty).
"""
from __future__ import annotations

from typing import Optional


def _same_text(a: Optional[str], b: Optional[str]) -> bool:
    """True if two text blocks are effectively identical (whitespace-normalized).
    Used to suppress a Scope section that just repeats the Definition."""
    if not a or not b:
        return False
    return " ".join(a.split()) == " ".join(b.split())


def _label_url(uri: Optional[str], label: Optional[str] = None) -> Optional[str]:
    """Format a URI and (optional) label for the Reference section."""
    if not uri:
        return None
    if label and label != uri:
        return f"{label} ({uri})"
    return uri


def _refs_line(refs: list, resolve) -> Optional[str]:
    """Turn a list of URIs into a comma-joined "label (uri)" line."""
    if not refs:
        return None
    parts = []
    for ref in refs:
        entry = resolve(ref) if resolve else None
        label = (entry or {}).get("label") if isinstance(entry, dict) else None
        parts.append(_label_url(ref, label))
    return ", ".join(p for p in parts if p)


def compose_documentation(
    *,
    source_uri: Optional[str] = None,
    source_lookup=None,
    cdif_definition: Optional[str] = None,
    cdif_scope: Optional[str] = None,
    cdif_example: Optional[str] = None,
    cdif_reference: Optional[str] = None,
    kind: str = "class",  # "class" | "attribute" | "association"
    owner_class: Optional[str] = None,
) -> str:
    """Build the RST body from lookups and the CDIF-side fields.

    `source_lookup` is either None or the dict from
    `definition_lookup.load_definition_lookup()`. `source_uri` selects the
    entry to pull from it. If either is missing, the Original section is
    omitted.
    """
    parts: list[str] = []
    parts.append("Documentation")
    parts.append("=============")
    parts.append("")

    # ----- Original section -----
    entry = None
    if source_uri and source_lookup is not None:
        # Support both compact-prefix and full-URI forms.
        from definition_lookup import _expand  # tools/ is on sys.path when run from build-docs
        entry = source_lookup.get(_expand(source_uri))
        # Policy: `cdif:X` is a CDIF-modified `cdi:X` term. Fall back to the
        # `cdi:X` entry as the upstream source when the `cdif:X` URI itself
        # has no definition — the Reference stays as the cdif: URI but the
        # Definition/Scope come from the DDI-CDI source class or property.
        # DDI-CDI associations are keyed as `cdi:<Source>_<role>_<Target>` in
        # the canonical XMI, so for `cdif:isDefinedBy_RepresentedVariable`
        # (or the equivalent `cdi:has_Statistics` where the target-qualified
        # form matches a DDI-CDI association) on class DataStructureComponent
        # we try `cdi:DataStructureComponent_isDefinedBy_RepresentedVariable`
        # first, then plain `cdi:X`, then progressive trailing-segment stripping.
        # Applied whenever the URI is cdi: or cdif: and the direct lookup misses.
        needs_fallback = (
            (entry is None or not entry.get("definition"))
            and (source_uri.startswith("cdif:") or source_uri.startswith("cdi:"))
        )
        if needs_fallback:
            prefix_len = len("cdif:") if source_uri.startswith("cdif:") else len("cdi:")
            local = source_uri[prefix_len:]
            candidates = []
            if owner_class:
                candidates.append(f"{owner_class}_{local}")
            candidates.append(local)
            work = local
            while "_" in work:
                work = work.rsplit("_", 1)[0]
                if owner_class:
                    candidates.append(f"{owner_class}_{work}")
                candidates.append(work)
            for cand in candidates:
                fallback = source_lookup.get(_expand("cdi:" + cand))
                if fallback and fallback.get("definition"):
                    entry = fallback
                    break

    if source_uri or entry:
        parts.append("Original")
        parts.append("--------")
        parts.append("")
        parts.append("Reference")
        parts.append(".........")
        parts.append(source_uri or "")
        parts.append("")
        defn = (entry or {}).get("definition") if entry else None
        if defn:
            parts.append("Definition")
            parts.append("..........")
            parts.append(defn.strip())
            parts.append("")
        scope_lines = _build_source_scope(entry, kind, source_lookup)
        if scope_lines and not _same_text(defn, "\n".join(scope_lines)):
            parts.append("Scope")
            parts.append(".....")
            parts.extend(scope_lines)
            parts.append("")
        parts.append("")

    # ----- CDIF section -----
    parts.append("CDIF")
    parts.append("----")
    parts.append("")
    if cdif_definition:
        parts.append("Definition")
        parts.append("..........")
        parts.append(cdif_definition.strip())
        parts.append("")
    if cdif_scope and not _same_text(cdif_definition, cdif_scope):
        parts.append("Scope")
        parts.append(".....")
        parts.append(cdif_scope.strip())
        parts.append("")
    if cdif_example:
        parts.append("Example")
        parts.append(".......")
        parts.append(cdif_example.strip())
        parts.append("")
    if cdif_reference:
        parts.append("Reference")
        parts.append(".........")
        parts.append(cdif_reference.strip())
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _build_source_scope(entry, kind: str, source_lookup) -> list[str]:
    """Extract the Scope lines for the Original section. For classes, list
    parent classes (subClassOf); for attributes / associations that came from
    an rdf:Property, list rdfs:range / rdfs:domain."""
    if not entry:
        return []
    lines: list[str] = []
    parents = entry.get("parents") or []
    ranges = entry.get("ranges") or []
    domains = entry.get("domains") or []

    def _resolve(u):
        return source_lookup.get(u) if source_lookup else None

    if kind == "class":
        if parents:
            line = _refs_line(parents, _resolve)
            if line:
                lines.append(f"Parent class(es): {line}.")
    else:
        if domains:
            line = _refs_line(domains, _resolve)
            if line:
                lines.append(f"Domain: {line}.")
        if ranges:
            line = _refs_line(ranges, _resolve)
            if line:
                lines.append(f"Range: {line}.")
    return lines
