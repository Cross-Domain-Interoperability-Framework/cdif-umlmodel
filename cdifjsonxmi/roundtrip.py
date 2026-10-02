"""Round-trip a building-block schema: schema.yaml -> bblock_to_xmi.py -> uml_to_schema.py -> schema.yaml.

Writes to roundtrip/<bblock name>/: the generated XMI, the regenerated schema.yaml, and
diff.txt listing every path where the regenerated schema differs from the original
(key order ignored). Exits 1 if there are differences.

Usage:
    python roundtrip.py <bblock dir>
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from bblock_to_xmi import Unmapped, build

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
    - local #/$defs/X refs are inlined and $defs dropped;
    - a one-branch anyOf is replaced by its branch;
    - in every object schema, 'required' and choice 'anyOf's from the top level
      and from allOf members are gathered into one place."""
    defs = schema.get("$defs", {})

    def walk(node, depth=0):
        if isinstance(node, list):
            return [walk(x, depth) for x in node]
        if not isinstance(node, dict):
            return node
        node = dict(node)
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/") and depth < 20:
            node.pop("$ref")
            node = {**defs[ref[len("#/$defs/"):]], **node}
            return walk(node, depth + 1)
        if isinstance(ref, str) and not ref.startswith("#"):
            node["$ref"] = (base_dir / ref).resolve().as_posix()
        if set(node) == {"anyOf"} and len(node["anyOf"]) == 1:
            return walk(node["anyOf"][0], depth)
        node.pop("$defs", None)
        node = {k: walk(v, depth) for k, v in node.items()}
        if node.get("type") == "object" and "properties" in node:
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
        return node

    return walk(schema)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bblock_dir", type=Path)
    args = ap.parse_args()

    bb_name = args.bblock_dir.resolve().name
    out_dir = HERE / "roundtrip" / bb_name
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        class_name, pkg, xml, warnings = build(args.bblock_dir)
    except Unmapped as e:
        sys.exit(f"cannot map to XMI: {e}")
    for msg in warnings:
        print(f"warning: {msg}", file=sys.stderr)
    xmi = out_dir / f"{class_name}.xmi"
    xmi.write_text(xml, encoding="utf-8")

    cmd = [sys.executable, str(UML_TO_SCHEMA), "--xmi", str(xmi), "--class", class_name,
           "--bb-name", "regenerated", "--out-dir", str(out_dir), "--prefix", pkg,
           "--title", class_name, "--strict-required", "--schema-only",
           "--xsd-formats", "--iri-reference-type", "IriReference",
           "--comment-directives", "--verbatim-docs"]
    run = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    if run.returncode:
        sys.exit(f"uml_to_schema.py failed:\n{run.stderr}")

    original = yaml.safe_load((args.bblock_dir / "schema.yaml").read_text(encoding="utf-8"))
    regenerated = yaml.safe_load((out_dir / "regenerated" / "schema.yaml").read_text(encoding="utf-8"))
    diffs = list(diff(original, regenerated))
    semantic = list(diff(normalize(original, args.bblock_dir.resolve()),
                         normalize(regenerated, (out_dir / "regenerated").resolve())))
    report = "\n".join([f"{bb_name}: {len(diffs)} exact difference(s)", *diffs, "",
                        f"{bb_name}: {len(semantic)} semantic difference(s) (file $refs resolved, "
                        "local $defs inlined, required/anyOf placement normalized)", *semantic]) + "\n"
    (out_dir / "diff.txt").write_text(report, encoding="utf-8")
    print(report, end="")
    sys.exit(1 if semantic else 0)


if __name__ == "__main__":
    main()
