"""Turn a Canonical XMI model of the linked set, as exported from Enterprise Architect
(export_from_ea.ps1) and made canonical (to_canonical.py), back into the linked layout that
bblock_to_xmi.py --linked writes: one file per building block with register identifiers, plus
the common types, shared types and shared unions files. uml_to_schema.py --linked then
regenerates each block's schema from its file, and the files can be compared with the ones
bblock_to_xmi.py wrote.

    python canonical_to_linked.py roundtrip/from-ea/linkTestEA4_canonical-unique-names.xmi -o roundtrip/from-ea/linked

How the canonical model maps back:
  - a block's package is the one whose comment is its register URI
    (https://w3id.org/cdif/bbr/metadata/<path>, written by linked_to_ea.py), so the folder
    packages above it don't matter; cdifSharedTypes, cdifSharedUnions and cdifCommonTypes
    (Common, XMLSchemaDataTypes) are found by name;
  - each classifier gets its linked id back: <register id>.<Name>, cdif.shared.<Name>,
    cdif.union.<Name>, common.<Name>, XMLSchemaDataTypes.<Name>; attributes <element id>.<name>;
  - types and generalizations in other files become hrefs; an association end gets the
    association id bblock_to_xmi.py gives it, <register id>.assoc.<Owner>_<role>_<Target>.

Canonical XMI orders attributes and literals by identifier, so their order in a file can differ
from bblock_to_xmi.py's (schema order); nothing depends on it.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from lxml import etree

import bblock_to_xmi as b2x
from bblock_to_xmi import (COMMON_TYPES_FILE, REGISTER_URI, SHARED_PREFIX, SHARED_TYPES_FILE, SHARED_UNIONS_FILE,
                           UNION_NS)
from uml_to_schema import DEFAULT_SOURCES_DIR

XMI = "{http://www.omg.org/spec/XMI/20131001}"
CLASSIFIERS = ("uml:Class", "uml:DataType", "uml:Enumeration")
SUPPORT_PACKAGES = {"Common": "common", "XMLSchemaDataTypes": "XMLSchemaDataTypes"}


def comment(e):
    """The body of an element's own comment, or None. Canonical XMI trims trailing whitespace,
    which takes the blank lines off a bare definition header; they're put back."""
    c = e.find("ownedComment")
    body = c.findtext("body") if c is not None else None
    if body is not None and body == b2x.DEFINITION_HEADER.rstrip():
        body = b2x.DEFINITION_HEADER
    return body


class Canonical:
    """The canonical model, with each classifier's place in the linked layout."""

    def __init__(self, path, out_root, sources):
        self.root = etree.parse(str(path)).getroot()
        self.out_root, self.sources = Path(out_root).resolve(), Path(sources).resolve()
        self.by_id = {e.get(XMI + "id"): e for e in self.root.iter() if e.get(XMI + "id")}
        # an association end's association, by the association's memberEnd references: the ids of
        # unnamed associations collide in to-canonical-xmi's output (<package>-packagedElement)
        self.association_of = {m.get(XMI + "idref"): e for e in self.root.iter("packagedElement")
                               if e.get(XMI + "type") == "uml:Association" for m in e.findall("memberEnd")}
        self.files = {}      # file key -> {"kind": block|shared|union, "bb_path", "elements": [canonical]}
        self.new_id = {}     # canonical classifier id -> (file key, linked id)
        for pkg in self.root.iter("packagedElement"):
            if pkg.get(XMI + "type") != "uml:Package":
                continue
            name, doc = pkg.findtext("name"), (comment(pkg) or "").strip()
            if doc.startswith(REGISTER_URI):
                bb_path = doc[len(REGISTER_URI):]
                key, prefix, kind = bb_path, b2x.bb_id(bb_path) + ".", "block"
            elif name == "cdifSharedTypes":
                key, prefix, kind, bb_path = SHARED_TYPES_FILE, SHARED_PREFIX, "shared", None
            elif name == "cdifSharedUnions":
                key, prefix, kind, bb_path = SHARED_UNIONS_FILE, UNION_NS + ".", "union", None
            elif name in SUPPORT_PACKAGES:
                key, prefix, kind, bb_path = COMMON_TYPES_FILE, SUPPORT_PACKAGES[name] + ".", "common", None
            else:
                continue  # a folder package
            entry = self.files.setdefault(key, {"kind": kind, "bb_path": bb_path, "prefix": prefix, "elements": []})
            for e in pkg.findall("packagedElement"):
                if e.get(XMI + "type") in CLASSIFIERS:
                    entry["elements"].append(e)
                    self.new_id[e.get(XMI + "id")] = (key, prefix + e.findtext("name"))

    def path(self, key):
        entry = self.files[key]
        if entry["kind"] == "block":
            return self.out_root / entry["bb_path"] / f"{Path(entry['bb_path']).name}.xmi"
        return self.out_root / key

    def ref(self, key, canonical_id):
        """(kind, ref) for a reference from file key to a canonical classifier."""
        target_key, linked = self.new_id[canonical_id]
        if target_key == key:
            return "idref", linked
        rel = os.path.relpath(self.path(target_key), self.path(key).parent).replace(os.sep, "/")
        return "href", f"{rel}#{linked}"

    def element(self, key, e):
        """bblock_to_xmi's element dict for canonical classifier e of file key."""
        own_id = self.new_id[e.get(XMI + "id")][1]
        elem = {"kind": e.get(XMI + "type"), "name": e.findtext("name"), "body": comment(e) or "", "attrs": []}
        if e.findtext("isAbstract") == "true":
            elem["abstract"] = True
        generals = [self.ref(key, g.find("general").get(XMI + "idref")) for g in e.findall("generalization")]
        if generals:
            elem["general"] = [r if kind == "href" else f"{self.path(key).name}#{r}" for kind, r in generals]
        for a in e.findall("ownedAttribute"):
            t = a.find("type")
            if t is None:
                type_ref = (None, None)
            elif t.get("href"):
                type_ref = ("prim", t.get("href").rsplit("#", 1)[-1])
            else:
                type_ref = self.ref(key, t.get(XMI + "idref"))
            body = comment(a)
            if body is None and a.get(XMI + "id") in self.association_of:
                # EA exports an association end's documentation as the association's comment
                body = comment(self.association_of[a.get(XMI + "id")])
            attr = {"name": a.findtext("name"), "type": type_ref,
                    "lower": a.findtext("lowerValue/value") or 0, "upper": a.findtext("upperValue/value") or 1,
                    "body": body}
            if a.find("association") is not None and t is not None and not t.get("href"):
                target = self.by_id[t.get(XMI + "idref")].findtext("name")
                attr["assoc"] = f"{own_id.rsplit('.', 1)[0]}.assoc.{elem['name']}_{attr['name']}_{target}"
            elem["attrs"].append(attr)
        literals = [l.find("name").text or "" for l in e.findall("ownedLiteral")]
        if literals:
            elem["literals"] = literals
        return own_id, elem

    def write(self):
        """Write every file; return the written paths."""
        written = []
        for key, entry in sorted(self.files.items()):
            path = self.path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            if entry["kind"] == "common":
                text = b2x.common_types_xmi()  # static: the XSD datatypes and the reference types
            else:
                elements = dict(self.element(key, e) for e in entry["elements"])
                text = self.block_xmi(entry, elements) if entry["kind"] == "block" else \
                    self.shared_xmi(entry, elements)
            path.write_text(text, encoding="utf-8")
            written.append(path)
        return written

    def block_xmi(self, entry, elements):
        """As bblock_to_xmi.build_linked writes it."""
        bb_path = entry["bb_path"]
        bdir = self.sources / bb_path
        link = b2x.Link(bb_path, self.out_root)
        meta = json.loads((bdir / "bblock.json").read_text(encoding="utf-8")) if (bdir / "bblock.json").exists() \
            else {"name": bdir.name}
        schema = yaml.safe_load((bdir / "schema.yaml").read_text(encoding="utf-8")) if (bdir / "schema.yaml").exists() \
            else {}
        root_id = f"{link.bb_id}.{b2x.root_name(bdir, schema)}"
        out = b2x.Writer(link.uid)
        out.lines += b2x.XMI_HEADER
        model_id = f"{link.bb_id}.model"
        out.add(1, f'<uml:Model xmi:id="{model_id}" xmi:uuid="{link.uid(model_id)}">')
        out.add(2, f"<name>{bdir.name}</name>")
        out.comment(2, model_id, f"Building block {link.bb_id} ('{meta['name']}'), "
                                 "generated by bblock_to_xmi.py --linked.")
        out.add(2, f'<packagedElement xmi:type="uml:Package" xmi:id="{link.bb_id}" xmi:uuid="{link.uid(link.bb_id)}">')
        out.add(3, f"<name>{bdir.name}</name>")
        out.add(3, f"<URI>{REGISTER_URI}{bb_path}</URI>")
        order = ([root_id] if root_id in elements else []) + sorted(e for e in elements if e != root_id)
        for eid in order:
            out.element(3, eid, elements[eid])
        for eid in order:
            out.associations(3, eid, elements[eid])
        out.add(2, "</packagedElement>")
        out.add(1, "</uml:Model>")
        out.add(0, "</xmi:XMI>")
        return "\n".join(out.lines) + "\n"

    def shared_xmi(self, entry, elements):
        """As bblock_to_xmi.shared_types_xmi / shared_unions write them."""
        if entry["kind"] == "shared":
            return b2x.shared_types_xmi(elements)
        out = b2x.Writer(b2x.common_uid)
        out.lines += b2x.XMI_HEADER
        out.add(1, f'<uml:Model xmi:id="{UNION_NS}.model" xmi:uuid="{b2x.common_uid(UNION_NS + ".model")}">')
        out.add(2, "<name>cdifSharedUnions</name>")
        out.add(2, f'<packagedElement xmi:type="uml:Package" xmi:id="{UNION_NS}" xmi:uuid="{b2x.common_uid(UNION_NS)}">')
        out.add(3, "<name>cdifSharedUnions</name>")
        out.add(3, "<URI>https://w3id.org/cdif/union/xmi/</URI>")
        for eid in sorted(elements):
            out.element(3, eid, elements[eid])
        for eid in sorted(elements):
            out.associations(3, eid, elements[eid])
        out.add(2, "</packagedElement>")
        out.add(1, "</uml:Model>")
        out.add(0, "</xmi:XMI>")
        return "\n".join(out.lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("canonical", type=Path, help="Canonical XMI from to_canonical.py (either variant)")
    ap.add_argument("-o", "--output", type=Path, required=True, help="root of the linked tree to write")
    ap.add_argument("--sources-dir", type=Path, default=DEFAULT_SOURCES_DIR,
                    help="metadataBuildingBlocks _sources (block names and root names; default: %(default)s)")
    args = ap.parse_args()
    c = Canonical(args.canonical, args.output, args.sources_dir)
    if not any(f["kind"] == "block" for f in c.files.values()):
        sys.exit("no building-block packages (a package comment with a register URI) in the model")
    for path in c.write():
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
