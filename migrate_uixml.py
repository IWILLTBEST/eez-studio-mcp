#!/usr/bin/env python3
"""One-shot migration: *.ir.json -> *.uixml with a structural round-trip
assertion (json -> xml -> parsed == original). Usage:

    python migrate_uixml.py a.ir.json [b.ir.json ...] [--out-dir DIR]
"""
import json
import os
import sys

import uixml

def migrate(src: str, out_dir: str | None) -> str:
    with open(src, encoding="utf-8") as f:
        ir = json.load(f)
    # canonicalize empty containers (some generators emit "widgets": [] /
    # "actions": [] — the XML form omits them; the compiler treats both alike)
    for k in ("widgets", "actions", "screens", "variables", "strings"):
        if k in ir and not ir[k]:
            del ir[k]
    base = os.path.basename(src).replace(".ir.json", ".uixml")
    dst = os.path.join(out_dir, base) if out_dir else src.replace(".ir.json", ".uixml")
    uixml.ir_to_xml(ir, dst)
    back = uixml.xml_to_ir(dst)
    diffs = uixml.roundtrip_equal(ir, back)
    if diffs:
        print(f"✗ {src}: round-trip mismatch ({len(diffs)}):")
        for d in diffs[:10]:
            print("   ", d)
        raise SystemExit(1)
    print(f"✓ {src} -> {dst} (round-trip lossless)")
    return dst

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out_dir = None
    if "--out-dir" in sys.argv:
        out_dir = sys.argv[sys.argv.index("--out-dir") + 1]
    for src in args:
        migrate(src, out_dir)
