#!/usr/bin/env python3
"""eezml 导出器（一期）：EEZ Studio 工程 -> uixml 格式 XML 文件集。

链路：.eez-project --eez2ir--> IR --uixml.ir_to_xml--> .uixml
配套产物：assets.uixml（位图/字体按名引用清单，供固件侧注册表对齐）。

用法：
    python eezml_export.py <project.eez-project> [-o outdir]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import eez2ir
import uixml


def export(project_path: str, outdir: str | None = None) -> list[str]:
    project_path = os.path.abspath(project_path)
    if not project_path.endswith(".eez-project"):
        raise SystemExit(f"not an .eez-project: {project_path}")
    with open(project_path, encoding="utf-8") as f:
        project = json.load(f)

    stem = os.path.splitext(os.path.basename(project_path))[0]
    outdir = os.path.abspath(outdir or os.path.join(
        os.path.dirname(project_path), "eezml"))
    os.makedirs(outdir, exist_ok=True)

    # 1) 工程 -> IR（eez2ir 负责 widget 树/样式/vars/actions 的语义化）
    ir = eez2ir.eez_to_ir(project)

    # 2) IR -> 单文件全量 uixml（一期形态；分离文件集后续按需）
    main_xml = os.path.join(outdir, f"{stem}.uixml")
    uixml.ir_to_xml(ir, main_xml)

    # 3) assets 引用清单：工程位图/字体按名登记，固件注册表以此对齐
    assets = {"bitmaps": [], "fonts": []}
    for b in project.get("bitmaps") or []:
        name = b.get("name")
        if name:
            assets["bitmaps"].append({"name": name})
    for fobj in project.get("fonts") or []:
        name = fobj.get("name")
        if name:
            assets["fonts"].append({"name": name})
    assets_xml = os.path.join(outdir, "assets.uixml")
    with open(assets_xml, "w", encoding="utf-8", newline="\n") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write("<assets>\n")
        for b in assets["bitmaps"]:
            f.write(f'  <bitmap name="{b["name"]}" />\n')
        for fo in assets["fonts"]:
            f.write(f'  <font name="{fo["name"]}" />\n')
        f.write("</assets>\n")

    return [main_xml, assets_xml]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="EEZ project -> eezml (uixml) export")
    ap.add_argument("project", help=".eez-project file path")
    ap.add_argument("-o", "--outdir", help="output directory (default: <project dir>/eezml)")
    args = ap.parse_args(argv)

    files = export(args.project, args.outdir)
    for fpath in files:
        size = os.path.getsize(fpath)
        print(f"written: {fpath} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
