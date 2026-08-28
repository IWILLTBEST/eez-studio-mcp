"""
ir2eez — IR(JSON) → EEZ Studio .eez-project (LVGL v9) compiler.

IR is a LVGL-native UI description (no HTML/CSS semantics) with five top-level
sections:
    project   project metadata (resolution, etc.)
    variables global variable declarations (referenced by widgets via bind;
              undeclared ones are auto-inferred)
    widgets   reusable user widget definitions (e.g. a top nav bar, instantiated
              inside screens)
    screens   pages (widget trees; may instantiate components defined in widgets)
    actions   actions: a linear steps sequence → compiled into EEZ Flow
              (Start→nodes→lines + auto layout); actions without steps become
              native stubs (implemented in firmware C)

AI/humans only write semantics: no objID, no connection lines, no node
coordinates — everything is generated and validated by this compiler.

Usage:
    python ir2eez.py navbar_demo.ir.json -o out_ir.eez-project

ir2eez — IR(JSON) → EEZ Studio .eez-project (LVGL v9) 编译器。
IR 是 LVGL 原生界面描述：project 工程元信息、variables 全局变量声明、
widgets 可复用 user widget、screens 页面、actions 动作（steps → EEZ Flow，
无 steps 生成 native 空壳由固件 C 实现）。
AI/人只写语义：无 objID、无连线、无节点坐标，全部由本编译器生成并校验。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from generator import DEFAULT_FLAGS, load_font_catalog, normalize_color, oid

# Windows console defaults to GBK; force UTF-8. Windows 控制台默认 GBK，强制 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# ---------- Validation 校验 ----------

class IRError(Exception):
    pass


def fail(path: str, msg: str) -> None:
    raise IRError(f"{path}: {msg}")


def need_int(path: str, val: Any, default: int | None = None) -> int:
    if val is None:
        if default is not None:
            return default
        fail(path, "missing integer value")
    if isinstance(val, bool) or not isinstance(val, int):
        fail(path, f"expected integer, got {val!r}")
    return val


def need_str(path: str, val: Any, default: str | None = None) -> str:
    if val is None:
        if default is not None:
            return default
        fail(path, "missing string value")
    if not isinstance(val, str):
        fail(path, f"expected string, got {val!r}")
    return val


# ---------- Size estimation (for flex auto-grow & default sizes) 尺寸估算（flex 容器自动撑大 & 默认尺寸） ----------

def estimate_text_width(text: str, font_size: int) -> int:
    """CJK chars ≈ 1em, ASCII ≈ 0.6em. 中文≈1em，ASCII≈0.6em。"""
    w = 0
    for ch in text:
        w += font_size if ord(ch) > 0x2E80 else int(font_size * 0.6)
    return w


def font_size_of(font_name: str) -> int:
    """'source_16' → 16; falls back to 16 on parse failure. 'source_16' → 16，解析失败按 16。"""
    try:
        return int(font_name.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        return 16


# Default size per type: (w, h); used for flex child defaults & parent size estimation. 类型默认尺寸：w, h（flex 子元素缺省 / 估父容器大小时用）
DEFAULT_SIZE: dict[str, tuple[int, int]] = {
    "button": (120, 40),
    "label": (80, 24),
    "image": (64, 64),
    "dropdown": (150, 40),
    "bar": (200, 12),
    "slider": (200, 12),
    "textarea": (160, 40),
    "checkbox": (120, 24),
    "switch": (50, 25),
    "arc": (150, 150),
    "spinner": (64, 64),
    "led": (24, 24),
    "container": (200, 40),
    "panel": (200, 40),
    "line": (100, 1),
    "canvas": (180, 100),
}

# Property bound per widget type & inferred variable type. bind 到不同 widget 时绑定的属性 & 推断变量类型
BIND_TARGET: dict[str, tuple[str, str, str]] = {
    # type → (EEZ property name, variable type, default value)
    "label": ("text", "string", '""'),
    "textarea": ("text", "string", '""'),
    "bar": ("value", "integer", "0"),
    "slider": ("value", "integer", "0"),
    "arc": ("value", "integer", "0"),
    "led": ("brightness", "integer", "0"),
    "switch": ("checkedState", "boolean", "false"),
    "checkbox": ("checkedState", "boolean", "false"),
}


# Type prefixes for identifiers (the EEZ object tree shows identifiers; the prefix
# makes the object type obvious at a glance). Must be all lowercase: EEZ stores
# identifiers as UnderscoreLowerCase and looks up actions by exact-name indexOf;
# uppercase causes "Widget index not found" (C vars are also lowercase, e.g. objects.panel_xxx).
# identifier 类型前缀：必须全小写，带大写会 "Widget index not found"。
_TYPE_PREFIX = {
    "LVGLLabelWidget": "label_",
    "LVGLButtonWidget": "button_",
    "LVGLPanelWidget": "panel_",
    "LVGLContainerWidget": "panel_",
    "LVGLSliderWidget": "slider_",
    "LVGLBarWidget": "bar_",
    "LVGLDropdownWidget": "dropdown_",
    "LVGLSwitchWidget": "switch_",
    "LVGLLedWidget": "led_",
    "LVGLCanvasWidget": "canvas_",
    "LVGLLineWidget": "line_",
    "LVGLArcWidget": "arc_",
    "LVGLCheckboxWidget": "checkbox_",
    "LVGLTextareaWidget": "textarea_",
    "LVGLImageWidget": "image_",
    "LVGLSpinnerWidget": "spinner_",
    "LVGLUserWidgetWidget": "widget_",
    "LVGLScreenWidget": "screen_",
}


# ---------- Variable collection 变量收集 ----------

class VarCollector:
    def __init__(self, declared: list[dict[str, Any]]):
        # declared: the IR "variables" section. declared: IR variables 段。
        self.vars: dict[str, dict[str, Any]] = {}
        self.explicit: set[str] = set()
        for v in declared:
            name = need_str("variables[].name", v.get("name"))
            vtype = need_str("variables[].type", v.get("type"), "string")
            if vtype not in ("integer", "float", "double", "boolean", "string"):
                fail(f"variables[{name!r}].type", f"unsupported type {vtype!r}")
            default = v.get("default")
            if default is None:
                default = {"string": '""', "integer": "0", "float": "0",
                           "double": "0", "boolean": "false"}[vtype]
            elif vtype == "string":
                # IR writes plain "Home"; compiled into the EEZ expression "\"Home\"". IR 里直接写 "Home"，编译成 EEZ 表达式 "\"Home\""
                default = json.dumps(str(default), ensure_ascii=False)
            else:
                default = str(default).lower()
            self.vars[name] = {
                "objID": oid(),
                "name": name,
                "type": vtype,
                "defaultValue": default,
                "persistent": False,
                "native": bool(v.get("native", True)),
            }
            self.explicit.add(name)

    def infer(self, name: str, vtype: str, default: str) -> None:
        """bind references an undeclared variable → declare it automatically. bind 引用了未声明的变量 → 自动声明。"""
        if name in self.vars:
            if name not in self.explicit and self.vars[name]["type"] != vtype:
                print(f"⚠ Inconsistent type for variable {name}: first inferred as "
                      f"{self.vars[name]['type']}, then used as {vtype}; keeping the first",
                      file=sys.stderr)
            return
        self.vars[name] = {
            "objID": oid(),
            "name": name,
            "type": vtype,
            "defaultValue": default,
            "persistent": False,
            "native": True,
        }


# ---------- Widget construction widget 构造 ----------

FLEX_FLOW = {"row": "ROW", "col": "COLUMN", "column": "COLUMN",
             "row-wrap": "ROW_WRAP", "row-reverse": "ROW_REVERSE",
             "col-reverse": "COLUMN_REVERSE", "column-reverse": "COLUMN_REVERSE"}
FLEX_JUSTIFY = {"start": "START", "end": "END", "center": "CENTER",
                "between": "SPACE_BETWEEN", "around": "SPACE_AROUND",
                "evenly": "SPACE_EVENLY"}
FLEX_ALIGN = {"start": "START", "end": "END", "center": "CENTER"}

WIDGET_TYPES = set(DEFAULT_SIZE) | {"container"}


class Compiler:
    def __init__(self, ir: dict[str, Any]):
        self.ir = ir
        proj = ir.get("project") or {}
        self.sw = need_int("project.width", proj.get("width"), 1024)
        self.sh = need_int("project.height", proj.get("height"), 600)
        self.widget_defs: dict[str, dict[str, Any]] = {}
        for name, w in (ir.get("widgets") or {}).items():
            if not isinstance(w, dict):
                fail(f"widgets[{name!r}]", "expected object")
            self.widget_defs[name] = w
        self.known_ids: set[str] = set()   # all full identifiers (for LVGL action target validation) 所有完整 identifier（lvgl 动作目标校验用）
        # IR short id → full identifier with type prefix (label_xxx / panel_xxx / button_xxx, ...).
        # Flow targets may use short ids; they are mapped automatically at compile time.
        # flow 的 target 写简短 id，编译时自动映射到完整 identifier。
        self.id_map: dict[str, str] = {}
        # Action name set (explicitly defined + referenced by events). action 名集合（显式定义 + 事件引用）。
        self.actions_ir: list[dict[str, Any]] = ir.get("actions") or []
        self.action_names: set[str] = set()
        for a in self.actions_ir:
            self.action_names.add(need_str("actions[].name", a.get("name")))
        self.pending_actions: set[str] = set()   # referenced by events but undefined → native stubs 事件引用但未定义 → native 空壳
        # Native action list (incl. explicit step-less ones): name → is value-change kind (takes a value param). native 动作清单：name → 是否值变化类（带 value 参数）
        self.native_actions: dict[str, bool] = {}
        # Which events reference each action (explicit ones too; used for action.h signatures). 每个动作被哪些事件引用（action.h 签名用）。
        self.action_event_kinds: dict[str, set[str]] = {}
        self.vars = VarCollector(ir.get("variables") or [])
        self.default_font = need_str("project.font", proj.get("font"), "")
        self.errors: list[str] = []

    def err(self, path: str, msg: str) -> None:
        self.errors.append(f"{path}: {msg}")

    # ----- Common fields 公共字段 -----

    def base(self, wtype: str, node: dict[str, Any], path: str,
             x: int, y: int, w: int, h: int) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "objID": oid(),
            "type": wtype,
            "left": x,
            "top": y,
            "width": w,
            "height": h,
            "customInputs": [],
            "customOutputs": [],
            "style": {"objID": oid(), "useStyle": "default",
                      "conditionalStyles": [], "childStyle": []},
            "timeline": [],
            "eventHandlers": self.event_handlers(node, path),
            "leftUnit": "px", "topUnit": "px", "widthUnit": "px", "heightUnit": "px",
            "children": [],
            "widgetFlags": DEFAULT_FLAGS,
            "hiddenFlagType": "literal",
            "hiddenFlag": bool(node.get("hidden", False)),
            "clickableFlagType": "literal",
            "clickableFlag": False,
            "flagScrollbarMode": "", "flagScrollDirection": "",
            "scrollSnapX": "", "scrollSnapY": "",
            "checkedStateType": "literal",
            "disabledStateType": "literal",
            "states": "",
            "localStyles": {"objID": oid()},
            "group": "", "groupIndex": 0,
        }
        if node.get("id"):
            semantic = str(node["id"])
            prefix = _TYPE_PREFIX.get(wtype, "w_")
            word = prefix.rstrip("_")
            # Avoid double prefix when the semantic id already carries the type word (canvas_ch1 stays canvas_ch1). 语义 id 已带类型词时避免双重前缀。
            full = semantic if (semantic.startswith(word + "_") or semantic == word) \
                else prefix + semantic
            obj["identifier"] = full
            self.known_ids.add(full)
            self.id_map[semantic] = full
        init_states = []
        if node.get("checked"):
            init_states.append("CHECKED")
        if node.get("disabled"):
            init_states.append("DISABLED")
        if init_states:
            # Initial states (EEZ "states" field; pairs with state styles / objAddState actions). 初始状态（EEZ states 字段，配合 states 样式 / objAddState 动作）。
            obj["states"] = "|".join(init_states)
        return obj

    def event_handlers(self, node: dict[str, Any], path: str) -> list[dict[str, Any]]:
        handlers = []
        for evt, act in (node.get("events") or {}).items():
            evt_u = str(evt).upper()
            if not isinstance(act, str):
                self.err(f"{path}.events[{evt!r}]", "value must be an action name string")
                continue
            # Record event kinds (for action.h signature inference: VALUE_CHANGED-triggered ones take a value param). 记录事件种类（action.h 签名推导用）。
            self.action_event_kinds.setdefault(act, set()).add(evt_u)
            if act not in self.action_names:
                self.pending_actions.add(act)
            handlers.append({
                "objID": oid(),
                "eventName": evt_u,
                "handlerType": "action",
                "action": act,
                "userData": 0,
            })
        return handlers

    def styles_for(self, node: dict[str, Any], path: str,
                   extra: dict[str, Any] | None = None,
                   use_default_font: bool = True) -> dict[str, Any]:
        """font/color/bg/radius → localStyles.definition MAIN.DEFAULT;
        node.states = {"CHECKED": {"bg": ..., "color": ...}} → MAIN.CHECKED (selected-state
        style; combined with objAddState/objClearState actions for selection highlight).

        font/color/bg/radius → MAIN.DEFAULT；states → MAIN.CHECKED（选中态样式，
        配合 objAddState/objClearState 实现选中高亮）。"""
        props: dict[str, Any] = dict(extra or {})
        font = node.get("font") or (self.default_font if use_default_font else "")
        if font:
            props["text_font"] = font
        if node.get("color"):
            props["text_color"] = normalize_color(str(node["color"]))
        if node.get("bg"):
            props["bg_color"] = normalize_color(str(node["bg"]))
        if node.get("radius") is not None:
            props["radius"] = need_int(f"{path}.radius", node.get("radius"), 0)
        if node.get("bgOpa") is not None:
            props["bg_opa"] = need_int(f"{path}.bgOpa", node.get("bgOpa"), 255)
        definition: dict[str, Any] = {}
        if props:
            definition["MAIN"] = {"DEFAULT": props}
        for state, sprops in (node.get("states") or {}).items():
            sp: dict[str, Any] = {}
            if sprops.get("bg"):
                sp["bg_color"] = normalize_color(str(sprops["bg"]))
            if sprops.get("color"):
                sp["text_color"] = normalize_color(str(sprops["color"]))
            if sprops.get("radius") is not None:
                sp["radius"] = int(sprops["radius"])
            if not sp:
                self.err(f"{path}.states[{state!r}]", "empty state style")
                continue
            definition.setdefault("MAIN", {})[str(state).upper()] = sp
        if not definition:
            return {"objID": oid()}
        return {"objID": oid(), "definition": definition}

    # ----- Per-widget builders 各 widget -----

    def build_widget(self, node: dict[str, Any], path: str,
                     x: int, y: int, w: int, h: int) -> dict[str, Any]:
        # user widget instance ({"widget": "NavBar"}; no type needed). user widget 实例（无需 type）。
        if "widget" in node:
            ref = need_str(f"{path}.widget", node.get("widget"))
            if ref not in self.widget_defs:
                self.err(path, f"references undefined user widget {ref!r}")
            d = self.widget_defs.get(ref) or {}
            obj = self.base("LVGLUserWidgetWidget", node, path, x, y,
                            need_int(f"{path}.w", node.get("w"),
                                     need_int(f"widgets[{ref!r}].width", d.get("width"), 100)),
                            need_int(f"{path}.h", node.get("h"),
                                     need_int(f"widgets[{ref!r}].height", d.get("height"), 50)))
            obj["userWidgetPageName"] = ref
            if node.get("children"):
                self.err(path, "user widget instance cannot have children")
            return obj

        wtype = need_str(f"{path}.type", node.get("type"))
        if wtype not in WIDGET_TYPES:
            self.err(f"{path}.type", f"unknown widget type {wtype!r}")
            wtype = "label"

        builder = getattr(self, f"_build_{wtype}", None)
        if builder is None:
            self.err(f"{path}.type", f"widget type {wtype!r} not yet supported")
            wtype = "label"
            builder = self._build_label
        return builder(node, path, x, y, w, h)

    def _bind(self, node: dict[str, Any], path: str, wtype: str) -> tuple[str, str] | None:
        """Returns (property name, variable name); None if no bind. 返回 (属性名, 变量名)；无 bind 返回 None。"""
        var = node.get("bind")
        if var is None:
            return None
        if not isinstance(var, str) or not var:
            self.err(f"{path}.bind", "expected variable name string")
            return None
        prop, vtype, default = BIND_TARGET[wtype]
        self.vars.infer(var, vtype, default)
        return prop, var

    def _build_label(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLLabelWidget", n, p, x, y, w, h)
        obj["localStyles"] = self.styles_for(n, p)
        bind = self._bind(n, p, "label")
        if bind:
            obj["text"], obj["textType"] = bind[1], "expression"
            obj["previewValue"] = str(n.get("preview", bind[1]))
            text = str(n.get("preview", bind[1]))
        else:
            obj["text"] = need_str(f"{p}.text", n.get("text"), "Label")
            obj["textType"] = "literal"
            text = str(obj["text"])
        # Height guard: never smaller than font line-height × line count (16px font ≈ line-height 20; h=14 clips). 高度兜底：不小于字体行高×行数。
        font = str(n.get("font") or self.default_font or "x_16")
        line_h = int(font_size_of(font) * 1.25) + 1
        need_h = (text.count("\n") + 1) * line_h
        if obj["height"] < need_h:
            obj["height"] = need_h

        # Width guard: estimate the needed width from the longest line (CJK ≈ font size,
        # ASCII ≈ 0.6×size + padding) to prevent truncated/wrapped text (AI often writes too-narrow w in IR).
        # 宽度兜底：按最长行估算所需宽度，防止文字被截断/换行。
        fs = font_size_of(font)
        longest_line = max(text.split("\n"), key=len) if "\n" in text else text
        need_w = 0
        for ch in longest_line:
            need_w += fs if ord(ch) > 0x2E80 else int(fs * 0.65)
        need_w += 16  # left/right padding 左右 padding
        if obj["width"] < need_w:
            obj["width"] = need_w

        # No wrapping (width is guarded above — clipping beats wrapping). 不换行（宽度已兜底，截断比换行好）。
        obj["longMode"] = "WRAP"
        obj["recolor"] = False
        # Text alignment (IR: align = left/center/right/auto). A variable-bound numeric label
        # is a fixed-width box with an estimated width; default left alignment makes the
        # runtime digits hug the left — centered boxes should use align=center.
        # Note the style property is text_align (aligns text lines only); align is object
        # alignment (ALIGN_CENTER moves the object to the parent center; left/top become offsets).
        # 文字对齐：注意 text_align（对齐文字）与 align（对象对齐）是两个不同的属性。
        align = str(n.get("align", "")).strip().lower()
        if align in ("left", "center", "right", "auto"):
            obj["localStyles"].setdefault("definition", {}).setdefault(
                "MAIN", {}
            ).setdefault("DEFAULT", {})["text_align"] = align.upper()
        return obj

    def _build_button(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLButtonWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        # Default card bg + radius: without bg, the LVGL theme's default (grey) button color shows through. 默认卡片底色+圆角。
        obj["localStyles"] = self.styles_for(n, p, extra={"radius": 6, "bg_color": "#1C2333"})
        text = need_str(f"{p}.text", n.get("text"), "Button")
        # The child label sets no text_color of its own — it inherits the button's
        # (LVGL inherited property) so CHECKED/PRESSED text-color switches apply to it.
        # 子 label 不写 text_color，继承按钮的，状态变色才能作用到文字。
        lbl = self._build_label({"text": text, "font": n.get("font")},
                                f"{p}.label", 0, 0, 80, 32)
        lbl["widthUnit"] = "content"
        lbl["heightUnit"] = "content"
        # Center within the button. 居中于按钮。
        d = lbl["localStyles"].setdefault("definition", {})
        d.setdefault("MAIN", {}).setdefault("DEFAULT", {})["align"] = "CENTER"
        obj["children"].append(lbl)
        return obj

    def _build_image(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLImageWidget", n, p, x, y, w, h)
        obj["image"] = need_str(f"{p}.src", n.get("src"), "")
        obj["pivotX"] = 0
        obj["pivotY"] = 0
        return obj

    def _build_bar(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLBarWidget", n, p, x, y, w, h)
        obj["min"] = need_int(f"{p}.min", n.get("min"), 0)
        obj["minType"] = "literal"
        obj["max"] = need_int(f"{p}.max", n.get("max"), 100)
        obj["maxType"] = "literal"
        obj["mode"] = "NORMAL"
        bind = self._bind(n, p, "bar")
        if bind:
            obj["value"], obj["valueType"] = bind[1], "expression"
            obj["valueStart"] = 0
            obj["valueStartType"] = "literal"
        else:
            obj["value"] = need_int(f"{p}.value", n.get("value"), 0)
            obj["valueType"] = "literal"
        obj["enableAnimation"] = False
        return obj

    def _build_slider(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self._build_bar(n, p, x, y, w, h)
        obj["type"] = "LVGLSliderWidget"
        obj["clickableFlag"] = True
        obj["knob"] = ""
        # Prefix follows the type: rewrite the bar-built identifier/id_map entries to slider_. 前缀随类型改成 slider_。
        ident = obj.get("identifier")
        if ident and ident.startswith("bar_"):
            semantic = ident[len("bar_"):]
            new = "slider_" + semantic
            obj["identifier"] = new
            self.known_ids.discard(ident)
            self.known_ids.add(new)
            if semantic in self.id_map:
                self.id_map[semantic] = new
        return obj

    def _build_textarea(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLTextareaWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        bind = self._bind(n, p, "textarea")
        if bind:
            obj["text"], obj["textType"] = bind[1], "expression"
            obj["previewValue"] = str(n.get("preview", ""))
        else:
            obj["text"] = need_str(f"{p}.text", n.get("text"), "")
            obj["textType"] = "literal"
        obj["longMode"] = "WRAP"
        obj["recolor"] = False
        obj["oneLineMode"] = True
        obj["passwordMode"] = bool(n.get("password", False))
        obj["acceptedCharacters"] = ""
        obj["maxTextLength"] = 128
        return obj

    def _build_dropdown(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLDropdownWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        obj["localStyles"] = self.styles_for(n, p)
        opts = n.get("options")
        if not isinstance(opts, list) or not all(isinstance(o, str) for o in opts):
            self.err(f"{p}.options", "expected an array of strings")
            opts = ["Option 1", "Option 2"]
        # The expanded list uses LV_FONT_DEFAULT (montserrat); CJK turns into boxes → warn. 展开列表用 montserrat，中文会变方框。
        if any(ord(c) > 0x2E80 for o in opts for c in o):
            print(f"⚠ {p}: dropdown options contain CJK characters; EEZ renders the "
                  f"expanded list with montserrat and they will show as boxes", file=sys.stderr)
        obj["options"] = "\n".join(opts)
        obj["optionsType"] = "literal"
        obj["selected"] = need_int(f"{p}.selected", n.get("selected"), 0)
        obj["selectedType"] = "literal"
        obj["direction"] = str(n.get("direction", "bottom"))
        obj["useStaticText"] = True
        # Height: an explicit h pins px (content mode follows font line-height; a 16px font grows to 30+). 高度：显式 h 用 px 定高。
        if n.get("h") is None:
            obj["heightUnit"] = "content"
        return obj

    def _build_switch(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLSwitchWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        obj["widgetFlags"] = ("CHECKABLE|CLICKABLE|CLICK_FOCUSABLE|PRESS_LOCK|"
                              "GESTURE_BUBBLE|SNAPPABLE")
        bind = self._bind(n, p, "switch")
        if bind:
            obj["checkedStateType"], obj["checkedState"] = "expression", bind[1]
        else:
            obj["checkedStateType"] = "literal"
            obj["checkedState"] = bool(n.get("checked", False))
        return obj

    def _build_checkbox(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLCheckboxWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        obj["text"] = need_str(f"{p}.text", n.get("text"), "")
        obj["textType"] = "literal"
        obj["useStaticText"] = True
        obj["widthUnit"] = "content"
        obj["heightUnit"] = "content"
        bind = self._bind(n, p, "checkbox")
        if bind:
            obj["checkedStateType"], obj["checkedState"] = "expression", bind[1]
        else:
            obj["checkedStateType"] = "literal"
            obj["checkedState"] = bool(n.get("checked", False))
        return obj

    def _build_arc(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLArcWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        obj["useAngle"] = False
        obj["rangeMin"] = need_int(f"{p}.min", n.get("min"), 0)
        obj["rangeMinType"] = "literal"
        obj["rangeMax"] = need_int(f"{p}.max", n.get("max"), 100)
        obj["rangeMaxType"] = "literal"
        bind = self._bind(n, p, "arc")
        if bind:
            obj["value"], obj["valueType"] = bind[1], "expression"
        else:
            obj["value"] = need_int(f"{p}.value", n.get("value"), 25)
            obj["valueType"] = "literal"
        obj["valueStart"] = 0
        obj["valueStartType"] = "literal"
        # A missing angle field makes EEZ report "must be an integer" (historical pitfall). 角度字段缺一 EEZ 报 "must be an integer"。
        obj["mode"] = str(n.get("mode", "NORMAL"))
        bg_s = need_int(f"{p}.bgStartAngle", n.get("bgStartAngle"), 135)
        bg_e = need_int(f"{p}.bgEndAngle", n.get("bgEndAngle"), 45)
        fg_s = need_int(f"{p}.startAngle", n.get("startAngle"), 135)
        fg_e = need_int(f"{p}.endAngle", n.get("endAngle"), 45)
        rot = need_int(f"{p}.rotation", n.get("rotation"), 0)
        for key, val in (("startAngle", fg_s), ("endAngle", fg_e),
                         ("bgStartAngle", bg_s), ("bgEndAngle", bg_e),
                         ("rotation", rot)):
            obj[key] = val
            obj[key + "Type"] = "literal"
            obj["preview" + key[0].upper() + key[1:]] = str(val)
        return obj

    def _build_spinner(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        return self.base("LVGLSpinnerWidget", n, p, x, y, w, h)

    def _build_canvas(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        """Custom-draw area (waveforms etc.): lv_canvas_create; the buffer is filled by
        firmware at runtime (lv_canvas_set_buffer + identifier for code-side lookup).

        波形等自绘区域：缓冲区由固件运行时填充（identifier 供代码定位）。"""
        obj = self.base("LVGLCanvasWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = False
        obj["localStyles"] = self.styles_for(n, p, use_default_font=False)
        return obj

    def _build_line(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        """Divider line (see ppa32's LVGLLineWidget usage): dir="h" (default, w is the
        length) or "v" (h is the length); color defaults to border grey.

        分割线：dir="h"（w 为长度）或 "v"（h 为长度），color 默认边框灰。"""
        obj = self.base("LVGLLineWidget", n, p, x, y, w, h)
        vertical = str(n.get("dir", "h" if w >= h else "v")).lower().startswith("v")
        length = h if vertical else w
        obj["widthUnit"] = "content"
        obj["heightUnit"] = "content"
        obj["points"] = f"0,0 1,{length}" if vertical else f"0,0 {length},1"
        obj["invertY"] = True
        obj["needleLength"] = 0
        obj["value"] = 0
        obj["valueType"] = "literal"
        obj["previewValue"] = 0
        obj["widgetFlags"] = ("CLICK_FOCUSABLE|GESTURE_BUBBLE|PRESS_LOCK|SCROLLABLE|"
                              "SCROLL_CHAIN_HOR|SCROLL_CHAIN_VER|SCROLL_ELASTIC|"
                              "SCROLL_MOMENTUM|SCROLL_WITH_ARROW|SNAPPABLE")
        obj["localStyles"] = {"objID": oid(), "definition": {"MAIN": {"DEFAULT": {
            "line_color": normalize_color(str(n.get("color", "#2A3040"))),
            "line_width": 1,
        }}}}
        return obj

    def _build_led(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLLedWidget", n, p, x, y, w, h)
        # EEZ/theme default shadow_width=12 causes a glow; zero it for a clean dot. 显式归零 shadow 得到干净圆点。
        obj["localStyles"] = {"objID": oid(), "definition": {"MAIN": {"DEFAULT": {
            "shadow_width": 0,
        }}}}
        # EEZ LED color must be a literal #RRGGBB; only brightness (0-255) can bind a variable. color 只能字面量，brightness 可绑变量。
        obj["color"] = normalize_color(need_str(f"{p}.color", n.get("color"), "#0000FF"))
        obj["colorType"] = "literal"
        bind = self._bind(n, p, "led")
        if bind:
            obj["brightness"], obj["brightnessType"] = bind[1], "expression"
        else:
            obj["brightness"] = need_int(f"{p}.brightness", n.get("brightness"), 255)
            obj["brightnessType"] = "literal"
        return obj

    def _build_box(self, n: dict, p: str, x: int, y: int, w: int, h: int,
                   wtype: str) -> dict:
        """Shared by container/panel: both are lv_obj_create and must explicitly zero
        padding/border — the default LVGL theme adds a card style to plain lv_obj
        (pad_all≈16-24px + 2px border). Child coordinates start from the content area
        (origin + padding + border), so each unreset nested container shifts its subtree
        by ~18-26px (EEZ does the same zeroing only for user widget instances in the C
        build path; see UserWidget.tsx buildStyleIfNotDefined).

        container/panel 共用：必须显式清零 padding/border，否则默认主题 card 样式
        会让每层嵌套子树整体偏移 ~18-26px。"""
        if n.get("layout"):
            w, h = self.flex_autosize(n, p, w, h)
        obj = self.base(wtype, n, p, x, y, w, h)
        # With events (e.g. tap-to-select file entries) CLICKABLE is required to fire;
        # "clickable": true alone is for transparent click shields (swallow clicks, no events).
        # 有事件必须 CLICKABLE 才触发；clickable:true 用于透明点击屏蔽板。
        obj["clickableFlag"] = bool(n.get("events") or n.get("clickable"))
        # Scroll region: the viewport panel matches screen height; children overflowing
        # the content area make it scrollable. CLICKABLE is also required — touch-drag
        # scrolling needs the object to accept press events.
        # 滚动区域：子元素超出内容区即可滚动，且必须 CLICKABLE。
        if n.get("scrollable"):
            obj["widgetFlags"] += "|SCROLLABLE"
            obj["clickableFlag"] = True
        obj["localStyles"] = self.styles_for(n, p, extra={
            "pad_left": 0, "pad_top": 0, "pad_right": 0, "pad_bottom": 0,
            "border_width": 0,
        })
        if n.get("layout"):
            self.apply_flex(n, obj)
        self.fill_children(obj, n, p)
        return obj

    def _build_container(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        return self._build_box(n, p, x, y, w, h, "LVGLContainerWidget")

    def _build_panel(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        return self._build_box(n, p, x, y, w, h, "LVGLPanelWidget")

    def apply_flex(self, n: dict, obj: dict) -> None:
        mode = str(n.get("layout", "row")).lower()
        flow = FLEX_FLOW.get(mode, "ROW")
        gap = need_int("gap", n.get("gap"), 4)
        justify = FLEX_JUSTIFY.get(str(n.get("justify", "start")).lower(), "START")
        align = FLEX_ALIGN.get(str(n.get("align", "start")).lower(), "START")
        d = obj["localStyles"].setdefault("definition", {})
        main = d.setdefault("MAIN", {}).setdefault("DEFAULT", {})
        # layout=FLEX must be set first to activate flex; only then do flex_flow etc. take effect. 必须先 layout=FLEX 激活。
        main["layout"] = "FLEX"
        main["flex_flow"] = flow
        main["flex_main_place"] = justify
        main["flex_cross_place"] = align
        main["pad_row"] = gap
        main["pad_column"] = gap

    # ----- Child layout 子元素布局 -----

    def estimate_size(self, node: dict, p: str, parent_w: int) -> tuple[int, int]:
        """Estimate flex child size (for parent auto-grow / placeholders). 估算 flex 子元素尺寸（撑大父容器/占位用）。"""
        dw, dh = DEFAULT_SIZE.get(str(node.get("type", "label")), (80, 24))
        if "widget" in node:
            d = self.widget_defs.get(node["widget"], {})
            return (need_int("w", node.get("w"), need_int("width", d.get("width"), 100)),
                    need_int("h", node.get("h"), need_int("height", d.get("height"), 50)))
        w = need_int(f"{p}.w", node.get("w"), None) if node.get("w") else None
        h = need_int(f"{p}.h", node.get("h"), None) if node.get("h") else None
        t = str(node.get("type", "label"))
        if w is None and t in ("button",):
            text = str(node.get("text", "Btn"))
            fs = font_size_of(str(node.get("font") or self.default_font or "x_16"))
            w = estimate_text_width(text, fs) + 48
        if w is None and t == "label":
            text = str(node.get("text") or node.get("preview") or node.get("bind") or "Label")
            fs = font_size_of(str(node.get("font") or self.default_font or "x_16"))
            w = estimate_text_width(text, fs)
        if w is None:
            w = dw if t not in ("container", "panel") else parent_w
        if h is None:
            h = dh
            if t == "label":
                h = font_size_of(str(node.get("font") or self.default_font or "x_16")) + 10
        return w, h

    def flex_autosize(self, node: dict, p: str, w: int, h: int) -> tuple[int, int]:
        """Grow a flex container to fit its children (declared sizes win; grow-only, never shrink):
        - row: h = max(child h) (gap is child-to-child spacing, not container padding — not added)
        - col: h = sum(child h) + gap*(n-1), w = max(child w)
        Note: def nodes use height/width keys, regular nodes use h/w; both are accepted.

        flex 容器按子元素撑大（显式声明优先，只撑大不缩小）：
        row: h=max(子h)；col: h=sum(子h)+gap*(n-1)，w=max(子w)。
        def 节点用 height/width 键，普通节点用 h/w 键，两者都认。
        """
        gap = need_int("gap", node.get("gap"), 4)
        kids = node.get("children") or []
        if not kids:
            return w, h
        sizes = [self.estimate_size(c, f"{p}.children[{i}]", w) for i, c in enumerate(kids)]
        is_col = str(node.get("layout", "row")).lower().startswith("col")
        declared_h = node.get("h", node.get("height"))
        declared_w = node.get("w", node.get("width"))
        if is_col:
            need_h = sum(s[1] for s in sizes) + gap * (len(sizes) - 1)
            need_w = max(s[0] for s in sizes)
        else:
            need_h = max(s[1] for s in sizes)
            need_w = sum(s[0] for s in sizes) + gap * (len(sizes) - 1)
        # Height: declared value wins; warn and grow when insufficient
        if declared_h is not None:
            h_final = max(int(declared_h), need_h)
            if need_h > int(declared_h):
                print(f"⚠ {p}: declared height {declared_h} < {need_h} required by children, "
                      f"grown to {h_final}", file=sys.stderr)
        else:
            h_final = max(h, need_h)
        # Width: col grows to fit children (declared wins); row keeps the incoming w. 宽度：col 按子元素撑（声明优先）；row 沿用传入 w。
        if is_col:
            if declared_w is not None:
                w_final = max(int(declared_w), need_w)
            else:
                w_final = max(w, need_w)
        else:
            w_final = w
        return w_final, h_final

    def place_children(self, node: dict, p: str, parent_w: int) -> list[dict[str, Any]]:
        """Lay out and build node.children; returns the component list:
        - flex container: children placed at (0,0); LVGL re-flows at runtime
        - explicit x/y: used as-is
        - no coordinates: stacked vertically

        对 node.children 布局并构建：flex 占位 (0,0) 运行时重排；
        显式 x/y 直接用；无坐标竖向堆叠。
        """
        kids = node.get("children") or []
        flex_mode = bool(node.get("layout"))
        cursor_y = 0
        gap = need_int("gap", node.get("gap"), 4)
        out: list[dict[str, Any]] = []
        for i, c in enumerate(kids):
            cp = f"{p}.children[{i}]"
            if not isinstance(c, dict):
                self.err(cp, "expected object")
                continue
            if flex_mode:
                w, h = self.estimate_size(c, cp, parent_w)
                child = self.build_widget(c, cp, 0, 0, w, h)
            elif c.get("x") is not None or c.get("y") is not None:
                x = need_int(f"{cp}.x", c.get("x"), 0)
                y = need_int(f"{cp}.y", c.get("y"), 0)
                w, h = self.estimate_size(c, cp, parent_w)
                child = self.build_widget(c, cp, x, y, w, h)
            else:
                w, h = self.estimate_size(c, cp, parent_w)
                if str(c.get("type", "label")) in ("container", "panel"):
                    w = need_int(f"{cp}.w", c.get("w"), parent_w)
                child = self.build_widget(c, cp, 0, cursor_y, w, h)
                cursor_y += h + gap
            if c.get("children") and child["type"] not in ("LVGLContainerWidget", "LVGLPanelWidget"):
                self.err(cp, f"{c.get('type')!r} does not support children (container/panel only)")
            out.append(child)
        return out

    def fill_children(self, obj: dict, node: dict, p: str) -> None:
        """Lay out and build node.children into obj['children']. 把 node.children 布局并构建，塞进 obj['children']。"""
        obj["children"].extend(self.place_children(node, p, obj["width"]))

    # ----- Pages 页面 -----

    def build_page(self, name: str, node: dict[str, Any], width: int, height: int,
                   is_user_widget: bool) -> dict:
        """Assemble a Page (a userPages/userWidgets entry).

        Regular page: root is an LVGLScreenWidget (a real screen); children hang below it.

        User widget page (official structure, verified in practice): **no root widget** —
        the page components are the flattened children (Page.lvglCreate's else branch
        iterates page-level components); coordinates are relative to the user widget
        itself. Do not add a ScreenWidget root (the preview path Screen.tsx calls
        createScreen unconditionally; nested under an instance it renders misplaced) or
        a Panel middle layer (same offset problem). A def-level bg is realized as a
        full-size background container as the first sibling; later components draw on
        top. User widget pages must set isUsedAsUserWidget: true explicitly.

        普通页：root 为 LVGLScreenWidget。user widget 页：没有根 widget，components
        平铺，坐标以 user widget 为基准；不要加 ScreenWidget 根或 Panel 中间层；
        def 级 bg 用全尺寸背景容器做第一个兄弟；必须 isUsedAsUserWidget:true。
        """
        p = f"{'widgets' if is_user_widget else 'screens'}[{name!r}]"

        if is_user_widget:
            if node.get("layout"):
                print(f"⚠ {p}: flex inside a user widget needs a container layer, which "
                      f"introduces a coordinate offset (verified in practice); layout "
                      f"ignored, use explicit x/y", file=sys.stderr)
            children_node = {"children": copy.deepcopy(node.get("children") or [])}
            comps: list[dict[str, Any]] = []
            if node.get("bg"):
                bg = self.build_widget({"type": "panel", "x": 0, "y": 0,
                                        "w": width, "h": height,
                                        "bg": node["bg"]}, f"{p}.bg", 0, 0, width, height)
                bg["clickableFlag"] = False
                comps.append(bg)
            comps.extend(self.place_children(children_node, p, width))

            # Page-level flow: a component event pin connects directly to an action chain
            # (handlerType=flow). Scoping rule (identifiers.ts): top-level actions only see
            # regular-page widgets; widgets inside a user widget page are visible only to
            # that page's own flow — internal component interactions must go through this.
            # 页面级 flow：组件事件引脚直连动作链；user widget 内部交互必须走这里。
            flow_lines: list[dict[str, Any]] = []

            def find_by_id(objs: list, ident: str) -> dict[str, Any] | None:
                full = self.id_map.get(ident, ident)   # when.id may use the short id when.id 可写简短 id
                for o in objs:
                    if o.get("identifier") == full:
                        return o
                    r = find_by_id(o.get("children", []), ident)
                    if r:
                        return r
                return None

            for fi, trigger in enumerate(node.get("flow") or []):
                tp = f"{p}.flow[{fi}]"
                when = trigger.get("when") or {}
                wid = need_str(f"{tp}.when.id", when.get("id"))
                evt = str(need_str(f"{tp}.when.event", when.get("event"), "clicked")).upper()
                target_widget = find_by_id(comps, wid)
                if target_widget is None:
                    self.err(f"{tp}.when.id", f"no component with id {wid!r} on this page")
                    continue
                target_widget["eventHandlers"].append({
                    "objID": oid(),
                    "eventName": evt,
                    "handlerType": "flow",
                })
                fcomps, flines = self.flow_nodes(
                    trigger.get("steps") or [], f"{tp}", 60 + fi * 100,
                    entry=(target_widget["objID"], evt))
                comps.extend(fcomps)
                flow_lines.extend(flines)

            page = {
                "objID": oid(),
                "components": comps,
                "connectionLines": flow_lines,
                "localVariables": [],
                "componentGroups": [],
                "userProperties": [],
                "name": name,
                "left": 0, "top": 0,
                "width": width, "height": height,
                "isUsedAsUserWidget": True,
            }
            return page

        root = self.base("LVGLScreenWidget", {}, p, 0, 0, width, height)
        root["clickableFlag"] = True
        root["widgetFlags"] = (
            "CLICKABLE|PRESS_LOCK|CLICK_FOCUSABLE|GESTURE_BUBBLE|SNAPPABLE|"
            "SCROLLABLE|SCROLL_ELASTIC|SCROLL_MOMENTUM|SCROLL_CHAIN_HOR|SCROLL_CHAIN_VER"
        )
        # Styles/layout on the root. 根上的样式/布局。
        root["localStyles"] = self.styles_for(node, p, use_default_font=False)
        if node.get("layout"):
            self.apply_flex(node, root)
            w, h = self.flex_autosize(node, p, width, height)
            root["width"], root["height"] = w, h
        # Top-level children: flex placeholders when layout is set; otherwise vertical stacking / explicit coords. 顶层 children：有 layout 用 flex 占位，否则竖向堆叠/显式坐标。
        children_node = {"children": copy.deepcopy(node.get("children") or []),
                         "layout": node.get("layout"), "gap": node.get("gap")}
        self.fill_children(root, children_node, p)
        page: dict[str, Any] = {
            "objID": oid(),
            "components": [root],
            "connectionLines": [],
            "localVariables": [],
            "componentGroups": [],
            "userProperties": [],
            "name": name,
            "left": 0, "top": 0,
            "width": width, "height": height,
        }
        if is_user_widget:
            page["isUsedAsUserWidget"] = True
        return page

    def compile(self) -> dict[str, Any]:
        # 1) user widget definition pages 定义页
        user_widgets = []
        for name, d in self.widget_defs.items():
            w = need_int(f"widgets[{name!r}].width", d.get("width"), 100)
            h = need_int(f"widgets[{name!r}].height", d.get("height"), 50)
            page = self.build_page(name, d, w, h, is_user_widget=True)
            user_widgets.append(page)

        # 2) screens 屏幕
        pages = []
        screen_names: set[str] = set()
        for s in self.ir.get("screens") or []:
            name = need_str("screens[].name", s.get("name"))
            if name in screen_names:
                self.err(f"screens[{name!r}]", "duplicate name")
            screen_names.add(name)
            page = self.build_page(name, s, self.sw, self.sh, is_user_widget=False)
            pages.append(page)

        # 3) actions (explicit flows + native stubs referenced by events). action（显式 flow + 事件引用的 native 空壳）。
        actions = []
        for a in self.actions_ir:
            name = need_str("actions[].name", a.get("name"))
            steps = a.get("steps")
            if steps:
                actions.append(self.build_flow_action(name, steps))
            else:
                actions.append({
                    "objID": oid(),
                    "components": [], "connectionLines": [], "localVariables": [],
                    "componentGroups": [], "userProperties": [],
                    "name": name, "implementationType": "native",
                })
                self.native_actions[name] = "VALUE_CHANGED" in self.action_event_kinds.get(name, set())
        for name in sorted(self.pending_actions - self.action_names):
            actions.append({
                "objID": oid(),
                "components": [], "connectionLines": [], "localVariables": [],
                "componentGroups": [], "userProperties": [],
                "name": name, "implementationType": "native",
            })
            self.native_actions.setdefault(name, False)
            print(f"⚠ action {name!r} referenced by an event is not defined; generated a native stub", file=sys.stderr)

        if self.errors:
            raise IRError("IR validation failed:\n  " + "\n  ".join(self.errors))

        return assemble_project(pages, user_widgets,
                                list(self.vars.vars.values()), actions,
                                self.sw, self.sh)

    # ----- flow action -----

    def flow_nodes(self, steps: list, p: str, top: int,
                   entry: tuple[dict[str, Any], str] | None = None
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """A linear steps sequence → a node chain (@seqout→@seqin) with auto layout.
        entry=(starting component, output pin name): page flows enter from an event pin;
        entry=None generates a Start node as the entry (for top-level actions).
        Returns (components, connectionLines).

        steps 线性序列 → 节点链（@seqout→@seqin），坐标自动布局；
        entry=None 时生成 Start 节点做入口。返回 (components, connectionLines)。
        """
        comps: list[dict[str, Any]] = []
        lines: list[dict[str, Any]] = []

        def fnode(wtype: str, extra: dict[str, Any], col_i: int) -> dict[str, Any]:
            return {
                "objID": oid(),
                "type": wtype,
                "left": 60 + col_i * 280,
                "top": top,
                "width": 100, "height": 40,   # purely visual; EEZ auto-sizes 纯视觉，EEZ 会 autoSize
                "customInputs": [], "customOutputs": [],
                "description": "",
                **extra,
            }

        def connect(src_objid: str, out: str, dst: dict, inp: str) -> None:
            lines.append({"objID": oid(), "source": src_objid, "output": out,
                          "target": dst["objID"], "input": inp})

        if entry is None:
            start = fnode("StartActionComponent", {}, 0)
            comps.append(start)
            prev_id, prev_out = start["objID"], "@seqout"
        else:
            prev_id, prev_out = entry

        for i, step in enumerate(steps):
            sp = f"{p}.steps[{i}]"
            if not isinstance(step, dict):
                self.err(sp, "step must be an object")
                continue
            op = need_str(f"{sp}.op", step.get("op"))
            if op == "lvgl":
                node = fnode("LVGLActionComponent",
                             {"actions": [self.lvgl_action_item(step, sp)]}, i + 1)
            elif op == "set":
                entries = [{
                    "objID": oid(),
                    "variable": need_str(f"{sp}.variable", step.get("variable")),
                    "value": need_str(f"{sp}.value", step.get("value")),
                }]
                node = fnode("SetVariableActionComponent", {"entries": entries}, i + 1)
            elif op == "delay":
                node = fnode("DelayActionComponent",
                             {"milliseconds": str(need_int(f"{sp}.ms", step.get("ms"), 100))}, i + 1)
            elif op == "call":
                target = need_str(f"{sp}.action", step.get("action"))
                node = fnode("CallActionActionComponent", {"action": target}, i + 1)
            else:
                self.err(sp, f"unknown op {op!r} (supported: lvgl/set/delay/call)")
                continue
            comps.append(node)
            connect(prev_id, prev_out, node, "@seqin")
            prev_id, prev_out = node["objID"], "@seqout"

        return comps, lines

    def build_flow_action(self, name: str, steps: list) -> dict[str, Any]:
        comps, lines = self.flow_nodes(steps, f"actions[{name!r}]", 60)
        return {
            "objID": oid(),
            "components": comps,
            "connectionLines": lines,
            "localVariables": [],
            "componentGroups": [],
            "userProperties": [],
            "name": name,
            "implementationType": "flow",
        }

    def lvgl_action_item(self, step: dict, p: str) -> dict[str, Any]:
        action = need_str(f"{p}.action", step.get("action"))
        if action in ("objAddState", "objClearState"):
            return self._lvgl_action_state_change(step, p)
        if action in ("objAddFlag", "objClearFlag"):
            return self._lvgl_action_state_change(step, p)  # same structure: object + flag (default HIDDEN) 结构相同：object + flag(默认 HIDDEN)
        if action == "labelSetText":
            return {
                "objID": oid(),
                "action": "labelSetText",
                "object": self._lvgl_action_target(step, p),
                "objectType": "literal",
                "text": need_str(f"{p}.text", step.get("text"), ""),
                "textType": "literal",
            }
        if action == "changeScreen":
            screen = need_str(f"{p}.screen", step.get("screen"))
            names = {s.get("name") for s in self.ir.get("screens") or []}
            if screen not in names:
                self.err(f"{p}.screen", f"references undefined screen {screen!r}")
            return {
                "objID": oid(),
                "action": "changeScreen",
                "screen": screen, "screenType": "literal",
                "fadeMode": str(step.get("fade", "FADE_IN")),
                "fadeModeType": "literal",
                "speed": need_int(f"{p}.speed", step.get("speed"), 200),
                "speedType": "literal",
                "delay": need_int(f"{p}.delay", step.get("delay"), 0),
                "delayType": "literal",
                "useStack": bool(step.get("useStack", False)),
                "useStackType": "literal",
            }
        if action == "objSetY":
            return {
                "objID": oid(),
                "action": "objSetY",
                "object": self._lvgl_action_target(step, p),
                "objectType": "literal",
                "y": need_int(f"{p}.y", step.get("y"), 0),
                "yType": "literal",
            }
        self.err(p, f"lvgl action {action!r} not yet supported")
        return {"objID": oid(), "action": action}

    def _lvgl_action_target(self, step: dict, p: str) -> str:
        """Validate and return the action target's full identifier: target may be an IR
        short id (auto-mapped to the prefixed full name) or a full identifier.

        校验并返回动作目标完整 identifier（简短 id 自动映射）。
        """
        target = need_str(f"{p}.target", step.get("target"))
        if target in self.id_map:
            return self.id_map[target]
        if target in self.known_ids:
            return target
        self.err(f"{p}.target", f"target identifier {target!r} does not exist (give the component an id first)")
        return target

    def _lvgl_action_state_change(self, step: dict, p: str) -> dict[str, Any]:
        """objAddState / objClearState (object + state, default CHECKED);
        objAddFlag / objClearFlag (object + flag, default HIDDEN, for partial view switching).

        objAddState/objClearState 默认 CHECKED；objAddFlag/objClearFlag 默认 HIDDEN。
        """
        action = need_str(f"{p}.action", step.get("action"))
        if "Flag" in action:
            key, default = "flag", "HIDDEN"
        else:
            key, default = "state", "CHECKED"
        item = {
            "objID": oid(),
            "action": action,
            "object": self._lvgl_action_target(step, p),
            "objectType": "literal",
        }
        item[key] = str(step.get(key, default)).upper()
        item[key + "Type"] = "literal"
        return item


# ---------- Project assembly 项目组装 ----------

def assemble_project(pages: list, user_widgets: list, variables: list,
                     actions: list, sw: int, sh: int) -> dict[str, Any]:
    return {
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
                "embedFonts": False,
                "cacheFonts": False,
            },
            "build": {
                "objID": oid(),
                "configurations": [{"objID": oid(), "name": "Default"}],
                "files": [{
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
                }],
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
        "variables": {"objID": oid(), "globalVariables": variables},
        "actions": actions,
        "userPages": pages,
        "userWidgets": user_widgets,
        "lvglStyles": {"objID": oid(), "styles": []},
        "lvglGroups": {"objID": oid(), "groups": []},
        "fonts": load_font_catalog(),
        "bitmaps": [],
        "colors": [],
        "themes": [{
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
        }],
    }


# ---------- Glyph coverage check 字形覆盖校验 ----------

def check_font_coverage(project: dict[str, Any], out: list[str]) -> None:
    """Every displayed text character must be in the target font's charset, or the
    device shows boxes. Charset = lvglSymbols from fonts/<name>.meta.json + icon-source symbols.

    所有会显示的字符必须在字体字符集里，否则设备上方块。
    字符集 = fonts/<名>.meta.json 的 lvglSymbols + 图标源 symbols。
    """
    from generator import FONTS_DIR

    charsets: dict[str, set[str]] = {}
    for f in project.get("fonts", []):
        meta_path = FONTS_DIR / f"{f['name']}.meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cs = set(meta.get("lvglSymbols", ""))
        for src in meta.get("iconSources", []):
            cs |= set(src.get("symbols", ""))
            # ranges look like "0xF048,0xF293-0xF294": parse into a codepoint set. ranges 解析成码点集合。
            for piece in str(src.get("ranges", "")).split(","):
                piece = piece.strip()
                if not piece:
                    continue
                try:
                    if "-" in piece:
                        lo, hi = (int(p, 16) for p in piece.split("-", 1))
                        cs |= {chr(c) for c in range(lo, hi + 1)}
                    else:
                        cs.add(chr(int(piece, 16)))
                except ValueError:
                    continue
        cs |= set(chr(c) for c in range(32, 128))
        charsets[f["name"]] = cs

    if not charsets:
        return

    default_font = next(iter(charsets))

    def check_text(text: str, font: str, path: str) -> None:
        font = font or default_font
        cs = charsets.get(font)
        if cs is None:
            return
        missing = sorted({ch for ch in text if ord(ch) > 32 and ch not in cs})
        if missing:
            out.append(f"{path}: font {font} is missing glyphs {''.join(missing)!r} "
                       f"(add this text source to the character scan when recompiling the font)")

    def walk(w: dict[str, Any], font: str, path: str) -> None:
        styles = w.get("localStyles", {}).get("definition", {})
        font = styles.get("MAIN", {}).get("DEFAULT", {}).get("text_font") or font
        if w.get("textType") != "expression" and isinstance(w.get("text"), str):
            check_text(w["text"], font, path)
        elif isinstance(w.get("previewValue"), str):
            check_text(w["previewValue"], font, path)
        opts = w.get("options")
        if isinstance(opts, str) and opts:
            check_text(opts.replace("\n", ""), font, path)
        for i, c in enumerate(w.get("children", [])):
            walk(c, font, f"{path}[{i}]")

    for pg in project["userPages"] + project["userWidgets"]:
        font = default_font
        for comp in pg["components"]:
            walk(comp, font, pg["name"])


# ---------- Output self-check 产物自检 ----------

def check_project(project: dict[str, Any]) -> list[str]:
    """Self-check of the compiled output: unique objIDs, existing line endpoints, valid userWidgetPageName refs. 编译产物结构自检：objID 唯一、连线两端存在、userWidgetPageName 引用有效。"""
    problems: list[str] = []
    ids: dict[str, str] = {}
    flow_nodes: dict[str, list[str]] = {}   # objID → available output pins objID → 可用输出引脚
    screens = {p["name"] for p in project["userPages"]}
    widgets = {w["name"] for w in project["userWidgets"]}
    actions = {a["name"] for a in project["actions"]}

    def walk(o: Any, path: str) -> None:
        if isinstance(o, dict):
            if "objID" in o and isinstance(o["objID"], str):
                if o["objID"] in ids and not o["objID"].startswith("objid-placeholder"):
                    problems.append(f"duplicate objID: {o['objID']} ({ids[o['objID']]} and {path})")
                ids[o["objID"]] = path
            for k, v in o.items():
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")

    walk(project, "$")

    for a in project["actions"]:
        for c in a.get("components", []):
            outs = ["@seqout"]
            if c["type"] == "LVGLActionComponent":
                continue
            flow_nodes[c["objID"]] = outs
        for ln in a.get("connectionLines", []):
            src, dst = ln["source"], ln["target"]
            if src not in ids:
                problems.append(f"action {a['name']}: line source {src} does not exist")
            if dst not in ids:
                problems.append(f"action {a['name']}: line target {dst} does not exist")
            if ln["input"] != "@seqin":
                problems.append(f"action {a['name']}: line input must be @seqin")

    def walk_widgets(children: list, page: str, page_lines: list) -> None:
        for c in children:
            if c["type"] == "LVGLUserWidgetWidget":
                if c.get("userWidgetPageName") not in widgets:
                    problems.append(f"{page}: user widget instance references undefined {c.get('userWidgetPageName')!r}")
            for h in c.get("eventHandlers", []):
                if h.get("handlerType") == "flow":
                    # Page flow: a line must start from this component's event pin. 页面 flow：必须存在从事件引脚出发的连线。
                    has_line = any(ln.get("source") == c["objID"] and ln.get("output") == h["eventName"]
                                   for ln in page_lines)
                    if not has_line:
                        problems.append(f"{page}: {c.get('identifier', '?')} {h['eventName']} "
                                        f"flow handler is missing a connection line")
                elif h.get("action") not in actions:
                    problems.append(f"{page}: event references undefined action {h.get('action')!r}")
            walk_widgets(c.get("children", []), page, page_lines)

    for p in project["userPages"] + project["userWidgets"]:
        lines = p.get("connectionLines", [])
        for comp in p["components"]:
            walk_widgets([comp], p["name"], lines)
            for h in comp.get("eventHandlers", []):
                if h.get("handlerType") == "flow":
                    if not any(ln.get("source") == comp["objID"] and ln.get("output") == h["eventName"]
                               for ln in lines):
                        problems.append(f"{p['name']}: {comp.get('identifier', '?')} "
                                        f"{h['eventName']} flow handler is missing a connection line")
                elif h.get("action") not in actions:
                    problems.append(f"{p['name']}: event references undefined action {h.get('action')!r}")

    for a in project["actions"]:
        for c in a.get("components", []):
            if c["type"] == "LVGLActionComponent":
                for item in c.get("actions", []):
                    if item["action"] == "changeScreen" and item["screen"] not in screens:
                        problems.append(f"action {a['name']}: changeScreen target {item['screen']!r} is undefined")
    return problems


# ---------- Main entry 主入口 ----------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="IR(JSON) → EEZ Studio .eez-project (LVGL)")
    ap.add_argument("input", help="path to the IR JSON file")
    ap.add_argument("-o", "--output", default="out_ir.eez-project", help="output file")
    args = ap.parse_args(argv)

    with open(args.input, "r", encoding="utf-8") as f:
        ir = json.load(f)
    if not isinstance(ir, dict):
        print("IR root must be a JSON object", file=sys.stderr)
        return 1

    try:
        compiler = Compiler(ir)
        project = compiler.compile()
    except IRError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    problems = check_project(project)
    check_font_coverage(project, problems)
    if problems:
        print("✗ Output self-check found problems:", file=sys.stderr)
        for pb in problems:
            print(f"  - {pb}", file=sys.stderr)
        return 1

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(project, f, ensure_ascii=False, indent=2)

    # action.h: the native action list; firmware includes it and implements these callbacks (porting interface). action.h：native 动作清单，固件实现这些回调。
    if compiler.native_actions:
        import os
        h_path = os.path.join(
            os.path.dirname(os.path.abspath(args.output)) or ".", "action.h"
        )
        lines = [
            "// action.h — auto-generated by ir2eez (do not edit)",
            f"// Native action list ({len(compiler.native_actions)}): value-change kinds take a value param",
            "//   slider/arc -> current value; switch -> 0/1; dropdown -> option index; click kinds take no param",
            "#pragma once",
            "#include <stdint.h>",
            "",
            "#ifdef __cplusplus",
            'extern "C" {',
            "#endif",
            "",
        ]
        for name in sorted(compiler.native_actions):
            sig = "(int32_t value)" if compiler.native_actions[name] else "(void)"
            lines.append(f"void {name}{sig};")
        lines += ["", "#ifdef __cplusplus", "}", "#endif", ""]
        with open(h_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
        print(f"action.h → {h_path} ({len(compiler.native_actions)} native actions)")

    n_widgets = sum(len(p["components"][0].get("children", []))
                    for p in project["userPages"] + project["userWidgets"])
    print(f"✓ Generated {args.output}")
    print(f"  Screens:     {[p['name'] for p in project['userPages']]}")
    print(f"  user widget: {[w['name'] for w in project['userWidgets']]}")
    print(f"  Variables:   {len(project['variables']['globalVariables'])}")
    for v in project["variables"]["globalVariables"]:
        print(f"     - {v['name']:16s} {v['type']:8s} default={v['defaultValue']}"
              + (" [native]" if v["native"] else ""))
    print(f"  Actions:     {len(project['actions'])}")
    for a in project["actions"]:
        n = len(a.get("components", []))
        kind = f"flow({n} nodes)" if a["implementationType"] == "flow" else "native"
        print(f"     - {a['name']:24s} {kind}")
    print(f"  Top-level widgets: {n_widgets}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
