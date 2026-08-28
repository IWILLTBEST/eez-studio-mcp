"""
html2eez — HTML → EEZ Studio .eez-project (LVGL v9) generator MVP

Usage:
    python generator.py example.html -o out.eez-project
    python generator.py example.html                # default output: out.eez-project

Depends only on the Python standard library. The generated .eez-project can be
opened directly in EEZ Studio via "Open Project" and edited there.

Fonts:
    Reads fonts/catalog.json plus each font's meta.json (both < 5KB).
    Binary .otf/.bin/.c files are not embedded into the .eez-project; they are
    referenced by relative path and EEZ Studio loads them via source.filePath.
    To re-edit glyphs inside EEZ, place the corresponding .otf in a fonts/
    folder next to the .eez-project.

html2eez — HTML → EEZ Studio .eez-project (LVGL v9) 生成器 MVP。
只依赖 Python 标准库，产物可在 EEZ Studio 中直接打开编辑。
字体：读取 fonts/catalog.json + meta.json；二进制不进 .eez-project，
按相对路径引用，EEZ 打开时自动加载。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# Windows console defaults to GBK; force stdout/stderr UTF-8 (avoids emoji/CJK crashes). Windows 控制台默认 GBK，强制 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# ---------- Utilities 工具 ----------

def oid() -> str:
    """Generate the objID EEZ expects (a UUID v4 string). 生成 EEZ 期望的 objID（UUID v4 字符串）。"""
    return str(uuid.uuid4())


def parse_events(attr_value: str) -> list[tuple[str, str]]:
    """data-event="CLICKED:a,PRESSED:b" → [("CLICKED","a"),("PRESSED","b")]"""
    out: list[tuple[str, str]] = []
    for piece in attr_value.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            evt, act = piece.split(":", 1)
            out.append((evt.strip().upper(), act.strip()))
        else:
            # Action name only → defaults to CLICKED. 只有 action 名 → 默认 CLICKED。
            out.append(("CLICKED", piece))
    return out


# ---------- DOM ----------

SELF_CLOSING = {"img", "input", "br", "hr", "meta", "link"}


class Node:
    def __init__(self, tag: str, attrs: dict[str, str | None]):
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node] = []
        self.text = ""          # direct text (used by leaves) 直接文本（叶子用）
        self.parent: Node | None = None

    def get(self, name: str, default: str = "") -> str:
        v = self.attrs.get(name)
        return v if v else default

    def has(self, name: str) -> bool:
        v = self.attrs.get(name)
        return v not in (None, "", None)


class DOMBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {})
        self.stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs):
        node = Node(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in SELF_CLOSING:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str):
        # Pop the stack up to the matching tag. 弹到匹配标签。
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str):
        text = data.strip()
        if text:
            # Append to the top-of-stack node's text (for leaf nodes). 追加到栈顶节点的文本（叶子节点用）。
            top = self.stack[-1]
            if top.text:
                top.text += " " + text
            else:
                top.text = text


def parse_html(text: str) -> Node:
    p = DOMBuilder()
    p.feed(text)
    p.close()
    return p.root


# ---------- Collection 收集 ----------

class Collector:
    """First pass: collect all referenced variables, actions and fonts; infer variable types. 第一次遍历：收集引用过的变量、动作、字体；推断变量类型。"""

    def __init__(self) -> None:
        self.vars: dict[str, dict[str, Any]] = {}     # name → declaration
        self.actions: set[str] = set()

    def declare_var(self, name: str, vtype: str = "string", default: str = '""') -> None:
        if name not in self.vars:
            self.vars[name] = {
                "objID": oid(),
                "name": name,
                "type": vtype,
                "defaultValue": default,
                "persistent": False,
                "native": True,
            }

    def declare_action(self, name: str) -> None:
        self.actions.add(name)


def is_leaf_text(node: Node) -> bool:
    """Leaf text node: no element children of its own. 叶子文本节点：自身没有 element 子节点。"""
    return not any(c.tag not in ("#text",) for c in node.children)


# ---------- Layout 布局 ----------

# HTML inline elements: flow horizontally within a row; width estimated from content. HTML inline 元素：行内横排，宽度按内容估算。
INLINE_TAGS = {"button", "a", "img", "span", "input", "label", "select", "checkbox",
               "switch", "arc", "spinner", "led", "dropdown"}

# Estimate character width by font size (CJK ≈ 1em, ASCII ≈ 0.6em). 按字体大小估算字符宽度（中文≈1em，ASCII≈0.6em）。
def estimate_text_width(text: str, font_size: int) -> int:
    w = 0
    for ch in text:
        # CJK / fullwidth characters. CJK / 全角字符。
        if ord(ch) > 0x2E80:
            w += font_size
        else:
            w += int(font_size * 0.6)
    return w


class Layout:
    """HTML-like layout:
    - inline elements (button/a/img/span/input) flow horizontally by content width, wrapping when the row is full
    - block elements (div/p/h1/hr) take the whole row

    HTML-like 布局：inline 元素按内容宽度横排、超出换行；block 元素独占整行。
    """

    def __init__(self, screen_w: int, screen_h: int):
        self.sw = screen_w
        self.sh = screen_h

    def is_inline(self, node: Node) -> bool:
        return node.tag in INLINE_TAGS and not node.has("data-x")

    def estimate_size(self, node: Node, parent_w: int, default_h: int = 40) -> tuple[int, int]:
        """Estimate widget width/height (for inline elements). 估算 widget 宽高（用于 inline 元素）。"""
        w_attr = node.get("data-w", "")
        h_attr = node.get("data-h", "")
        if w_attr:
            w = int(w_attr)
        elif node.tag in ("button", "a"):
            # Button: text width + 24px padding on each side. 按钮：文字宽度 + 左右 padding 各 24。
            text = node.text or node.get("data-text", "Btn")
            font_size = 16  # default estimate 默认估算
            font_name = resolve_attr(node, "data-font")
            if font_name and "_" in font_name:
                try:
                    font_size = int(font_name.rsplit("_", 1)[-1])
                except ValueError:
                    pass
            w = estimate_text_width(text, font_size) + 48
        elif node.tag == "img":
            w = 80  # default icon 默认图标
        elif node.tag == "input":
            t = node.get("type", "text")
            if t == "range":
                w = 200
            elif t == "checkbox":
                w = 32
            else:
                w = 160
        elif node.tag == "select":
            w = 150
        elif node.tag == "switch":
            w = 50
        elif node.tag == "arc":
            w = 150
        elif node.tag == "spinner":
            w = 80
        elif node.tag == "led":
            w = 32
        else:
            w = parent_w
        # Default height depends on widget type. 默认高度按 widget 类型。
        if h_attr:
            h = int(h_attr)
        elif node.tag == "arc":
            h = 150
        elif node.tag == "spinner":
            h = 80
        elif node.tag in ("switch", "led"):
            h = node.tag == "switch" and 25 or 32
        elif node.tag == "input" and node.get("type") == "checkbox":
            h = 32
        else:
            h = default_h
        return w, h

    def place_inline(self, node: Node, parent_w: int, cursor_x: int, cursor_y: int, row_h: int) -> tuple[int, int, int, int, int]:
        """Inline placement: returns (x, y, w, h, new_row_h). inline 放置：返回 (x, y, w, h, new_row_h)。"""
        w, h = self.estimate_size(node, parent_w)
        x = cursor_x
        y = cursor_y
        # Line-wrap check. 换行检测。
        if cursor_x + w > parent_w and cursor_x > 0:
            x = 0
            y = cursor_y + row_h + 4
            new_row_h = h
        else:
            new_row_h = max(row_h, h)
        return x, y, w, h, new_row_h

    def place(self, node: Node, parent_w: int, cursor_y: int) -> tuple[int, int, int, int]:
        x = int(node.get("data-x", "0"))
        y = int(node.get("data-y", str(cursor_y)))
        w_attr = node.get("data-w", "")
        h_attr = node.get("data-h", "")
        w = int(w_attr) if w_attr else parent_w
        h = int(h_attr) if h_attr else 40
        return x, y, w, h


# ---------- Widget construction Widget 构造 ----------

DEFAULT_FLAGS = (
    "CLICK_FOCUSABLE|GESTURE_BUBBLE|PRESS_LOCK|SCROLL_CHAIN_HOR|"
    "SCROLL_CHAIN_VER|SCROLL_ELASTIC|SCROLL_MOMENTUM|"
    "SCROLL_WITH_ARROW|SNAPPABLE"
)


def base_widget(wtype: str, node: Node, x: int, y: int, w: int, h: int) -> dict[str, Any]:
    """Build the common widget fields. 构造 widget 公共字段。"""
    obj: dict[str, Any] = {
        "objID": oid(),
        "type": wtype,
        "left": x,
        "top": y,
        "width": w,
        "height": h,
        "customInputs": [],
        "customOutputs": [],
        "style": {
            "objID": oid(),
            "useStyle": "default",
            "conditionalStyles": [],
            "childStyle": [],
        },
        "timeline": [],
        "eventHandlers": [],
        "leftUnit": "px",
        "topUnit": "px",
        "widthUnit": "px",
        "heightUnit": "px",
        "children": [],
        "widgetFlags": DEFAULT_FLAGS,
        "hiddenFlagType": "literal",
        "hiddenFlag": node.has("data-hidden"),
        "clickableFlagType": "literal",
        "clickableFlag": False,
        "flagScrollbarMode": "",
        "flagScrollDirection": "",
        "scrollSnapX": "",
        "scrollSnapY": "",
        "checkedStateType": "literal",
        "disabledStateType": "literal",
        "states": "",
        "localStyles": {"objID": oid()},
        "group": "",
        "groupIndex": 0,
    }
    if node.has("data-identifier"):
        obj["identifier"] = node.get("data-identifier")
    return obj


def make_event_handlers(node: Node, col: Collector) -> list[dict[str, Any]]:
    handlers: list[dict[str, Any]] = []
    # data-action="X" → CLICKED:X
    if node.has("data-action"):
        for evt, act in parse_events(node.get("data-action")):
            col.declare_action(act)
            handlers.append({
                "objID": oid(),
                "eventName": evt,
                "handlerType": "action",
                "action": act,
                "userData": 0,
            })
    # data-event="EVENT:action,..."
    if node.has("data-event"):
        for evt, act in parse_events(node.get("data-event")):
            col.declare_action(act)
            handlers.append({
                "objID": oid(),
                "eventName": evt,
                "handlerType": "action",
                "action": act,
                "userData": 0,
            })
    return handlers


def resolve_attr(node: Node, name: str) -> str:
    """Walk up from node through parents looking for an attribute value (for data-font inheritance). 从 node 向上查 parent 找属性值（data-font 继承用）。"""
    cur: Node | None = node
    while cur is not None:
        if cur.has(name):
            return cur.get(name)
        cur = cur.parent
    return ""


def local_styles_for(node: Node) -> dict[str, Any]:
    """Build localStyles.definition from data-font / data-color. 构造 localStyles.definition，根据 data-font / data-color。"""
    definition: dict[str, Any] = {}
    main_default: dict[str, Any] = {}
    font = resolve_attr(node, "data-font")
    if font:
        main_default["text_font"] = font
    if node.has("data-color"):
        main_default["text_color"] = node.get("data-color")
    if node.has("data-bg"):
        main_default["bg_color"] = node.get("data-bg")
    if main_default:
        definition = {"MAIN": {"DEFAULT": main_default}}
    if definition:
        return {"objID": oid(), "definition": definition}
    return {"objID": oid()}


# ---------- Main conversion 主转换 ----------

TAG_TO_WIDGET = {
    "div": "LVGLContainerWidget",
    "button": "LVGLButtonWidget",
    "a": "LVGLButtonWidget",
    "p": "LVGLLabelWidget",
    "span": "LVGLLabelWidget",
    "label": "LVGLLabelWidget",
    "h1": "LVGLLabelWidget",
    "h2": "LVGLLabelWidget",
    "h3": "LVGLLabelWidget",
    "img": "LVGLImageWidget",
    "select": "LVGLDropdownWidget",
    "ul": "LVGLListWidget",
    "ol": "LVGLListWidget",
}


def build_label(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    obj = base_widget("LVGLLabelWidget", node, x, y, w, h)
    obj["localStyles"] = local_styles_for(node)
    if node.has("data-var"):
        var = node.get("data-var")
        col.declare_var(var, "string", '""')
        obj["text"] = var
        obj["textType"] = "expression"
        obj["previewValue"] = node.get("data-preview", node.text or var)
    else:
        obj["text"] = node.text or node.get("data-text", "Label")
        obj["textType"] = "literal"
    obj["longMode"] = "WRAP"
    obj["recolor"] = False
    return obj


def build_button(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    obj = base_widget("LVGLButtonWidget", node, x, y, w, h)
    obj["clickableFlag"] = True
    obj["localStyles"] = local_styles_for(node)
    obj["eventHandlers"] = make_event_handlers(node, col)
    # Child label: content-sized + align=CENTER within the parent button (EEZ default button pattern). 子 Label：自适应大小 + 居中于父按钮。
    label_text = node.text or node.get("data-text", "Button")
    label_node = Node("label", {})
    label_node.text = label_text
    label_node.parent = node  # so resolve_attr can find data-font upward 让 resolve_attr 能向上找 data-font
    lbl = build_label(label_node, 0, 0, 80, 32, col)
    # Override to content-sized. 覆盖为自适应尺寸。
    lbl["widthUnit"] = "content"
    lbl["heightUnit"] = "content"
    # localStyles: inherited text_font + align=CENTER (object centered in parent). localStyles：text_font 继承 + align=CENTER。
    lbl_style = lbl["localStyles"]
    if "definition" not in lbl_style:
        lbl_style["definition"] = {}
    lbl_style["definition"].setdefault("MAIN", {}).setdefault("DEFAULT", {})["align"] = "CENTER"
    obj["children"].append(lbl)
    return obj


def build_image(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    obj = base_widget("LVGLImageWidget", node, x, y, w, h)
    obj["image"] = node.get("src", node.get("data-image", ""))
    obj["pivotX"] = 0
    obj["pivotY"] = 0
    return obj


def build_bar_or_slider(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    """input[type=range] → Bar if data-var is present (progress display), else Slider. input[type=range] → 有 data-var 用 Bar（显示进度），否则 Slider。"""
    var = node.get("data-var", "")
    wtype = "LVGLBarWidget" if var else "LVGLSliderWidget"
    obj = base_widget(wtype, node, x, y, w, h)
    obj["clickableFlag"] = True
    obj["min"] = int(node.get("min", "0"))
    obj["minType"] = "literal"
    obj["max"] = int(node.get("max", "100"))
    obj["maxType"] = "literal"
    obj["mode"] = "NORMAL"
    if var:
        col.declare_var(var, "integer", "0")
        obj["value"] = var
        obj["valueType"] = "expression"
        obj["valueStart"] = 0
        obj["valueStartType"] = "literal"
    obj["enableAnimation"] = False
    if wtype == "LVGLSliderWidget":
        obj["knob"] = ""
    return obj


def build_textarea(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    obj = base_widget("LVGLTextareaWidget", node, x, y, w, h)
    obj["clickableFlag"] = True
    var = node.get("data-var", "")
    if var:
        col.declare_var(var, "string", '""')
        obj["text"] = var
        obj["textType"] = "expression"
        obj["previewValue"] = node.get("data-preview", "")
    else:
        obj["text"] = node.get("value", node.get("placeholder", ""))
        obj["textType"] = "literal"
    obj["longMode"] = "WRAP"
    obj["recolor"] = False
    obj["oneLineMode"] = True
    obj["passwordMode"] = node.get("type") == "password"
    obj["acceptedCharacters"] = ""
    obj["maxTextLength"] = 128
    return obj


def build_container(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    layout_mode = node.get("data-layout", "")
    if layout_mode:
        # Flex container: auto-grow the parent to fit children, avoiding clipping. flex 容器：自动按子元素撑大父容器，避免裁剪。
        w, h = _flex_autosize(node, w, h)

    obj = base_widget("LVGLContainerWidget", node, x, y, w, h)
    obj["clickableFlag"] = False
    obj["localStyles"] = local_styles_for(node)
    if layout_mode:
        apply_flex(node, obj)
    return obj


def _estimate_child_size(node: Node, parent_w: int) -> tuple[int, int]:
    """Estimate child size; recursively computes the real size for flex-container children. 估算子元素尺寸，flex 容器子元素递归计算真实尺寸。"""
    layout = Layout(parent_w, 0)
    w, h = layout.estimate_size(node, parent_w)
    # If the child is itself a flex container, recursively compute what it really needs. 如果子元素本身是 flex 容器，递归算真正需要的尺寸。
    child_layout = node.get("data-layout", "")
    if child_layout:
        cw, ch = _flex_autosize(node, w, h)
        w, h = cw, ch
    return w, h


def _flex_autosize(node: Node, w: int, h: int) -> tuple[int, int]:
    """Auto-grow a flex container so parent ≥ children (recursive):
    - row: height = max(child h) + vertical gap
    - col: height = sum(child h) + spacing + vertical gap; width = max(child w) + horizontal gap
    - data-h="auto" or unset → use the computed value
    - data-h=<N> → max(N, computed), with a warning printed

    flex 容器自动撑大，保证父 > 子（递归）：row 高度 = max(子高)+gap；
    col 高度 = sum(子高)+间隔+gap，宽度 = max(子宽)+gap。
    data-h="auto"/未设用计算值；data-h=<N> 取 max(N, 计算值) 并告警。
    """
    layout = Layout(w, h)
    gap = int(node.get("data-gap", "4"))
    flow = node.get("data-layout", "").lower()
    is_row = flow.startswith("row")
    is_col = flow.startswith("col") or flow == "column"

    children = [c for c in node.children
                if c.tag not in ("br", "hr", "#text", "#document")]
    sizes = [_estimate_child_size(c, w) for c in children]
    if not sizes:
        return w, h

    max_child_w = max(s[0] for s in sizes)
    max_child_h = max(s[1] for s in sizes)
    sum_child_h = sum(s[1] for s in sizes) + gap * (len(sizes) - 1)

    pad = gap * 2
    w_attr = node.get("data-w", "")
    h_attr = node.get("data-h", "")

    # Height 高度
    if h_attr.lower() == "auto" or not h_attr:
        h = (max_child_h + pad) if is_row else (sum_child_h + pad)
    else:
        explicit_h = int(h_attr)
        needed_h = (max_child_h + pad) if is_row else (sum_child_h + pad)
        if explicit_h < needed_h:
            print(f"⚠ Container data-h={explicit_h} < {needed_h} needed by children, auto-grown to {needed_h}", file=sys.stderr)
            h = needed_h

    # Width (only col layouts need growing to max child; row keeps parent width by default). 宽度（col 布局才按 max_child 撑大；row 默认沿用父宽）。
    if w_attr.lower() == "auto" or (is_col and not w_attr):
        w = max_child_w + pad
    elif w_attr and w_attr.lower() != "auto":
        explicit_w = int(w_attr)
        if is_col:
            needed_w = max_child_w + pad
            if explicit_w < needed_w:
                print(f"⚠ Container data-w={explicit_w} < {needed_w} needed by children, auto-grown to {needed_w}", file=sys.stderr)
                w = needed_w

    return w, h


# ---------- Flex ----------

_FLEX_FLOW_MAP = {
    "row": "ROW",
    "col": "COLUMN",
    "column": "COLUMN",
    "row-wrap": "ROW_WRAP",
    "row-reverse": "ROW_REVERSE",
    "col-reverse": "COLUMN_REVERSE",
    "column-reverse": "COLUMN_REVERSE",
}

_FLEX_JUSTIFY_MAP = {
    "start": "START",
    "end": "END",
    "center": "CENTER",
    "between": "SPACE_BETWEEN",
    "around": "SPACE_AROUND",
    "evenly": "SPACE_EVENLY",
}

_FLEX_ALIGN_MAP = {
    "start": "START",
    "end": "END",
    "center": "CENTER",
}


def apply_flex(node: Node, obj: dict[str, Any]) -> None:
    """Add flex styles to the container from data-layout/data-gap/data-justify/data-align. 根据 data-layout/data-gap/data-justify/data-align 给容器加 flex 样式。"""
    mode = node.get("data-layout", "")
    flow = _FLEX_FLOW_MAP.get(mode.lower(), "ROW")
    gap = int(node.get("data-gap", "4"))
    justify = _FLEX_JUSTIFY_MAP.get(node.get("data-justify", "start").lower(), "START")
    align = _FLEX_ALIGN_MAP.get(node.get("data-align", "start").lower(), "START")

    style = obj["localStyles"]
    if "definition" not in style:
        style["definition"] = {}
    main_def = style["definition"].setdefault("MAIN", {}).setdefault("DEFAULT", {})
    # Key point: layout=FLEX must be set first to activate flex; only then do flex_flow etc. take effect. 必须先设 layout=FLEX 激活，flex_flow 等才生效。
    main_def["layout"] = "FLEX"
    main_def["flex_flow"] = flow
    main_def["flex_main_place"] = justify
    main_def["flex_cross_place"] = align
    main_def["pad_row"] = gap
    main_def["pad_column"] = gap


def normalize_color(value: str) -> str:
    """Convert any color input to the #RRGGBB EEZ accepts (6 hex, case preserved).
    EEZ color-format.ts only accepts 3-6 hex (no alpha).
    - #RRGGBB / RRGGBB → returned as-is
    - #RRGGBBAA / RRGGBBAA → first 6 digits kept
    - rgb(r,g,b) / rgba(...) → converted

    把任意颜色输入转成 EEZ 接受的 #RRGGBB（3-6 hex，不含 alpha）。
    """
    s = value.strip()
    # Looks like #RRGGBBAA or RRGGBBAA (8 hex). 形如 #RRGGBBAA 或 RRGGBBAA（8 hex）。
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 8 and all(c in "0123456789abcdefABCDEF" for c in s):
        s = s[:6]  # drop alpha 丢 alpha
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return f"#{s}"
    if len(s) == 3 and all(c in "0123456789abcdefABCDEF" for c in s):
        # RGB → RRGGBB
        expanded = "".join(c * 2 for c in s)
        return f"#{expanded}"
    # Fallback for other formats. 其他格式兜底。
    return "#000000"


# ---------- More widgets 更多 Widget ----------

def build_dropdown(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    """<select><option>A</option><option>B</option></select>"""
    obj = base_widget("LVGLDropdownWidget", node, x, y, w, h)
    obj["clickableFlag"] = True
    obj["localStyles"] = local_styles_for(node)
    # Collect <option> texts. 收集 <option> 文本。
    options: list[str] = []
    for c in node.children:
        if c.tag == "option":
            options.append(c.text or c.get("value", ""))
    obj["options"] = "\n".join(options) if options else "Option 1\nOption 2"
    obj["optionsType"] = "literal"
    obj["selected"] = int(node.get("data-selected", "0"))
    obj["selectedType"] = "literal"
    obj["direction"] = node.get("data-direction", "bottom")
    obj["useStaticText"] = True
    obj["heightUnit"] = "content"
    return obj


def build_switch(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    """<switch> or <input type="checkbox" data-style="switch">"""
    obj = base_widget("LVGLSwitchWidget", node, x, y, w or 50, h or 25)
    obj["clickableFlag"] = True
    obj["widgetFlags"] = "CHECKABLE|CLICKABLE|CLICK_FOCUSABLE|PRESS_LOCK|GESTURE_BUBBLE|SNAPPABLE"
    var = node.get("data-var", "")
    if var:
        col.declare_var(var, "boolean", "false")
        obj["checkedStateType"] = "expression"
        obj["checkedState"] = var
    else:
        obj["checkedStateType"] = "literal"
        obj["checkedState"] = node.has("checked") or node.has("data-checked")
    return obj


def build_arc(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    """<arc data-var="X" data-min="0" data-max="100" data-value="25">"""
    obj = base_widget("LVGLArcWidget", node, x, y, w or 150, h or 150)
    obj["clickableFlag"] = True
    obj["useAngle"] = False
    obj["rangeMin"] = int(node.get("data-min", "0"))
    obj["rangeMinType"] = "literal"
    obj["rangeMax"] = int(node.get("data-max", "100"))
    obj["rangeMaxType"] = "literal"
    var = node.get("data-var", "")
    if var:
        col.declare_var(var, "integer", "0")
        obj["value"] = var
        obj["valueType"] = "expression"
    else:
        obj["value"] = int(node.get("data-value", "25"))
        obj["valueType"] = "literal"
    obj["valueStart"] = int(node.get("data-value-start", "0"))
    obj["valueStartType"] = "literal"

    # Angle fields (EEZ validates strictly; one missing reports "must be an integer"). 角度字段（缺一会报 "must be an integer"）。
    obj["mode"] = node.get("data-mode", "NORMAL")
    bg_start = int(node.get("data-bg-start-angle", "135"))
    bg_end = int(node.get("data-bg-end-angle", "45"))
    fg_start = int(node.get("data-start-angle", "135"))
    fg_end = int(node.get("data-end-angle", "45"))
    rotation = int(node.get("data-rotation", "0"))
    obj["startAngle"] = fg_start
    obj["startAngleType"] = "literal"
    obj["previewStartAngle"] = str(fg_start)
    obj["endAngle"] = fg_end
    obj["endAngleType"] = "literal"
    obj["previewEndAngle"] = str(fg_end)
    obj["bgStartAngle"] = bg_start
    obj["bgStartAngleType"] = "literal"
    obj["previewBgStartAngle"] = str(bg_start)
    obj["bgEndAngle"] = bg_end
    obj["bgEndAngleType"] = "literal"
    obj["previewBgEndAngle"] = str(bg_end)
    obj["rotation"] = rotation
    obj["rotationType"] = "literal"
    obj["previewRotation"] = str(rotation)
    return obj


def build_spinner(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    """<spinner>"""
    obj = base_widget("LVGLSpinnerWidget", node, x, y, w or 80, h or 80)
    return obj


def build_checkbox(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    """<input type="checkbox" data-text="启" data-var="cb_log">"""
    obj = base_widget("LVGLCheckboxWidget", node, x, y, w or 16, h or 16)
    obj["clickableFlag"] = True
    # Text (Checkbox must have text/textType fields; EEZ rendering breaks without them). Checkbox 必须有 text/textType 字段。
    text = node.get("data-text", node.text or "")
    obj["text"] = text
    obj["textType"] = "literal"
    obj["useStaticText"] = True
    # Content-sized (checkbox box + text measured together). 自适应尺寸（框 + 文字一起算大小）。
    obj["widthUnit"] = "content"
    obj["heightUnit"] = "content"
    # Checked state. 勾选状态。
    var = node.get("data-var", "")
    if var:
        col.declare_var(var, "boolean", "false")
        obj["checkedStateType"] = "expression"
        obj["checkedState"] = var
    else:
        obj["checkedStateType"] = "literal"
        obj["checkedState"] = node.has("checked") or node.has("data-checked")
    return obj


def build_led(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    """<led data-color="#FF0000" data-brightness="255">"""
    obj = base_widget("LVGLLedWidget", node, x, y, w or 32, h or 32)
    obj["clickableFlag"] = False
    # EEZ only accepts #RRGGBB (6 hex); HTML habit is #RRGGBBAA (8 hex with alpha) → normalize. EEZ 只认 #RRGGBB，HTML 习惯 8 hex → 规整。
    obj["color"] = normalize_color(node.get("data-color", "#0000FF"))
    obj["colorType"] = "literal"
    var = node.get("data-var", "")
    if var:
        col.declare_var(var, "integer", "0")
        obj["brightness"] = var
        obj["brightnessType"] = "expression"
    else:
        obj["brightness"] = int(node.get("data-brightness", "255"))
        obj["brightnessType"] = "literal"
    return obj


def build_widget(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    tag = node.tag
    if tag in ("h1", "h2", "h3", "p", "span", "label"):
        return build_label(node, x, y, w, h, col)
    if tag in ("button", "a"):
        return build_button(node, x, y, w, h, col)
    if tag == "img":
        return build_image(node, x, y, w, h, col)
    if tag == "input":
        t = node.get("type", "text")
        if t == "range":
            return build_bar_or_slider(node, x, y, w, h, col)
        if t in ("text", "password", "search"):
            return build_textarea(node, x, y, w, h, col)
        if t == "checkbox":
            # Default checkbox; data-style="switch" uses a Switch widget. 默认 checkbox；data-style="switch" 用 Switch。
            if node.get("data-style", "").lower() == "switch":
                return build_switch(node, x, y, w, h, col)
            return build_checkbox(node, x, y, w, h, col)
    if tag in ("select", "dropdown"):
        return build_dropdown(node, x, y, w, h, col)
    if tag == "switch":
        return build_switch(node, x, y, w, h, col)
    if tag == "arc":
        return build_arc(node, x, y, w, h, col)
    if tag == "spinner":
        return build_spinner(node, x, y, w, h, col)
    if tag == "led":
        return build_led(node, x, y, w, h, col)
    if tag == "div":
        return build_container(node, x, y, w, h, col)
    # Fallback. 兜底。
    return build_label(node, x, y, w, h, col)


def layout_children(parent: Node, parent_w: int) -> list[tuple[Node, int, int, int, int]]:
    """HTML-like layout for parent.children; returns [(child, x, y, w, h), ...]
    If parent has data-layout (flex), children get placeholder coordinates sized
    by content; LVGL flex re-layouts at runtime and overwrites them.

    对 parent.children 做 HTML-like 布局；flex 时子元素按内容尺寸占位，
    运行时被 LVGL flex 覆盖。
    """
    layout = Layout(parent_w, 0)
    result: list[tuple[Node, int, int, int, int]] = []
    cursor_y = 0
    cursor_x = 0
    row_h = 0
    gap = 4

    flex_mode = parent.has("data-layout")

    for child in parent.children:
        if child.tag in ("br", "#text", "#document"):
            if child.tag == "br" and cursor_x > 0:
                cursor_y += row_h + gap
                cursor_x = 0
                row_h = 0
            continue
        if child.tag == "hr":
            cursor_y += row_h + 4 if row_h else 4
            cursor_x = 0
            row_h = 0
            cursor_y += 8
            continue

        # Explicit user coordinates → used as-is, skipping flow layout. 用户显式指定坐标 → 直接用，不参与流式布局。
        if child.has("data-x") or child.has("data-y"):
            x, y, w, h = layout.place(child, parent_w, cursor_y)
            result.append((child, x, y, w, h))
            continue

        # Inside a flex container: children get content-sized placeholders (LVGL flex overrides coords at runtime). flex 容器内：子元素按内容尺寸占位。
        if flex_mode:
            w, h = layout.estimate_size(child, parent_w, default_h=40)
            result.append((child, 0, 0, w, h))
            continue

        if layout.is_inline(child):
            w, h = layout.estimate_size(child, parent_w)
            if cursor_x + w > parent_w and cursor_x > 0:
                cursor_y += row_h + gap
                cursor_x = 0
                row_h = 0
            result.append((child, cursor_x, cursor_y, w, h))
            cursor_x += w + gap
            row_h = max(row_h, h)
        else:
            if cursor_x > 0:
                cursor_y += row_h + gap
                cursor_x = 0
                row_h = 0
            w_attr = child.get("data-w", "")
            h_attr = child.get("data-h", "")
            w = int(w_attr) if w_attr else parent_w
            h = int(h_attr) if h_attr else 40
            result.append((child, 0, cursor_y, w, h))
            cursor_y += h + gap
    return result


def build_subtree(node: Node, x: int, y: int, w: int, h: int, col: Collector) -> dict[str, Any]:
    widget = build_widget(node, x, y, w, h, col)
    # Only container types recurse into children. 容器类才递归子节点。
    if widget["type"] in ("LVGLContainerWidget", "LVGLScreenWidget"):
        for child, cx, cy, cw, ch in layout_children(node, w):
            child_obj = build_subtree(child, cx, cy, cw, ch, col)
            widget["children"].append(child_obj)
    return widget


# ---------- Screen / Page ----------

def build_screen(body: Node, col: Collector) -> dict[str, Any]:
    sw = int(body.get("data-width", "1024"))
    sh = int(body.get("data-height", "600"))
    screen_name = body.get("data-screen", "main")

    # ScreenWidget root. ScreenWidget 根。
    screen = base_widget("LVGLScreenWidget", body, 0, 0, sw, sh)
    screen["clickableFlag"] = True
    screen["widgetFlags"] = (
        "CLICKABLE|PRESS_LOCK|CLICK_FOCUSABLE|GESTURE_BUBBLE|SNAPPABLE|"
        "SCROLLABLE|SCROLL_ELASTIC|SCROLL_MOMENTUM|SCROLL_CHAIN_HOR|SCROLL_CHAIN_VER"
    )

    for child, cx, cy, cw, ch in layout_children(body, sw):
        child_obj = build_subtree(child, cx, cy, cw, ch, col)
        screen["children"].append(child_obj)

    # Wrap into a Page (a userPages[] entry). 包装到 Page（userPages[] 条目）。
    page = {
        "objID": oid(),
        "components": [screen],
        "connectionLines": [],
        "localVariables": [],
        "componentGroups": [],
        "userProperties": [],
        "name": screen_name,
        "left": 0,
        "top": 0,
        "width": sw,
        "height": sh,
    }
    return page


# ---------- Font ----------

PROJECT_ROOT = Path(__file__).parent.resolve()
FONTS_DIR = PROJECT_ROOT / "fonts"


def parse_ranges(spec: str) -> list[dict[str, int]]:
    """'32-127,0x4E00-0x9FFF' → [{from:32,to:127}, ...]
    EEZ EncodingRange = {from, to, mapped_from?}
    """
    out: list[dict[str, int]] = []
    if not spec:
        return out
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        m = re.match(r"^(\d+|0x[0-9a-fA-F]+)\s*-\s*(\d+|0x[0-9a-fA-F]+)$", piece)
        if m:
            out.append({"from": int(m.group(1), 0), "to": int(m.group(2), 0)})
        else:
            n = int(piece, 0)
            out.append({"from": n, "to": n})
    return out


def load_font_catalog() -> list[dict[str, Any]]:
    """Read fonts/catalog.json and return the EEZ font JSON object for each font.
    Only catalog.json and meta.json are read (both KB-sized); binaries never enter memory.

    读取 fonts/catalog.json；只读 catalog/meta（KB 级），二进制不进内存。
    """
    catalog_path = FONTS_DIR / "catalog.json"
    if not catalog_path.exists():
        return []
    with open(catalog_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    fonts_json: list[dict[str, Any]] = []
    for entry in catalog.get("fonts", []):
        meta_path = FONTS_DIR / entry["meta"]
        if not meta_path.exists():
            print(f"⚠ Font {entry['name']} meta missing: {meta_path}", file=sys.stderr)
            continue
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        # Icon font merging (lvglAdditionalSources): declare external icon fonts to EEZ too;
        # EEZ merges glyphs from all sources when loading.
        # 图标字体合并：外部 icon 字体也声明给 EEZ，加载时合并字形。
        additional_sources = []
        for idx, icon in enumerate(meta.get("iconSources", [])):
            src_path = Path(icon["path"])
            if not src_path.exists():
                print(f"⚠ Font {meta['name']} icon source missing: {src_path}", file=sys.stderr)
                continue
            # Copy into fonts/ so relative paths resolve; idx distinguishes multiple sources. 复制到 fonts/ 目录；多源用 idx 区分。
            stem = src_path.stem
            dst_name = f"{meta['name']}_icons_{idx}_{stem}{src_path.suffix.lower()}" if len(meta.get("iconSources", [])) > 1 else f"{meta['name']}_icons{src_path.suffix.lower()}"
            dst_path = FONTS_DIR / dst_name
            if not dst_path.exists() or dst_path.stat().st_size != src_path.stat().st_size:
                shutil.copyfile(src_path, dst_path)
            additional_sources.append({
                "objID": oid(),
                "filePath": f"fonts/{dst_name}",
                "lvglRanges": icon.get("ranges", ""),
                "lvglSymbols": icon.get("symbols", ""),
            })

        fonts_json.append({
            "objID": oid(),
            "name": meta["name"],
            "renderingEngine": meta.get("renderingEngine", "LVGL"),
            "source": {
                "objID": oid(),
                "filePath": f"fonts/{meta['files']['src']}",
                "size": meta["source"]["size"],
                "threshold": meta["source"].get("threshold", 0),
            },
            "embeddedFontFile": "",   # embedFonts=false: small size, loaded from disk on demand. embedFonts=false：体积小，按需加载。
            "bpp": meta["bpp"],
            "threshold": meta.get("threshold", 0),
            "height": meta["height"],
            "ascent": meta["ascent"],
            "descent": meta["descent"],
            "glyphs": [],             # LVGL fonts use lvglGlyphs, not glyphs. LVGL 字体用 lvglGlyphs 而非 glyphs。
            "lvglRanges": meta.get("lvglRanges", ""),
            "lvglSymbols": meta.get("lvglSymbols", ""),
            "lvglAdditionalSources": additional_sources,
            "lvglGlyphs": {
                "encodings": parse_ranges(meta.get("lvglRanges", "")),
                "symbols": meta.get("lvglSymbols", ""),
            },
        })
    return fonts_json


# ---------- .eez-project assembly .eez-project 组装 ----------

def build_project(body: Node, col: Collector) -> dict[str, Any]:
    sw = int(body.get("data-width", "1024"))
    sh = int(body.get("data-height", "600"))

    # Build the page first; col.vars / col.actions fill up during the walk. 先构建页面，遍历中填充 col.vars / col.actions。
    page = build_screen(body, col)

    # Placeholder actions (referenced but not yet implemented by the user) — must come after build_screen. 占位 actions（被引用未实现）——必须在 build_screen 之后。
    action_objs = []
    for name in sorted(col.actions):
        action_objs.append({
            "objID": oid(),
            "components": [],
            "connectionLines": [],
            "localVariables": [],
            "componentGroups": [],
            "userProperties": [],
            "name": name,
            "implementationType": "native",
        })

    project = {
        "themesVersion": 1,
        "objID": oid(),
        "settings": {
            "objID": oid(),
            "general": {
                "objID": oid(),
                "projectVersion": "v3",
                "projectType": "lvgl",
                "lvglVersion": "9.5.0",
                "extensions": [],
                "imports": [],
                "flowSupport": True,
                "displayWidth": sw,
                "displayHeight": sh,
                "displayBorderRadius": 0,
                "darkTheme": True,
                "colorFormat": "BGR",
                "resourceFiles": [],
                "hiddenWidgetLines": "dimmed",
                "dimmedLinesOpacity": "20",
                "embedBitmaps": True,
                "embedFonts": False,    # fonts are not embedded in .eez-project; loaded on demand from fonts/ 字体不嵌入，从 fonts/ 按需加载。
                "cacheFonts": False,
            },
            "build": {
                "objID": oid(),
                "configurations": [
                    {"objID": oid(), "name": "Default"}
                ],
                "files": [
                    {
                        "objID": oid(),
                        "fileName": "ui.h",
                        "template": (
                            "#ifndef EEZ_LVGL_UI_GUI_H\n"
                            "#define EEZ_LVGL_UI_GUI_H\n"
                            "//${eez-studio LVGL_INCLUDE}\n"
                            "#ifdef __cplusplus\n"
                            'extern "C" {\n'
                            "#endif\n"
                            "void ui_init();\n"
                            "void ui_tick();\n"
                            "#ifdef __cplusplus\n"
                            "}\n"
                            "#endif\n"
                            "#endif\n"
                        ),
                    }
                ],
                "destinationFolder": ".",
                "separateFolderForImagesAndFonts": False,
                "imageExportMode": "source",
                "fontExportMode": "source",
                "lvglInclude": "lvgl.h",
                "screensLifetimeSupport": False,
                "useDockerDesktop": True,
                "generateSourceCodeForEezFramework": True,
                "compressFlowDefinition": False,
                "executionQueueSize": 1000,
                "expressionEvaluatorStackSize": 20,
            },
        },
        "variables": {
            "objID": oid(),
            "globalVariables": list(col.vars.values()),
        },
        "actions": action_objs,
        "userPages": [page],
        "userWidgets": [],
        "lvglStyles": {
            "objID": oid(),
            "styles": []
        },
        "lvglGroups": {"objID": oid(), "groups": []},
        "fonts": load_font_catalog(),
        "bitmaps": [],
        "colors": [],
        "themes": [
            {
                "objID": oid(),
                "name": "Default",
                "colors": {
                    "objID": oid(),
                    "background": "#000000FF",
                    "text": "#FFFFFFFF",
                    "content": "#FFFFFFFF",
                    "active": "#FFFFFFFF",
                    "border": "#FFFFFFFF",
                    "button": "#FFFFFFFF",
                    "chart": "#FFFFFFFF",
                },
            }
        ],
    }
    return project


# ---------- Main entry 主入口 ----------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="HTML → EEZ Studio .eez-project")
    ap.add_argument("input", help="path to the HTML file")
    ap.add_argument("-o", "--output", default="out.eez-project", help="output file")
    args = ap.parse_args(argv)

    with open(args.input, "r", encoding="utf-8") as f:
        html_text = f.read()

    root = parse_html(html_text)
    # Find <body>. 找 body。
    body = None

    def find(n: Node):
        nonlocal body
        if n.tag == "body":
            body = n
            return
        for c in n.children:
            find(c)

    find(root)
    if body is None:
        print("HTML is missing <body>", file=sys.stderr)
        return 1

    col = Collector()
    project = build_project(body, col)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(project, f, ensure_ascii=False, indent=2)

    # Console report. 控制台报告。
    print(f"✓ Generated {args.output}")
    print(f"  Screen:     {body.get('data-screen')}")
    print(f"  Resolution: {project['settings']['general']['displayWidth']}x{project['settings']['general']['displayHeight']}")
    print(f"  Variables:  {len(col.vars)}")
    for v in col.vars.values():
        print(f"     - {v['name']:20s} : {v['type']}")
    print(f"  Actions:    {len(col.actions)}")
    for a in sorted(col.actions):
        print(f"     - {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
