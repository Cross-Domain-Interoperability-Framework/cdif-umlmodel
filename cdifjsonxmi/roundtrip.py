"""Round-trip building-block schemas: schema.yaml -> bblock_to_xmi.py -> uml_to_schema.py -> schema.yaml.

Single-file mode writes to roundtrip/<bblock name>/: the generated XMI (with stubs for
referenced blocks), the regenerated schema.yaml, and diff.txt. Linked mode writes one
linked XMI file per block under roundtrip/linked/<path under _sources>/, regenerates each
block from its own file (following hrefs into the others), and writes regenerated/ and
diff.txt beside each file. Exits 1 if any semantic difference remains.

Usage:
    python roundtrip.py <bblock dir>
    python roundtrip.py --linked <bblock dir> [<bblock dir> ...]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from bblock_to_xmi import Unmapped, build, write_linked

HERE = Path(__file__).resolve().parent
UML_TO_SCHEMA = HERE / "uml_to_schema.py"


def diff(a, b, path="$"):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                yield f"{path}.{k}: only in regenerated: {json.dumps(b[k])[:200]}"
            elif k not in b:
                yield f"{path}.{k}: only in original: {json.dumps(a[k])[:200]}"
            else:
                yield from diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff(x, y, f"{path}[{i}]")
    elif a != b:
        yield f"{path}:\n    original:    {json.dumps(a)[:300]}\n    regenerated: {json.dumps(b)[:300]}"


def normalize(schema, base_dir):
    """Rewrite a schema into a canonical form that keeps its meaning:
    - file $refs become absolute paths (resolved against base_dir), so the same
      target written from different directories compares equal;
    - local #/$defs/X refs are inlined and $defs dropped; a recursive one (X inside X) stays a
      $ref, which then only matches the same recursion;
    - a one-branch anyOf is replaced by its branch; a branch that is itself only an anyOf is
      flattened into its parent, and anyOf / oneOf branches are sorted (both are unordered);
    - an array's "contains one of these types" is written one way: contains {enum: [...]},
      contains {anyOf: [{const}]} and anyOf [{contains: {const}}] all become
      contains {anyOf: [{const}, ...]} with the consts sorted;
    - in every object schema, 'required' and choice 'anyOf's from the top level
      and from allOf members are gathered into one place, and a top-level if / then / else,
      not or oneOf (beside properties) becomes an allOf member; allOf members are sorted."""
    defs = schema.get("$defs", {})

    def walk(node, expanding=()):
        if isinstance(node, list):
            return [walk(x, expanding) for x in node]
        if not isinstance(node, dict):
            return node
        node = dict(node)
        ref = node.get("$ref")
        key = ref[len("#/$defs/"):] if isinstance(ref, str) and ref.startswith("#/$defs/") else None
        # A definition that is only a union is always expanded (a few times at most): one side
        # may name a union the other writes inline, and a cut there would come one level apart.
        union_def = key in defs and set(defs[key]) - {"description"} in ({"anyOf"}, {"oneOf"})
        if key in defs and (key not in expanding or (union_def and expanding.count(key) < 3)):
            node.pop("$ref")
            node = {**defs[key], **node}
            return walk(node, expanding + (key,))
        if key is not None:
            # a recursive reference: cut, without the definition's name (the two sides may name
            # the same definition differently)
            return {**{k: v for k, v in node.items() if k != "$ref"}, "$recursive": True}
        if isinstance(ref, str) and not ref.startswith("#"):
            node["$ref"] = (base_dir / ref).resolve().as_posix()
        if node.get("items") == {}:
            node.pop("items")  # items: {} allows any item, as no items does
        if isinstance(node.get("enum"), list):
            # an enum is a set (canonical XMI orders enumeration literals by identifier)
            node["enum"] = sorted(node["enum"], key=lambda v: json.dumps(v, sort_keys=True))
        if isinstance(node.get("description"), str):
            node["description"] = node["description"].rstrip()  # (canonical XMI trims comment ends)
        if set(node) == {"anyOf"} and len(node["anyOf"]) == 1:
            return walk(node["anyOf"][0], expanding)
        contains = node.get("contains")
        if isinstance(contains, dict) and set(contains) == {"enum"}:
            node["contains"] = {"anyOf": [{"const": v} for v in contains["enum"]]}
        if isinstance(node.get("anyOf"), list) and node["anyOf"] \
                and all(isinstance(b, dict) and set(b) == {"contains"} for b in node["anyOf"]) \
                and "contains" not in node:
            consts = []
            for b in node.pop("anyOf"):
                c = b["contains"]
                consts += c["anyOf"] if set(c) == {"anyOf"} else [c]
            node["contains"] = {"anyOf": consts}
        node.pop("$defs", None)
        if isinstance(node.get("allOf"), list):
            # hoist members' own allOf first, so a member doesn't gather its nested members'
            # 'required' as its own before they join the parent
            flat = []
            for m in node["allOf"]:
                if isinstance(m, dict) and isinstance(m.get("allOf"), list) and "$ref" not in m:
                    flat += m["allOf"]
                    m = {k: v for k, v in m.items() if k != "allOf"}
                    if m:
                        flat.append(m)
                else:
                    flat.append(m)
            node["allOf"] = flat
        node = {k: walk(v, expanding) for k, v in node.items()}
        for key in ("anyOf", "oneOf"):
            if isinstance(node.get(key), list):
                flat = []
                for b in node[key]:
                    # flattening is only equivalent for anyOf
                    flat += b[key] if key == "anyOf" and isinstance(b, dict) and set(b) == {key} else [b]
                node[key] = sorted(flat, key=lambda b: json.dumps(b, sort_keys=True))
        contains = node.get("contains")
        if isinstance(contains, dict) and set(contains) == {"anyOf"} and len(contains["anyOf"]) == 1:
            node["contains"] = contains["anyOf"][0]
        if "if" in node or "not" in node or (node.get("type") == "object" and "properties" in node):
            # a top-level conditional (or, beside properties, an exclusive choice) means the same
            # as an allOf member
            keys = ("if", "then", "else", "not") + (("oneOf",) if "properties" in node else ())
            lifted = {k: node.pop(k) for k in keys if k in node}
            if lifted:
                node["allOf"] = node.get("allOf", []) + [lifted]
        if isinstance(node.get("allOf"), list):
            # allOf is unordered, and a member's own allOf can join its parent's
            flat = []
            for m in node["allOf"]:
                if isinstance(m, dict) and isinstance(m.get("allOf"), list):
                    flat += m["allOf"]
                    m = {k: v for k, v in m.items() if k != "allOf"}
                    if m:
                        flat.append(m)
                else:
                    flat.append(m)
            node["allOf"] = sorted(flat, key=lambda b: json.dumps(b, sort_keys=True))
        if node.get("type") == "object" and "allOf" in node:
            # an allOf member that only adds property schemas means the same as those properties
            # on the object itself (cdifProvenance's partial schema:subjectOf), if not already there
            kept = []
            for block in node["allOf"]:
                if isinstance(block, dict) and set(block) == {"properties"} \
                        and not set(block["properties"]) & set(node.get("properties", {})):
                    node["properties"] = {**node.get("properties", {}), **block["properties"]}
                else:
                    kept.append(block)
            if kept:
                node["allOf"] = kept
            else:
                node.pop("allOf")
        if node.get("type") == "object":
            req, anyofs = set(node.pop("required", [])), []
            if "anyOf" in node:
                anyofs.append(node.pop("anyOf"))
            for block in node.pop("allOf", []):
                block = dict(block)
                req.update(block.pop("required", []))
                if "anyOf" in block:
                    anyofs.append(block.pop("anyOf"))
                if block:
                    node.setdefault("allOf", []).append(block)
            if req:
                node["required"] = sorted(req)
            if anyofs:
                node["anyOf"] = sorted(json.dumps(a, sort_keys=True) for a in anyofs)
            if "allOf" in node:
                node["allOf"] = sorted(node["allOf"], key=lambda b: json.dumps(b, sort_keys=True))
        return node

    return walk(schema)


def regenerate_and_compare(bblock_dir, xmi, class_name, pkg, out_dir, linked=False):
    """Regenerate bblock_dir's schema from xmi into out_dir/regenerated/, write out_dir/diff.txt,
    print the report, and return the number of semantic differences."""
    bb_name = bblock_dir.resolve().name
    cmd = [sys.executable, str(UML_TO_SCHEMA), "--xmi", str(xmi), "--class", class_name,
           "--bb-name", "regenerated", "--out-dir", str(out_dir), "--prefix", pkg,
           "--strict-required", "--schema-only",
           "--xsd-formats", "--iri-reference-type", "IriReference", "--id-reference-type", "IdReference",
           "--comment-directives", "--verbatim-docs"] + (["--linked"] if linked else [])
    run = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    if run.returncode:
        sys.exit(f"uml_to_schema.py failed:\n{run.stderr}")

    original = yaml.safe_load((bblock_dir / "schema.yaml").read_text(encoding="utf-8"))
    regenerated = yaml.safe_load((out_dir / "regenerated" / "schema.yaml").read_text(encoding="utf-8"))
    diffs = list(diff(original, regenerated))
    semantic = list(diff(normalize(original, bblock_dir.resolve()),
                         normalize(regenerated, (out_dir / "regenerated").resolve())))
    report = "\n".join([f"{bb_name}: {len(diffs)} exact difference(s)", *diffs, "",
                        f"{bb_name}: {len(semantic)} semantic difference(s) (file $refs resolved, "
                        "local $defs inlined, required/anyOf placement normalized)", *semantic]) + "\n"
    (out_dir / "diff.txt").write_text(report, encoding="utf-8")
    print(report, end="")
    return len(semantic)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bblock_dirs", type=Path, nargs="+")
    ap.add_argument("--linked", action="store_true",
                    help="write one linked XMI file per block and round-trip each from its own file")
    args = ap.parse_args()

    if args.linked:
        try:
            results = write_linked(args.bblock_dirs, HERE / "roundtrip" / "linked")
        except Unmapped as e:
            sys.exit(f"cannot map to XMI: {e}")
        failures = 0
        for d, (xmi, class_name, pkg, warnings) in results.items():
            for msg in warnings:
                print(f"warning: {msg}", file=sys.stderr)
            failures += bool(regenerate_and_compare(d, xmi, class_name, pkg, xmi.parent, linked=True))
        sys.exit(1 if failures else 0)

    if len(args.bblock_dirs) != 1:
        ap.error("single-file mode takes one building block (use --linked for several)")
    bblock_dir = args.bblock_dirs[0]
    out_dir = HERE / "roundtrip" / bblock_dir.resolve().name
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        class_name, pkg, xml, warnings = build(bblock_dir)
    except Unmapped as e:
        sys.exit(f"cannot map to XMI: {e}")
    for msg in warnings:
        print(f"warning: {msg}", file=sys.stderr)
    xmi = out_dir / f"{class_name}.xmi"
    xmi.write_text(xml, encoding="utf-8")
    sys.exit(1 if regenerate_and_compare(bblock_dir, xmi, class_name, pkg, out_dir) else 0)


if __name__ == "__main__":
    main()
