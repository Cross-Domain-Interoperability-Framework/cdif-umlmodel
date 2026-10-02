"""Report how building blocks reuse each other, explicitly ($ref) and implicitly (copies).

For linked XMI (bblock_to_xmi.py --linked), explicit $refs become hrefs between files.
Implicit reuse, the same RDF type or the same untyped shape defined in several blocks,
has no link at all; this report finds it so it can become one shared element.

Every object schema with an @type const (properties.@type.contains.const) is a
*definition* of that RDF type. Definitions are grouped by RDF type and, within a type, by
*shape*: the schema with annotations (description, title, $comment, examples, default)
removed. Each type defined in more than one block gets a recommendation:

  extract  - all definitions have one shape and no block has the type as its root:
             make one shared element and link to it
  link     - a block has the type as its root and other blocks copy it: link to that block
             (copies with a different shape are restrictions to review)
  review   - several shapes and no block owns the type: decide base element + restrictions

Usage:
    python reuse_report.py [--sources-dir DIR] [-o reuse_report.md]
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_SOURCES_DIR = HERE.parents[1] / "metadataBuildingBlocks" / "_sources"
ANNOTATIONS = {"$schema", "description", "title", "$comment", "examples", "default"}
MIN_UNTYPED_BLOCKS = 3  # untyped shapes reported when repeated in at least this many blocks


def strip_annotations(node):
    if isinstance(node, dict):
        return {k: strip_annotations(v) for k, v in node.items() if k not in ANNOTATIONS}
    if isinstance(node, list):
        return [strip_annotations(x) for x in node]
    return node


def shape_hash(node):
    return hashlib.sha1(json.dumps(strip_annotations(node), sort_keys=True,
                                   default=str).encode()).hexdigest()[:10]


def shape_diff(a, b, path=""):
    """Paths where two annotation-stripped shapes differ (a = reference shape)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in b:
                yield f"`{path}/{k}` missing"
            elif k not in a:
                yield f"`{path}/{k}` added: `{json.dumps(b[k])[:80]}`"
            else:
                yield from shape_diff(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from shape_diff(x, y, f"{path}[{i}]")
    elif a != b:
        yield f"`{path}`: `{json.dumps(a)[:60]}` → `{json.dumps(b)[:60]}`"


def rdf_type(node):
    t = node.get("properties", {}).get("@type") if isinstance(node.get("properties"), dict) else None
    c = t.get("contains") if isinstance(t, dict) else None
    return c.get("const") if isinstance(c, dict) and isinstance(c.get("const"), str) else None


def is_untyped_structure(node):
    """An object or union shape worth matching: has properties, or an anyOf/oneOf of objects."""
    if rdf_type(node):
        return False
    if isinstance(node.get("properties"), dict) and node["properties"]:
        return True
    branches = node.get("anyOf") or node.get("oneOf")
    return isinstance(branches, list) and any(isinstance(b, dict) and b.get("type") == "object"
                                              for b in branches)


def scan(sources):
    """Return (definitions, untyped, inbound $ref counts, root shape -> blocks, block count)."""
    definitions = collections.defaultdict(list)   # rdf type -> [(bb, path, shape, prop names)]
    untyped = collections.defaultdict(list)       # shape -> [(bb, path, sample)]
    inbound = collections.Counter()               # bb path -> file $refs pointing at it
    root_shapes = collections.defaultdict(list)   # shape of a block's whole schema -> blocks
    blocks = 0
    for schema_path in sorted(sources.rglob("schema.yaml")):
        bb = schema_path.parent.relative_to(sources).as_posix()
        if "archive" in bb.split("/"):
            continue
        try:
            doc = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        blocks += 1
        if isinstance(doc, dict):
            root_shapes[shape_hash(doc)].append(bb)

        def walk(node, path):
            if isinstance(node, list):
                for i, x in enumerate(node):
                    walk(x, f"{path}[{i}]")
                return
            if not isinstance(node, dict):
                return
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                target = (schema_path.parent / ref.split("#")[0]).resolve().parent
                try:
                    inbound[target.relative_to(sources.resolve()).as_posix()] += 1
                except ValueError:
                    pass
            t = rdf_type(node)
            if t:
                definitions[t].append((bb, path or "/", shape_hash(node), strip_annotations(node)))
            elif path and is_untyped_structure(node):
                untyped[shape_hash(node)].append((bb, path, strip_annotations(node)))
            for k, v in node.items():
                walk(v, f"{path}/{k}")

        walk(doc, "")
    return definitions, untyped, inbound, root_shapes, blocks


def recommend(defs):
    shapes = {s for _, _, s, _ in defs}
    roots = sorted({bb for bb, path, _, _ in defs if path == "/"})
    if roots:
        return "link", roots
    return ("extract" if len(shapes) == 1 else "review"), []


def report(sources):
    definitions, untyped, inbound, root_shapes, blocks = scan(sources)
    shared = {t: d for t, d in definitions.items() if len({bb for bb, *_ in d}) > 1}
    by_rec = collections.defaultdict(list)
    for t, d in shared.items():
        by_rec[recommend(d)[0]].append(t)
    repeated = {h: occ for h, occ in untyped.items() if len({bb for bb, *_ in occ}) >= MIN_UNTYPED_BLOCKS}

    out = ["# Building-block reuse report", "",
           f"Source: `{sources}`, {blocks} building blocks (archive excluded). "
           "Generated by `reuse_report.py`.", "",
           "## Summary", "",
           f"- Explicit reuse: {sum(inbound.values())} `$ref`s between building-block files, "
           f"into {len(inbound)} blocks. These become hrefs between linked XMI files.",
           f"- RDF types defined as object schemas: {len(definitions)}. "
           f"Defined in more than one block: **{len(shared)}**.",
           f"  - `extract` (identical copies, no owning block): {len(by_rec['extract'])}",
           f"  - `link` (a block owns the type; others copy it): {len(by_rec['link'])}",
           f"  - `review` (several shapes, no owning block): {len(by_rec['review'])}",
           f"- Untyped object/union shapes repeated in at least {MIN_UNTYPED_BLOCKS} blocks: {len(repeated)}.",
           "", "Shapes ignore `$schema`, `description`, `title`, `$comment`, `examples` and `default`. Local "
           "`$ref`s are compared as written, so two copies that name their `$defs` differently "
           "count as different shapes.", ""]

    for rec, heading in (("extract", "Extract: identical copies with no owning block"),
                         ("link", "Link: copies of a type some block owns"),
                         ("review", "Review: several shapes, no owning block")):
        types = sorted(by_rec[rec], key=lambda t: (-len({bb for bb, *_ in shared[t]}), t))
        out += [f"## {heading} ({len(types)})", ""]
        if not types:
            out += ["None.", ""]
            continue
        out += ["| RDF type | blocks | definitions | shapes | owning block |", "|---|---|---|---|---|"]
        for t in types:
            d = shared[t]
            _, roots = recommend(d)
            out.append(f"| `{t}` | {len({bb for bb, *_ in d})} | {len(d)} | {len({s for _, _, s, _ in d})} "
                       f"| {', '.join(f'`{r}`' for r in roots) or '-'} |")
        out.append("")
        for t in types:
            d = shared[t]
            out += [f"### `{t}`", ""]
            by_shape = collections.defaultdict(list)
            for bb, path, s, node in d:
                by_shape[s].append((bb, path, node))
            ordered = sorted(by_shape.items(), key=lambda kv: -len(kv[1]))
            reference = ordered[0][1][0][2]
            for i, (s, occ) in enumerate(ordered):
                label = "most common shape" if i == 0 else "differs from the most common shape"
                out.append(f"- shape `{s}` ({label}): {len(occ)} definition(s)")
                for bb, path, _ in occ:
                    out.append(f"  - `{bb}` `{path}`")
                if i:
                    diffs = list(shape_diff(reference, occ[0][2]))
                    out += [f"  - {x}" for x in diffs[:8]]
                    if len(diffs) > 8:
                        out.append(f"  - … {len(diffs) - 8} more")
            out.append("")

    out += [f"## Untyped shapes repeated in at least {MIN_UNTYPED_BLOCKS} blocks", ""]
    for h, occ in sorted(repeated.items(), key=lambda kv: -len({bb for bb, *_ in kv[1]})):
        sample = json.dumps(occ[0][2], sort_keys=True)
        owners = ", ".join(f"`{bb}`" for bb in root_shapes.get(h, []))
        owned = f" **Same shape as block {owners}**, so these copies could link to it." if owners else ""
        out += [f"- shape `{h}`: {len(occ)} occurrence(s) in {len({bb for bb, *_ in occ})} blocks.{owned} "
                f"Example `{occ[0][0]}` `{occ[0][1]}`:",
                f"  `{sample[:300]}{'…' if len(sample) > 300 else ''}`"]
    out += ["", "## Most-referenced building blocks (explicit `$ref`)", "",
            "| block | inbound `$ref`s |", "|---|---|"]
    out += [f"| `{bb}` | {n} |" for bb, n in inbound.most_common(25)]
    return "\n".join(out) + "\n", len(shared), dict((k, len(v)) for k, v in by_rec.items()), len(repeated)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources-dir", type=Path, default=DEFAULT_SOURCES_DIR)
    ap.add_argument("-o", "--output", type=Path, default=HERE / "reuse_report.md")
    args = ap.parse_args()
    text, n_shared, by_rec, n_untyped = report(args.sources_dir)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}: {n_shared} RDF types in more than one block {by_rec}; "
          f"{n_untyped} repeated untyped shapes")


if __name__ == "__main__":
    main()
