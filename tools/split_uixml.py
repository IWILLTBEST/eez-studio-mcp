#!/usr/bin/env python3
"""Split a single-file .uixml into the Qt-style form.

Usage: python tools/split_uixml.py <src.uixml>

Writes next to the source:
  <name>.uixml          manifest stitching the planes with <include>
  logic.uixml           project header + variables + actions
  strings.uixml         only when the project has tr keys
  screens/<scr>.uixml   one file per screen (user widgets included)

The manifest is named after the example directory (or the source stem when it
is not "project") so every example's entry file is uniquely named — editor
tabs stay distinguishable across examples.
把单文件工程拆成分离形态；清单按示例目录命名，各示例入口文件名唯一、
编辑器页签可区分。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
import uixml  # noqa: E402


def split_ir(ir: dict, d: str, name: str) -> bool:
    """Write the Qt-style split of `ir` into directory `d` with a manifest
    named <name>.uixml. Self-checks (manifest must re-parse to the identical
    IR) and refuses to touch an existing source on mismatch. 返回是否成功。"""
    includes = []

    def write(ir_part: dict, rel: str) -> None:
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        uixml.ir_to_xml(ir_part, p)
        print("written:", p)

    write({"project": ir.get("project") or {}, "variables": ir.get("variables") or [],
           "actions": ir.get("actions") or []}, "logic.uixml")
    includes.append("logic.uixml")

    if (ir.get("strings") or {}).get("texts"):
        write({"strings": ir["strings"]}, "strings.uixml")
        includes.append("strings.uixml")

    # User widgets are reusable components — their own plane, one file each,
    # included before the screens that use them. 用户部件独立平面、逐件成文件。
    for wname in sorted((ir.get("widgets") or {})):
        rel = os.path.join("widgets", f"{wname}.uixml")
        write({"widgets": {wname: ir["widgets"][wname]}}, rel)
        includes.append(rel.replace("\\", "/"))

    for scr in ir.get("screens") or []:
        rel = os.path.join("screens", f"{scr['name']}.uixml")
        write({"screens": [scr]}, rel)
        includes.append(rel.replace("\\", "/"))

    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             f"<!-- {name}, split form: logic{'/strings' if 'strings.uixml' in includes else ''}"
             f"{'/widgets' if any(i.startswith('widgets/') for i in includes) else ''}"
             "/screens stitched here. -->",
             "<ui>"]
    lines += [f'  <include src="{inc}"/>' for inc in includes]
    lines += ["</ui>", ""]
    # The manifest REPLACES the single-file source (same name) — write to a
    # temp name, verify the round-trip, then swap atomically so a failed
    # self-check never destroys the source. 清单与源同名：先写临时名、验证
    # 往返一致后再原子替换，自检失败绝不毁源文件。
    manifest = os.path.join(d, f"{name}.uixml")
    tmp = manifest + ".splitting"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    if uixml.xml_to_ir(tmp) != ir:
        os.remove(tmp)
        print("✗ manifest round-trip mismatch — source left untouched", file=sys.stderr)
        return False
    os.replace(tmp, manifest)
    print("written:", manifest)
    print(f"✓ split OK, manifest re-parses to the identical IR ({len(includes)} includes)")
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    src = os.path.abspath(sys.argv[1])
    ir = uixml.xml_to_ir(src)
    d = os.path.dirname(src)
    name = os.path.splitext(os.path.basename(src))[0]
    if name == "project":
        name = os.path.basename(d)
    return 0 if split_ir(ir, d, name) else 1


if __name__ == "__main__":
    sys.exit(main())
