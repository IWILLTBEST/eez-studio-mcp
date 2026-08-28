"""
html2eez — font tool

Subcommands:
    compile    compile an LVGL font (.bin + .c + meta.json) from a source .ttf/.otf
    extract    extract a font from an existing .eez-project (.otf + .bin + .c + meta.json)
    scan-html  scan an HTML file for all displayable characters (for --symbols-from-html debugging)
    list       list fonts registered in fonts/catalog.json
    show       print a font's meta.json (KB-sized, safe)

Design principles:
    1. All binaries (.otf/.bin/.c) go file-to-file; never printed to stdout
    2. Agents only read catalog.json and meta.json (both < 5KB)
    3. .eez-project generation leaves embeddedFontFile empty (embedFonts=false)
       so EEZ Studio reloads the .otf from source.filePath when opening

html2eez — 字体工具。
子命令：compile / extract / scan-html / list / show。
设计原则：二进制走文件到文件不进 stdout；agent 只读 catalog/meta（<5KB）；
生成时 embeddedFontFile 留空，EEZ 打开时按 source.filePath 重新加载 .otf。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.resolve()
FONTS_DIR = ROOT / "fonts"
CATALOG_PATH = FONTS_DIR / "catalog.json"
LV_FONT_CONV = ROOT / "node_modules" / "lv_font_conv" / "lv_font_conv.js"


# ---------- catalog ----------

def load_catalog() -> dict[str, Any]:
    if not CATALOG_PATH.exists():
        return {"fonts": []}
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_catalog(catalog: dict[str, Any]) -> None:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)


def upsert_catalog_entry(meta_name: str, meta_file: str) -> None:
    catalog = load_catalog()
    fonts = catalog.get("fonts", [])
    for entry in fonts:
        if entry.get("name") == meta_name:
            entry["meta"] = meta_file
            break
    else:
        fonts.append({"name": meta_name, "meta": meta_file})
    catalog["fonts"] = fonts
    save_catalog(catalog)


# ---------- compile ----------

def cmd_compile(args: argparse.Namespace) -> int:
    src = Path(args.src).resolve()
    if not src.exists():
        print(f"✗ Source font not found: {src}", file=sys.stderr)
        return 1
    if not LV_FONT_CONV.exists():
        print(f"✗ lv_font_conv not installed: {LV_FONT_CONV}", file=sys.stderr)
        return 1

    name = args.name
    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    # Merge symbols: explicit --symbols + HTML scan (if provided). 合并 symbols：显式 --symbols + HTML 扫描。
    symbols = args.symbols or ""
    if args.symbols_from_html:
        html_path = Path(args.symbols_from_html).resolve()
        if not html_path.exists():
            print(f"✗ HTML not found: {html_path}", file=sys.stderr)
            return 1
        html_chars = collect_html_chars(html_path)
        # Merge and dedup (order preserved). 合并去重（保持顺序）。
        merged: list[str] = []
        seen: set[str] = set()
        for ch in symbols + html_chars:
            if ch not in seen:
                seen.add(ch)
                merged.append(ch)
        symbols = "".join(merged)
        print(f"→ Merged HTML characters: {len(html_chars)} → {len(symbols)} total", file=sys.stderr)

    # 1. Copy the source file to fonts/<name>.<ext> (extension preserved). 复制源文件到 fonts/<name>.<ext>。
    ext = src.suffix.lower()  # .ttf / .otf / .woff
    src_dst = FONTS_DIR / f"{name}{ext}"
    shutil.copyfile(src, src_dst)

    # 2. Three lv_font_conv invocations (file-to-file; nothing on stdout). 三次 lv_font_conv 调用（文件到文件）。
    bin_path = FONTS_DIR / f"{name}.bin"
    c_path = FONTS_DIR / f"{name}.c"

    common = [
        "--font", str(src_dst),
        "--size", str(args.size),
        "--bpp", str(args.bpp),
        "--no-compress",
        "--no-prefilter",
    ]
    if args.range:
        common += ["--range", args.range]
    if symbols:
        common += ["--symbols", symbols]

    # Icon font merging (multiple allowed): lv_font_conv supports multiple --font blocks, each followed by its own --range/--symbols. 图标字体合并：多个 --font 块各带 --range/--symbols。
    icon_sources = []
    for icon_arg in (args.icon_font or []):
        # Format: <path>:<ranges>, e.g. fa-solid.ttf:0xF077-0xF078,0xF00C. 格式：<path>:<ranges>。
        if ":" not in icon_arg:
            print(f"⚠ --icon-font ignored (missing :ranges): {icon_arg}", file=sys.stderr)
            continue
        ipath, iranges = icon_arg.rsplit(":", 1)
        ip = Path(ipath).resolve()
        if not ip.exists():
            print(f"⚠ --icon-font ignored (file not found): {ip}", file=sys.stderr)
            continue
        common += ["--font", str(ip), "--range", iranges]
        icon_sources.append({"path": str(ip), "ranges": iranges})

    # 2a. dump (lv_font_conv treats dump's -o as a directory; outputs font_info.json + glyph PNGs). dump 的 -o 是目录。
    print(f"→ [1/3] dump {name} ...", file=sys.stderr)
    dump_dir = FONTS_DIR / f"{name}.dump"
    if dump_dir.exists():
        shutil.rmtree(dump_dir)
    subprocess.run(
        ["node", str(LV_FONT_CONV)] + common + ["--format", "dump", "-o", str(dump_dir)],
        check=True, stdout=subprocess.DEVNULL,
    )
    font_info_path = dump_dir / "font_info.json"
    with open(font_info_path, "r", encoding="utf-8") as f:
        dump_data = json.load(f)
    # Delete the whole dump directory (PNGs not kept). 删除整个 dump 目录。
    shutil.rmtree(dump_dir)
    dump_meta = {
        "ascent": dump_data.get("ascent"),
        "descent": dump_data.get("descent"),
        "glyph_count": len(dump_data.get("glyphs", [])),
    }

    # 2b. bin (writes the file via -o; stdout discarded). bin 用 -o 直接写文件。
    print(f"→ [2/3] bin   {name} → {bin_path.name} ...", file=sys.stderr)
    subprocess.run(
        ["node", str(LV_FONT_CONV)] + common + ["--format", "bin", "-o", str(bin_path)],
        check=True, stdout=subprocess.DEVNULL,
    )

    # 2c. LVGL C source (same as above). lvgl C source（同上）。
    print(f"→ [3/3] lvgl  {name} → {c_path.name} ...", file=sys.stderr)
    subprocess.run(
        ["node", str(LV_FONT_CONV)] + common + ["--format", "lvgl", "-o", str(c_path)],
        check=True, stdout=subprocess.DEVNULL,
    )

    # 3. Write meta.json (EEZ convention: descent negated to positive, height = ascent - raw_descent). 写 meta.json（对齐 EEZ 约定）。
    raw_ascent = dump_meta.get("ascent") or 0
    raw_descent = dump_meta.get("descent") or 0  # usually negative 通常为负
    ascent = raw_ascent
    descent = -raw_descent  # EEZ stores positive EEZ 存正值
    height = raw_ascent - raw_descent
    threshold = 128 if args.bpp == 1 else 0

    meta = {
        "name": name,
        "renderingEngine": "LVGL",
        "source": {
            "filePath": f"{name}{ext}",
            "size": args.size,
            "threshold": threshold,
        },
        "bpp": args.bpp,
        "threshold": threshold,
        "height": height,
        "ascent": ascent,
        "descent": descent,
        "lvglRanges": args.range or "",
        "lvglSymbols": symbols,
        "glyphCount": dump_meta.get("glyph_count", 0),
        "files": {
            "src": f"{name}{ext}",
            "bin": f"{name}.bin",
            "c": f"{name}.c",
        },
        "iconSources": icon_sources,
        "sizes_bytes": {
            "src": src_dst.stat().st_size,
            "bin": bin_path.stat().st_size,
            "c": c_path.stat().st_size,
        },
    }
    meta_path = FONTS_DIR / f"{name}.meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 4. Update the catalog. 更新 catalog。
    upsert_catalog_entry(name, meta_path.name)

    # 5. Report (KB-sized numbers only). 报告（仅 KB 级数字）。
    print(f"✓ Font {name} compiled")
    print(f"  source:     {meta['sizes_bytes']['src']:>12,} bytes  ({meta['files']['src']})")
    print(f"  bin output: {meta['sizes_bytes']['bin']:>12,} bytes  ({meta['files']['bin']})")
    print(f"  c output:   {meta['sizes_bytes']['c']:>12,} bytes  ({meta['files']['c']})")
    print(f"  glyphs:     {meta['glyphCount']}")
    print(f"  metrics:    ascent={ascent} descent={descent} height={height}")
    print(f"  meta:       {meta_path.relative_to(ROOT)}")
    return 0


# ---------- extract (recover fonts from an existing .eez-project) extract（反解字体）----------

def cmd_extract(args: argparse.Namespace) -> int:
    """Read source.eez-project and pull out the given font's source .otf and compiled
    artifacts. The .eez-project can be large (>10MB), but json.load + field access +
    binary file writes all stay inside Python; nothing ever reaches stdout.

    读取 .eez-project，拆出指定字体的源文件和编译产物（绝不进 stdout）。
    """
    src_project = Path(args.src_project).resolve()
    if not src_project.exists():
        print(f"✗ Source project not found: {src_project}", file=sys.stderr)
        return 1

    name = args.name
    print(f"→ Reading {src_project.name} ...", file=sys.stderr)
    with open(src_project, "r", encoding="utf-8") as f:
        project = json.load(f)

    # Find the matching font (by name). 找匹配字体（按 name）。
    target = None
    for fnt in project.get("fonts", []):
        if fnt.get("name") == name:
            target = fnt
            break
    if target is None:
        avail = [f.get("name") for f in project.get("fonts", [])]
        print(f"✗ Font '{name}' not found in project, available: {avail}", file=sys.stderr)
        return 1

    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Source .otf/.ttf (decode embeddedFontFile base64). 源 .otf/.ttf（base64 解码）。
    embedded = target.get("embeddedFontFile", "")
    if not embedded:
        print(f"⚠ Font {name} has an empty embeddedFontFile; cannot recover the source file", file=sys.stderr)
        return 1
    src_path_obj = Path(target.get("source", {}).get("filePath", f"{name}.otf"))
    src_ext = src_path_obj.suffix.lower() or ".otf"
    src_dst = FONTS_DIR / f"{name}{src_ext}"
    print(f"→ Decoding source → {src_dst.name} ({len(embedded):,} base64 chars)...", file=sys.stderr)
    with open(src_dst, "wb") as f:
        f.write(base64.b64decode(embedded))

    # 2. .bin / .c (if present). .bin / .c（如果存在）。
    bin_dst = FONTS_DIR / f"{name}.bin"
    if target.get("lvglBinFile"):
        with open(bin_dst, "wb") as f:
            f.write(base64.b64decode(target["lvglBinFile"]))
        bin_size = bin_dst.stat().st_size
    else:
        bin_size = 0

    c_dst = FONTS_DIR / f"{name}.c"
    if target.get("lvglSourceFile"):
        with open(c_dst, "wb") as f:
            f.write(base64.b64decode(target["lvglSourceFile"]))
        c_size = c_dst.stat().st_size
    else:
        c_size = 0

    # 3. meta.json (schema kept consistent with compile). meta.json（与 compile 一致）。
    source_meta = target.get("source", {})
    meta = {
        "name": name,
        "renderingEngine": target.get("renderingEngine", "LVGL"),
        "source": {
            "filePath": f"{name}{src_ext}",
            "size": source_meta.get("size", 0),
            "threshold": source_meta.get("threshold", 0),
        },
        "bpp": target.get("bpp", 8),
        "threshold": target.get("threshold", 0),
        "height": target.get("height", 0),
        "ascent": target.get("ascent", 0),
        "descent": target.get("descent", 0),
        "lvglRanges": target.get("lvglRanges", ""),
        "lvglSymbols": target.get("lvglSymbols", ""),
        "glyphCount": len(target.get("glyphs", []) or []),
        "files": {
            "src": f"{name}{src_ext}",
            "bin": f"{name}.bin" if bin_size else "",
            "c": f"{name}.c" if c_size else "",
        },
        "sizes_bytes": {
            "src": src_dst.stat().st_size,
            "bin": bin_size,
            "c": c_size,
        },
    }
    meta_path = FONTS_DIR / f"{name}.meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    upsert_catalog_entry(name, meta_path.name)

    print(f"✓ Font {name} extracted")
    print(f"  source:     {meta['sizes_bytes']['src']:>12,} bytes  ({meta['files']['src']})")
    if bin_size:
        print(f"  bin output: {bin_size:>12,} bytes")
    if c_size:
        print(f"  c output:   {c_size:>12,} bytes")
    print(f"  bpp/size:   {meta['bpp']}bpp @ {meta['source']['size']}px")
    print(f"  ranges:     {meta['lvglRanges'] or '(none)'}")
    print(f"  symbols:    {len(meta['lvglSymbols'])} chars")
    print(f"  meta:       {meta_path.relative_to(ROOT)}")
    return 0


# ---------- HTML character scan HTML 字符扫描 ----------

# Tags whose text is not rendered. Paired tags whose inner text never displays
# → counted via skip nesting. 不渲染文本的标签：内部文本不显示 → 进 skip 嵌套计数。
_SKIP_TAGS = {"script", "style", "head", "title"}
# Void elements without closing tags: must not count toward skip_depth
# (depth would never return to 0 and the whole file would be skipped).
# 无闭合标签的 void 元素：不能计入 skip_depth。
_VOID_SKIP = {"meta", "link", "base"}
# Attributes whose values show up on screen. 可显示的属性（值会在屏幕上出现）。
_DISPLAY_ATTRS = ("placeholder", "value", "data-preview", "data-text", "alt", "title")


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.chars: list[str] = []  # order preserved; deduped later 保留顺序，后续 dedup

    def handle_starttag(self, tag: str, attrs):
        t = tag.lower()
        if t in _SKIP_TAGS:
            self.skip_depth += 1

    def handle_endtag(self, tag: str):
        if tag.lower() in _SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_startendtag(self, tag: str, attrs):
        # Self-closing tags like <input .../>: extract displayable attributes. 自闭合标签提取可显示属性。
        if self.skip_depth == 0:
            for k, v in attrs:
                if k.lower() in _DISPLAY_ATTRS and v:
                    self.chars.append(v)

    def handle_data(self, data: str):
        if self.skip_depth == 0:
            self.chars.append(data)

    # Comments are ignored automatically (HTMLParser doesn't pass them to handle_data). 注释自动忽略。


def collect_html_chars(html_path: Path) -> str:
    """Scan the HTML and extract every character that can appear on screen;
    returns a deduped string.
    Includes: element text + attribute values like placeholder/value/alt/data-preview.
    Excludes: <script>/<style>/<head> contents + HTML comments.

    扫描 HTML，提取所有会出现在屏幕上的字符（去重）：
    含元素文本与 placeholder/value 等属性值；排除 script/style/head 与注释。
    """
    with open(html_path, "r", encoding="utf-8") as f:
        text = f.read()
    p = _TextCollector()
    p.feed(text)
    p.close()
    # Concatenate + dedup per character (first-occurrence order kept). 拼接 + 按字符去重（保持首现顺序）。
    seen: dict[str, None] = {}
    for chunk in p.chars:
        for ch in chunk:
            if ch not in seen and not ch.isspace():
                seen[ch] = None
            elif ch.isspace() and ch not in seen:
                # Spaces aren't strictly required (lv_font_conv handles 0x20) but kept for dedup. 空格不强制包含，保留以便 dedup。
                seen[ch] = None
    return "".join(seen.keys())


def cmd_scan_html(args: argparse.Namespace) -> int:
    chars = collect_html_chars(Path(args.html).resolve())
    print(f"Scanned {len(chars)} unique characters:")
    print(f"  {chars}")
    return 0


# ---------- list / show ----------

def cmd_list(_args: argparse.Namespace) -> int:
    catalog = load_catalog()
    fonts = catalog.get("fonts", [])
    if not fonts:
        print("(catalog is empty)")
        return 0
    print(f"Registered {len(fonts)} fonts:")
    for entry in fonts:
        meta_path = FONTS_DIR / entry["meta"]
        if not meta_path.exists():
            print(f"  ✗ {entry['name']:<24} (meta missing: {entry['meta']})")
            continue
        with open(meta_path, "r", encoding="utf-8") as f:
            m = json.load(f)
        sizes = m.get("sizes_bytes", {})
        print(f"  · {m['name']:<24} {m['bpp']}bpp size={m['source']['size']:<3} "
              f"glyphs={m.get('glyphCount', '?'):<5} "
              f"bin={sizes.get('bin', 0):>10,}B")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    meta_path = FONTS_DIR / f"{args.name}.meta.json"
    if not meta_path.exists():
        print(f"✗ Not found: {meta_path}", file=sys.stderr)
        return 1
    with open(meta_path, "r", encoding="utf-8") as f:
        print(f.read())
    return 0


# ---------- main ----------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="html2eez font tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_compile = sub.add_parser("compile", help="compile an LVGL font from .ttf/.otf")
    p_compile.add_argument("--src", required=True, help="path to the source .ttf/.otf")
    p_compile.add_argument("--name", required=True, help="font name (e.g. myfont_32)")
    p_compile.add_argument("--size", type=int, required=True, help="pixel size")
    p_compile.add_argument("--bpp", type=int, default=8, choices=[1, 2, 3, 4, 8])
    p_compile.add_argument("--range", default="32-127", help="encoding range, e.g. 32-127")
    p_compile.add_argument("--symbols", default="", help="extra symbol characters")
    p_compile.add_argument("--symbols-from-html", default="",
                           help="HTML file path; scan all its displayable characters into symbols")
    p_compile.add_argument("--icon-font", action="append", default=[],
                           help="icon font to merge, format '<path>:<ranges>', "
                                "e.g. 'fontawesome/fa-solid-900.ttf:0xF077-0xF078,0xF00C'. May be given multiple times")
    p_compile.set_defaults(func=cmd_compile)

    p_extract = sub.add_parser("extract", help="extract fonts from an existing .eez-project")
    p_extract.add_argument("--src-project", required=True, help="path to the source .eez-project")
    p_extract.add_argument("--name", required=True, help="name of the font to extract")
    p_extract.set_defaults(func=cmd_extract)

    p_scan = sub.add_parser("scan-html", help="scan HTML for displayable characters")
    p_scan.add_argument("--html", required=True, help="path to the HTML file")
    p_scan.set_defaults(func=cmd_scan_html)

    p_list = sub.add_parser("list", help="list registered fonts")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="print a font's meta.json")
    p_show.add_argument("--name", required=True)
    p_show.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
