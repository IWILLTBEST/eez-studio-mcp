"""
html2eez — 字体工具

子命令：
    compile    从源 .ttf/.otf 编译 LVGL 字体（.bin + .c + meta.json）
    extract    从已有 .eez-project 反解字体（.otf + .bin + .c + meta.json）
    scan-html  扫描 HTML 提取所有可显示字符（用于 --symbols-from-html 调试）
    list       列出 fonts/catalog.json 中已注册字体
    show       打印某字体的 meta.json（KB 级，安全）

设计原则：
    1. 所有二进制（.otf/.bin/.c）走文件到文件，绝不打印到 stdout
    2. agent 只读 catalog.json 和 meta.json（都 < 5KB）
    3. .eez-project 生成时 embeddedFontFile 留空（embedFonts=false）
       让 EEZ Studio 在打开时按 source.filePath 重新加载 .otf
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
        print(f"✗ 源字体不存在: {src}", file=sys.stderr)
        return 1
    if not LV_FONT_CONV.exists():
        print(f"✗ lv_font_conv 未安装: {LV_FONT_CONV}", file=sys.stderr)
        return 1

    name = args.name
    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    # 合并 symbols：显式 --symbols + 从 HTML 扫描（若提供）
    symbols = args.symbols or ""
    if args.symbols_from_html:
        html_path = Path(args.symbols_from_html).resolve()
        if not html_path.exists():
            print(f"✗ HTML 不存在: {html_path}", file=sys.stderr)
            return 1
        html_chars = collect_html_chars(html_path)
        # 合并去重（保持顺序）
        merged: list[str] = []
        seen: set[str] = set()
        for ch in symbols + html_chars:
            if ch not in seen:
                seen.add(ch)
                merged.append(ch)
        symbols = "".join(merged)
        print(f"→ 合并 HTML 字符: {len(html_chars)} 个 → 共 {len(symbols)} 个", file=sys.stderr)

    # 1. 复制源文件到 fonts/<name>.<ext>（保留扩展名）
    ext = src.suffix.lower()  # .ttf / .otf / .woff
    src_dst = FONTS_DIR / f"{name}{ext}"
    shutil.copyfile(src, src_dst)

    # 2. 三次 lv_font_conv 调用（文件到文件，无 stdout 进入 context）
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

    # 图标字体合并（可多个）：lv_font_conv 支持多个 --font 块，每块后跟自己的 --range/--symbols
    icon_sources = []
    for icon_arg in (args.icon_font or []):
        # 格式：<path>:<ranges>  例如 fa-solid.ttf:0xF077-0xF078,0xF00C
        if ":" not in icon_arg:
            print(f"⚠ --icon-font 忽略（缺 :ranges）: {icon_arg}", file=sys.stderr)
            continue
        ipath, iranges = icon_arg.rsplit(":", 1)
        ip = Path(ipath).resolve()
        if not ip.exists():
            print(f"⚠ --icon-font 忽略（找不到文件）: {ip}", file=sys.stderr)
            continue
        common += ["--font", str(ip), "--range", iranges]
        icon_sources.append({"path": str(ip), "ranges": iranges})

    # 2a. dump（lv_font_conv 把 dump 的 -o 当目录，输出 font_info.json + 字形 PNG）
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
    # 删除整个 dump 目录（PNG 不需要保留）
    shutil.rmtree(dump_dir)
    dump_meta = {
        "ascent": dump_data.get("ascent"),
        "descent": dump_data.get("descent"),
        "glyph_count": len(dump_data.get("glyphs", [])),
    }

    # 2b. bin（用 -o 直接写文件，stdout 丢弃）
    print(f"→ [2/3] bin   {name} → {bin_path.name} ...", file=sys.stderr)
    subprocess.run(
        ["node", str(LV_FONT_CONV)] + common + ["--format", "bin", "-o", str(bin_path)],
        check=True, stdout=subprocess.DEVNULL,
    )

    # 2c. lvgl C source（同上）
    print(f"→ [3/3] lvgl  {name} → {c_path.name} ...", file=sys.stderr)
    subprocess.run(
        ["node", str(LV_FONT_CONV)] + common + ["--format", "lvgl", "-o", str(c_path)],
        check=True, stdout=subprocess.DEVNULL,
    )

    # 3. 写 meta.json（对齐 EEZ 约定：descent 取反为正，height = ascent - raw_descent）
    raw_ascent = dump_meta.get("ascent") or 0
    raw_descent = dump_meta.get("descent") or 0  # 通常为负
    ascent = raw_ascent
    descent = -raw_descent  # EEZ 存正值
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

    # 4. 更新 catalog
    upsert_catalog_entry(name, meta_path.name)

    # 5. 报告（仅 KB 级数字）
    print(f"✓ 字体 {name} 编译完成")
    print(f"  源文件:     {meta['sizes_bytes']['src']:>12,} bytes  ({meta['files']['src']})")
    print(f"  bin 产物:   {meta['sizes_bytes']['bin']:>12,} bytes  ({meta['files']['bin']})")
    print(f"  c    产物:  {meta['sizes_bytes']['c']:>12,} bytes  ({meta['files']['c']})")
    print(f"  字形数:     {meta['glyphCount']}")
    print(f"  metrics:    ascent={ascent} descent={descent} height={height}")
    print(f"  meta:       {meta_path.relative_to(ROOT)}")
    return 0


# ---------- extract（从已有 .eez-project 反解字体）----------

def cmd_extract(args: argparse.Namespace) -> int:
    """读取 source.eez-project，把指定字体的源 .otf 和编译产物拆出来。
    source.eez-project 可能很大（>10MB），但 json.load + 字段访问 + 二进制写文件
    全部走 Python 内部，绝不进 stdout。"""
    src_project = Path(args.src_project).resolve()
    if not src_project.exists():
        print(f"✗ 源工程不存在: {src_project}", file=sys.stderr)
        return 1

    name = args.name
    print(f"→ 读取 {src_project.name} ...", file=sys.stderr)
    with open(src_project, "r", encoding="utf-8") as f:
        project = json.load(f)

    # 找匹配字体（按 name）
    target = None
    for fnt in project.get("fonts", []):
        if fnt.get("name") == name:
            target = fnt
            break
    if target is None:
        avail = [f.get("name") for f in project.get("fonts", [])]
        print(f"✗ 工程内未找到字体 '{name}'，可用: {avail}", file=sys.stderr)
        return 1

    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 源 .otf/.ttf（embeddedFontFile base64 解码）
    embedded = target.get("embeddedFontFile", "")
    if not embedded:
        print(f"⚠ 字体 {name} 的 embeddedFontFile 为空，无法恢复源文件", file=sys.stderr)
        return 1
    src_path_obj = Path(target.get("source", {}).get("filePath", f"{name}.otf"))
    src_ext = src_path_obj.suffix.lower() or ".otf"
    src_dst = FONTS_DIR / f"{name}{src_ext}"
    print(f"→ 解码源文件 → {src_dst.name} ({len(embedded):,} base64 chars)...", file=sys.stderr)
    with open(src_dst, "wb") as f:
        f.write(base64.b64decode(embedded))

    # 2. .bin / .c（如果存在）
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

    # 3. meta.json（保持与 compile 一致的 schema）
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

    print(f"✓ 字体 {name} 反解完成")
    print(f"  源文件:     {meta['sizes_bytes']['src']:>12,} bytes  ({meta['files']['src']})")
    if bin_size:
        print(f"  bin 产物:   {bin_size:>12,} bytes")
    if c_size:
        print(f"  c    产物:  {c_size:>12,} bytes")
    print(f"  bpp/size:   {meta['bpp']}bpp @ {meta['source']['size']}px")
    print(f"  ranges:     {meta['lvglRanges'] or '(空)'}")
    print(f"  symbols:    {len(meta['lvglSymbols'])} 字符")
    print(f"  meta:       {meta_path.relative_to(ROOT)}")
    return 0


# ---------- HTML 字符扫描 ----------

# 不渲染文本的标签
# 有闭合标签、内部文本不显示 → 进 skip 嵌套计数
_SKIP_TAGS = {"script", "style", "head", "title"}
# 无闭合标签的 void 元素：不能计入 skip_depth（否则深度永远回不到 0，全文被跳过）
_VOID_SKIP = {"meta", "link", "base"}
# 可显示的属性（值会在屏幕上出现）
_DISPLAY_ATTRS = ("placeholder", "value", "data-preview", "data-text", "alt", "title")


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.chars: list[str] = []  # 保留顺序，后续 dedup

    def handle_starttag(self, tag: str, attrs):
        t = tag.lower()
        if t in _SKIP_TAGS:
            self.skip_depth += 1

    def handle_endtag(self, tag: str):
        if tag.lower() in _SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_startendtag(self, tag: str, attrs):
        # <input .../> 这类自闭合，提取可显示属性
        if self.skip_depth == 0:
            for k, v in attrs:
                if k.lower() in _DISPLAY_ATTRS and v:
                    self.chars.append(v)

    def handle_data(self, data: str):
        if self.skip_depth == 0:
            self.chars.append(data)

    # 注释自动忽略（HTMLParser 不传给 handle_data）


def collect_html_chars(html_path: Path) -> str:
    """扫描 HTML，提取所有会在屏幕上出现的字符，返回去重后的字符串。
    包含：元素文本内容 + placeholder/value/alt/data-preview 等属性值
    排除：<script>/<style>/<head> 内部 + HTML 注释
    """
    with open(html_path, "r", encoding="utf-8") as f:
        text = f.read()
    p = _TextCollector()
    p.feed(text)
    p.close()
    # 拼接 + 按字符去重（保持首次出现顺序）
    seen: dict[str, None] = {}
    for chunk in p.chars:
        for ch in chunk:
            if ch not in seen and not ch.isspace():
                seen[ch] = None
            elif ch.isspace() and ch not in seen:
                # 空格不强制包含（lv_font_conv 会处理空格 0x20），但保留以便 dedup
                seen[ch] = None
    return "".join(seen.keys())


def cmd_scan_html(args: argparse.Namespace) -> int:
    chars = collect_html_chars(Path(args.html).resolve())
    print(f"扫描到 {len(chars)} 个唯一字符:")
    print(f"  {chars}")
    return 0


# ---------- list / show ----------

def cmd_list(_args: argparse.Namespace) -> int:
    catalog = load_catalog()
    fonts = catalog.get("fonts", [])
    if not fonts:
        print("(catalog 为空)")
        return 0
    print(f"已注册 {len(fonts)} 个字体:")
    for entry in fonts:
        meta_path = FONTS_DIR / entry["meta"]
        if not meta_path.exists():
            print(f"  ✗ {entry['name']:<24} (meta 缺失: {entry['meta']})")
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
        print(f"✗ 未找到 {meta_path}", file=sys.stderr)
        return 1
    with open(meta_path, "r", encoding="utf-8") as f:
        print(f.read())
    return 0


# ---------- main ----------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="html2eez 字体工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_compile = sub.add_parser("compile", help="从 .ttf/.otf 编译 LVGL 字体")
    p_compile.add_argument("--src", required=True, help="源 .ttf/.otf 路径")
    p_compile.add_argument("--name", required=True, help="字体名（如 myfont_32）")
    p_compile.add_argument("--size", type=int, required=True, help="像素大小")
    p_compile.add_argument("--bpp", type=int, default=8, choices=[1, 2, 3, 4, 8])
    p_compile.add_argument("--range", default="32-127", help="编码范围，如 32-127")
    p_compile.add_argument("--symbols", default="", help="额外符号字符串")
    p_compile.add_argument("--symbols-from-html", default="",
                           help="HTML 文件路径；扫描其所有可显示字符加入 symbols")
    p_compile.add_argument("--icon-font", action="append", default=[],
                           help="图标字体合并，格式 '<path>:<ranges>'，"
                                "例如 'fontawesome/fa-solid-900.ttf:0xF077-0xF078,0xF00C'。可多次指定")
    p_compile.set_defaults(func=cmd_compile)

    p_extract = sub.add_parser("extract", help="从已有 .eez-project 反解字体")
    p_extract.add_argument("--src-project", required=True, help="源 .eez-project 路径")
    p_extract.add_argument("--name", required=True, help="要抽取的字体名")
    p_extract.set_defaults(func=cmd_extract)

    p_scan = sub.add_parser("scan-html", help="扫描 HTML 提取可显示字符")
    p_scan.add_argument("--html", required=True, help="HTML 文件路径")
    p_scan.set_defaults(func=cmd_scan_html)

    p_list = sub.add_parser("list", help="列出已注册字体")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="打印字体 meta.json")
    p_show.add_argument("--name", required=True)
    p_show.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
