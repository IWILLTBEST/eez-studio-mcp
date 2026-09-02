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
import os
import re
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


def yaml_str(s: str) -> str:
    """Plain YAML scalar when safe, double-quoted otherwise (lv_i18n translations).
    安全时输出裸标量，否则加双引号（lv_i18n 译文表）。"""
    if s and not any(c in s for c in ":#'\"\n\t") and not s[0] in " -?*&!|>%@`" and not s.endswith(" "):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
    "roller": (100, 100),
    "table": (240, 120),
    "chart": (260, 150),
    "scale": (200, 160),
    "calendar": (230, 240),
    "keyboard": (300, 120),
    "spinbox": (180, 60),
    "tabview": (320, 220),
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
    # roller selected is assignable: the flow emits rollerSetSelected when the
    # variable changes and the widget writes the variable on VALUE_CHANGED.
    # roller 的 selected 可写：变量变化→rollerSetSelected，VALUE_CHANGED→写变量。
    "roller": ("selected", "integer", "0"),
    "spinbox": ("value", "integer", "0"),
    # tabview selectedTab is assignable (tabviewSetActiveTab) and VALUE_CHANGED
    # fires on tab switch — same two-way pattern as roller/spinbox.
    "tabview": ("selectedTab", "integer", "0"),
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
    "LVGLRollerWidget": "roller_",
    "LVGLTableWidget": "table_",
    "LVGLChartWidget": "chart_",
    "LVGLScaleWidget": "scale_",
    "LVGLCalendarWidget": "calendar_",
    "LVGLKeyboardWidget": "keyboard_",
    "LVGLSpinboxWidget": "spinbox_",
    "LVGLTabviewWidget": "tabview_",
    "LVGLTabWidget": "tab_",
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
            # accept common aliases so a mistyped field doesn't silently zero
            # the default (design-time preview + simulator show variable
            # defaults). 别名兼容：字段名写错不该静默变成 0。
            default = v.get("default")
            if default is None:
                default = v.get("value")
            if default is None:
                default = v.get("init")
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
        # i18n: IR "strings" section. strings = {"default": "en", "texts": {key: {lang: text}}}
        # tr:"key" labels compile to T"key" expressions (upstream #1045 → lv_i18n);
        # translations.yaml (lv_i18n format) is written next to the output.
        # i18n 字符串表：tr 引用的 key 编译成 T"key" 表达式，译文表落 translations.yaml。
        strings = ir.get("strings") or {}
        if not isinstance(strings, dict):
            fail("strings", "expected object")
        self.strings_default = need_str("strings.default", strings.get("default"), "en")
        self.strings: dict[str, dict[str, str]] = {}
        for key, langs in (strings.get("texts") or {}).items():
            if not isinstance(langs, dict) or not langs:
                fail(f"strings.texts[{key!r}]", "expected object with at least one language")
            self.strings[str(key)] = {str(l): str(t) for l, t in langs.items()}
        self.tr_keys: set[str] = set()
        # Rich widgets (chart/table/roller structure) for ui_ext.h generation.
        # 富数据部件的结构参数，落 ui_ext.h 给固件当命名常量。
        self.rich_widgets: list[dict[str, Any]] = []
        self.default_font = need_str("project.font", proj.get("font"), "")
        # Font whitelist: catalog fonts + montserrat built-ins. A name outside
        # both fails the build (a private-chain font leaking into a public
        # example used to ship silently and Studio flagged it as check errors).
        # 字体白名单：目录字体 + montserrat 内建；越界即报错。
        self.errors: list[str] = []
        self.known_fonts = {f.get("name", "") for f in load_font_catalog()}
        if self.default_font and (self.default_font not in self.known_fonts
                                  and not self.default_font.upper().startswith("MONTSERRAT")):
            self.err("project.font",
                     f"project font {self.default_font!r} not in the font catalog "
                     f"(available: {sorted(self.known_fonts)}) nor a montserrat built-in")

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
            if (font not in self.known_fonts
                    and not font.upper().startswith("MONTSERRAT")):
                self.err(f"{path}.font",
                         f"font {font!r} not in the font catalog (available: "
                         f"{sorted(self.known_fonts)}) nor a montserrat built-in")
            props["text_font"] = font
        if node.get("color"):
            props["text_color"] = normalize_color(str(node["color"]))
        if node.get("bg"):
            props["bg_color"] = normalize_color(str(node["bg"]))
        if node.get("radius") is not None:
            props["radius"] = need_int(f"{path}.radius", node.get("radius"), 0)
        if node.get("bgOpa") is not None:
            props["bg_opa"] = need_int(f"{path}.bgOpa", node.get("bgOpa"), 255)
        # align: text alignment inside the widget box — CENTER is what makes a
        # gauge value sit on the arc's axis instead of hugging the box's left
        # edge (box center == arc center is not enough with LEFT align).
        # align：盒内文本对齐——仪表盘数值要居中必须 CENTER，盒子居中不够。
        if node.get("align") is not None:
            a = str(node["align"]).upper()
            if a not in ("LEFT", "CENTER", "RIGHT", "AUTO"):
                self.err(f"{path}.align", f"expected left/center/right/auto, got {node['align']!r}")
            else:
                props["text_align"] = a
        # lv: raw LVGL style props passthrough (shadow_*, border_*, bg_grad_*,
        # text_opa, pad_* ...). Full catalog: EEZ style-catalog / LVGL docs.
        # lv：LVGL 样式属性透传（阴影/边框/渐变/文字透明度/内边距等全目录）。
        if isinstance(node.get("lv"), dict):
            for k, v in node["lv"].items():
                if not isinstance(k, str) or not k.replace("_", "").isalnum():
                    self.err(f"{path}.lv[{k!r}]", "invalid style property name")
                    continue
                props[k] = v
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

    def _preview(self, expr: str) -> str:
        """Design-time preview: EEZ renders previewValue on the canvas, NOT the
        expression (that one only evaluates at runtime). Substitute variable
        defaults and safe-eval arithmetic/concatenation; on anything we can't
        resolve (unknown identifiers, function calls) fall back to the raw
        expression text. 设计时画布渲染的是 previewValue：用变量默认值求值，求不动就原样回退。"""
        expr = expr.strip()
        if not expr:
            return ""

        # T"key" → localized default-language text (canvas preview); missing key → key
        m = re.fullmatch(r'T"([^"]*)"', expr)
        if m:
            return self.strings.get(m.group(1), {}).get(self.strings_default, m.group(1))

        def fmt(v: Any) -> str:
            if v is True:
                return "true"
            if v is False:
                return "false"
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            return str(v)

        # bare variable name → its default (numbers/bools pass through,
        # quoted strings get unquoted for display)
        if expr in self.vars.vars:
            d = self.vars.vars[expr]["defaultValue"]
            if len(d) >= 2 and d[0] == '"' and d[-1] == '"':
                return json.loads(d)
            return d

        ids = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr))
        if not ids or ids - set(self.vars.vars) - {"true", "false"}:
            return expr
        sub = re.sub(
            r"[A-Za-z_][A-Za-z0-9_]*",
            lambda m: (self.vars.vars[m.group(0)]["defaultValue"]
                       if m.group(0) in self.vars.vars else
                       ("True" if m.group(0) == "true" else "False")),
            expr,
        )
        # After the identifier gate only numbers, operators, parens and quoted
        # string contents remain, so attribute access / names are impossible —
        # eval() here is arithmetic + concatenation only. 标识符门禁后只剩数字/
        # 运算符/括号/字符串字面量，eval 只可能做算术和拼接。
        try:
            return fmt(eval(sub, {"__builtins__": {}}, {}))  # noqa: S307
        except Exception:
            return expr

    def _build_label(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLLabelWidget", n, p, x, y, w, h)
        obj["localStyles"] = self.styles_for(n, p)
        tr = n.get("tr")
        if tr is not None:
            # i18n label: text = T"key" expression (lvgl i18n via upstream #1045);
            # the canvas renders previewValue → localized default-language text.
            # i18n 标签：表达式 T"key"，画布渲染 previewValue=默认语言译文。
            key = need_str(f"{p}.tr", tr)
            if key not in self.strings:
                self.err(f"{p}.tr", f"key {key!r} missing in strings.texts (preview falls back to the key)")
            self.tr_keys.add(key)
            obj["text"], obj["textType"] = f'T"{key}"', "expression"
            preview = str(n.get("preview") or self.strings.get(key, {}).get(self.strings_default, key))
            obj["previewValue"] = preview
            text = preview
        else:
            bind = self._bind(n, p, "label")
            if bind:
                obj["text"], obj["textType"] = bind[1], "expression"
                preview = str(n.get("preview") or self._preview(bind[1]))
                obj["previewValue"] = preview
                text = preview
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
            obj["previewValue"] = str(n.get("preview") or self._preview(bind[1]))
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
            obj["previewValue"] = str(n.get("preview") or self._preview(bind[1]))
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
        # CHECKABLE is what makes tap-to-toggle work: without it the checkbox
        # presses (ripple) but never flips and VALUE_CHANGED never fires.
        # DEFAULT_FLAGS lacks it (TrailCurrent eezstudio skill "Trap 21").
        # CHECKABLE 是点击翻转的前提：缺它时有按压反馈但状态永不翻转。
        obj["widgetFlags"] = ("CHECKABLE|CLICKABLE|CLICK_FOCUSABLE|PRESS_LOCK|"
                              "GESTURE_BUBBLE|SNAPPABLE")
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
            obj["previewValue"] = str(n.get("preview") or self._preview(bind[1]))
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

    def _build_roller(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLRollerWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        obj["widgetFlags"] = ("CLICKABLE|CLICK_FOCUSABLE|GESTURE_BUBBLE|PRESS_LOCK|"
                              "SCROLL_CHAIN_HOR|SCROLL_ELASTIC|SCROLL_MOMENTUM|"
                              "SCROLL_WITH_ARROW|SNAPPABLE")
        opts = n.get("options")
        if isinstance(opts, list):
            if not opts or not all(isinstance(o, str) and o for o in opts):
                self.err(f"{p}.options", "expected a non-empty list of strings")
                opts = ["Option 1", "Option 2"]
            options = "\n".join(opts)
        elif isinstance(opts, str) and opts:
            options = opts          # raw "\n"-separated string also accepted
        else:
            self.err(f"{p}.options", "expected a list of options or a \\n-separated string")
            options = "Option 1\nOption 2"
        mode = str(n.get("mode", "normal")).upper()
        if mode not in ("NORMAL", "INFINITE"):
            self.err(f"{p}.mode", f"must be normal or infinite, got {n.get('mode')!r}")
            mode = "NORMAL"
        obj["options"], obj["optionsType"] = options, "literal"
        bind = self._bind(n, p, "roller")
        if bind:
            obj["selected"], obj["selectedType"] = bind[1], "expression"
        else:
            obj["selected"] = need_int(f"{p}.selected", n.get("selected"), 0)
            obj["selectedType"] = "literal"
        obj["mode"] = mode
        # Width guard: the roller shows one option at a time — fit the longest
        # one plus wheel padding. 宽度兜底：按最长选项估宽（滚轮一次只显示一项）。
        font = str(n.get("font") or self.default_font or "x_16")
        longest = max(options.split("\n"), key=len)
        need_w = estimate_text_width(longest, font_size_of(font)) + 44
        if obj["width"] < need_w:
            obj["width"] = need_w
        self.rich_widgets.append({"kind": "roller", "id": obj.get("identifier", ""),
                                  "options": options.split("\n")})
        return obj

    def _build_table(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        """EEZ compiles lv_table_create only — structure (col/row count, cell text)
        is runtime C. cols/rows/header are validated here and exported to ui_ext.h
        as named constants + a ready-to-loop header array; they do NOT change the
        .eez-project. EEZ 只编译 lv_table_create：结构是运行时 C 的事，cols/rows/header
        进 ui_ext.h 命名常量（不进工程文件）。"""
        obj = self.base("LVGLTableWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        obj["widgetFlags"] = ("CLICKABLE|CLICK_FOCUSABLE|GESTURE_BUBBLE|PRESS_LOCK|"
                              "SCROLLABLE|SCROLL_CHAIN_HOR|SCROLL_CHAIN_VER|"
                              "SCROLL_ELASTIC|SCROLL_MOMENTUM|SCROLL_WITH_ARROW|"
                              "SNAPPABLE")
        cols = need_int(f"{p}.cols", n.get("cols"), 3)
        rows = need_int(f"{p}.rows", n.get("rows"), 4)
        if not (1 <= cols <= 32 and 1 <= rows <= 256):
            self.err(f"{p}", f"cols/rows out of range (1..32 x 1..256), got {cols}x{rows}")
        header = n.get("header")
        if header is not None:
            if (not isinstance(header, list) or not header
                    or not all(isinstance(s, str) for s in header)):
                self.err(f"{p}.header", "expected a list of strings")
                header = None
            elif len(header) > cols:
                self.err(f"{p}.header", f"{len(header)} header cells exceed cols={cols}")
                header = header[:cols]
        self.rich_widgets.append({"kind": "table", "id": obj.get("identifier", ""),
                                  "cols": cols, "rows": rows, "header": header or []})
        return obj

    def _build_chart(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        """Same story as table: lv_chart_create only; series/ranges are runtime C.
        IR kind/min/max/points/series are validated and exported to ui_ext.h.
        与 table 同理：序列/量程是运行时 C，IR 参数进 ui_ext.h。"""
        obj = self.base("LVGLChartWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        obj["widgetFlags"] = ("CLICKABLE|CLICK_FOCUSABLE|GESTURE_BUBBLE|PRESS_LOCK|"
                              "SCROLLABLE|SCROLL_CHAIN_HOR|SCROLL_CHAIN_VER|"
                              "SCROLL_ELASTIC|SCROLL_MOMENTUM|SCROLL_WITH_ARROW|"
                              "SNAPPABLE")
        kind = str(n.get("kind", "line")).lower()
        if kind not in ("line", "scatter"):
            self.err(f"{p}.kind", f"must be line or scatter, got {n.get('kind')!r}")
            kind = "line"
        vmin = need_int(f"{p}.min", n.get("min"), 0)
        vmax = need_int(f"{p}.max", n.get("max"), 100)
        if vmin >= vmax:
            self.err(f"{p}", f"min must be < max, got {vmin}..{vmax}")
        points = need_int(f"{p}.points", n.get("points"), 120)
        if not (2 <= points <= 4096):
            self.err(f"{p}.points", f"must be 2..4096, got {points}")
            points = 120
        series = n.get("series")
        names: list[dict[str, Any]] = []
        if series is not None:
            if not isinstance(series, list) or not series:
                self.err(f"{p}.series", "expected a non-empty list of {name, color, width}")
                series = None
            else:
                for i, s in enumerate(series):
                    if not isinstance(s, dict) or not s.get("name"):
                        self.err(f"{p}.series[{i}]", "each series needs a name")
                        continue
                    color = normalize_color(str(s.get("color", "#5EE6C4")))
                    names.append({"name": str(s["name"]), "color": color,
                                  "width": need_int(f"{p}.series[{i}].width", s.get("width"), 2)})
        self.rich_widgets.append({"kind": "chart", "id": obj.get("identifier", ""),
                                  "chart_kind": kind, "min": vmin, "max": vmax,
                                  "points": points, "series": names})
        return obj

    _SCALE_MODES = ("HORIZONTAL_TOP", "HORIZONTAL_BOTTOM", "VERTICAL_LEFT",
                    "VERTICAL_RIGHT", "ROUND_INNER", "ROUND_OUTER")
    _KB_MODES = ("TEXT_LOWER", "TEXT_UPPER", "SPECIAL", "NUMBER",
                 "USER_1", "USER_2", "USER_3", "USER_4")

    def _build_scale(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        """LVGL 9 lv_scale — fully compiled (mode/range/angle/ticks/labels/sections).
        The LVGL 8 lv_meter equivalent is palette-disabled in 9.x projects.
        lv_scale 完整编译（8.x 的 meter 在 9.x 工程里被禁用，scale 是正主）。"""
        obj = self.base("LVGLScaleWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        mode = str(n.get("mode", "round_inner")).upper()
        if mode not in self._SCALE_MODES:
            self.err(f"{p}.mode", f"must be one of {[m.lower() for m in self._SCALE_MODES]}, got {n.get('mode')!r}")
            mode = "ROUND_INNER"
        obj["scaleMode"] = mode
        vmin = need_int(f"{p}.min", n.get("min"), 0)
        vmax = need_int(f"{p}.max", n.get("max"), 100)
        if vmin >= vmax:
            self.err(f"{p}", f"min must be < max, got {vmin}..{vmax}")
        angle = need_int(f"{p}.angle", n.get("angle"), 270)
        if not (0 <= angle <= 360):
            self.err(f"{p}.angle", f"must be 0..360, got {angle}")
            angle = 270
        obj["minValue"], obj["minValueType"] = vmin, "literal"
        obj["maxValue"], obj["maxValueType"] = vmax, "literal"
        obj["angleRange"] = angle
        obj["rotation"], obj["rotationType"] = need_int(f"{p}.rotate", n.get("rotate"), 135), "literal"
        obj["totalTickCount"] = need_int(f"{p}.ticks", n.get("ticks"), 11)
        obj["majorTickEvery"] = need_int(f"{p}.major", n.get("major"), 5)
        obj["showLabels"] = bool(n.get("labels", True))
        obj["labelTexts"] = str(n.get("labelTexts", ""))
        obj["postDraw"] = False
        obj["drawTicksOnTop"] = False
        sections = []
        for i, s in enumerate(n.get("sections") or []):
            if not isinstance(s, dict):
                self.err(f"{p}.sections[{i}]", "expected object {from, to, color, width}")
                continue
            smin = need_int(f"{p}.sections[{i}].from", s.get("from"), vmin)
            smax = need_int(f"{p}.sections[{i}].to", s.get("to"), vmax)
            if smin >= smax:
                self.err(f"{p}.sections[{i}]", f"from must be < to, got {smin}..{smax}")
            sections.append({
                "objID": oid(),
                "minValue": smin, "minValueType": "literal",
                "maxValue": smax, "maxValueType": "literal",
                "useStyle": "",
                "mainColor": normalize_color(str(s.get("color", "#E5484D"))),
                "mainWidth": need_int(f"{p}.sections[{i}].width", s.get("width"), 6),
            })
        obj["sections"] = sections
        obj["localStyles"] = self.styles_for(n, p)
        return obj

    def _build_calendar(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLCalendarWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        # Scroll flags OFF: a fixed-size calendar must not scroll — EEZ's
        # default SCROLLABLE makes the canvas render the header at a slightly
        # different scroll offset on every reload (golden-test flakiness) and
        # lets users fling the month view around in the simulator.
        # 固定尺寸日历禁滚动：默认 SCROLLABLE 会让画布每次重载渲染出
        # 微小滚动偏移（金标准抖动的根源），模拟器里还会被拖着滚。
        obj["widgetFlags"] = "CLICKABLE|CLICK_FOCUSABLE|GESTURE_BUBBLE|PRESS_LOCK"
        # Height guard: months spanning 6 calendar weeks (31 days starting on
        # a Saturday etc.) overflow a shorter calendar — the internal date
        # button-matrix child then scrolls on its own and parent flags cannot
        # stop it (user-observed: some months scroll, others don't).
        # 240 = header + weekday row + 6 rows at default fonts (EEZ default).
        # 高度兜底：跨 6 周的月份撑爆过矮的日历，内部日期矩阵子对象会自行
        # 滚动（父对象标志管不住，表现为"有的月份滚有的不滚"）。
        if obj["height"] < 240:
            print(f"⚠ {p}: calendar height {obj['height']} < 240 — a 6-week month "
                  f"overflows and the date grid scrolls; grown to 240", file=sys.stderr)
            obj["height"] = 240
        today = str(n.get("today", "2026-01-01"))
        try:
            yy, mm, dd = (int(v) for v in today.split("-"))
            if not (1 <= mm <= 12 and 1 <= dd <= 31):
                raise ValueError
        except ValueError:
            self.err(f"{p}.today", f"expected YYYY-MM-DD, got {today!r}")
            yy, mm, dd = 2026, 1, 1
        obj["todayYear"], obj["todayMonth"], obj["todayDay"] = yy, mm, dd
        header = str(n.get("header", "arrow")).capitalize()
        if header not in ("None", "Arrow", "Dropdown"):
            self.err(f"{p}.header", f"must be none/arrow/dropdown, got {n.get('header')!r}")
            header = "Arrow"
        obj["header"] = header
        obj["chineseMode"] = bool(n.get("chinese", False))
        return obj

    def _build_keyboard(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLKeyboardWidget", n, p, x, y, w, h)
        ta = need_str(f"{p}.textarea", n.get("textarea"), "")
        if ta:
            target = self.id_map.get(ta, ta)
            if target not in self.known_ids or not target.startswith("textarea_"):
                self.err(f"{p}.textarea",
                         f"{ta!r} is not a textarea identifier (give the textarea an id first)")
                ta = ""
            else:
                ta = target
        obj["textarea"] = ta
        mode = str(n.get("mode", "text_lower")).upper()
        if mode not in self._KB_MODES:
            self.err(f"{p}.mode", f"must be one of {[m.lower() for m in self._KB_MODES]}, got {n.get('mode')!r}")
            mode = "TEXT_LOWER"
        obj["mode"] = mode
        return obj

    def _build_spinbox(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        obj = self.base("LVGLSpinboxWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        vmin = need_int(f"{p}.min", n.get("min"), -99999)
        vmax = need_int(f"{p}.max", n.get("max"), 99999)
        if vmin >= vmax:
            self.err(f"{p}", f"min must be < max, got {vmin}..{vmax}")
        obj["min"], obj["minType"] = vmin, "literal"
        obj["max"], obj["maxType"] = vmax, "literal"
        obj["digitCount"] = need_int(f"{p}.digits", n.get("digits"), 5)
        obj["separatorPosition"] = need_int(f"{p}.separator", n.get("separator"), 0)
        obj["rollover"] = bool(n.get("rollover", False))
        obj["step"], obj["stepType"] = need_int(f"{p}.step", n.get("step"), 1), "literal"
        bind = self._bind(n, p, "spinbox")
        if bind:
            obj["value"], obj["valueType"] = bind[1], "expression"
        else:
            obj["value"] = need_int(f"{p}.value", n.get("value"), 0)
            obj["valueType"] = "literal"
        return obj

    def _build_tabview(self, n: dict, p: str, x: int, y: int, w: int, h: int) -> dict:
        """EEZ models tabview fully: tabs are LVGLTabWidget children (own title +
        content children), selectedTab is bindable (tabviewSetActiveTab). The one
        container besides panel that takes children. EEZ 对 tabview 建模完整：
        tabs 作为子组件（各自标题+内容），selectedTab 可绑定。"""
        obj = self.base("LVGLTabviewWidget", n, p, x, y, w, h)
        obj["clickableFlag"] = True
        position = str(n.get("position", "top")).upper()
        if position not in ("TOP", "BOTTOM", "LEFT", "RIGHT"):
            self.err(f"{p}.position", f"must be top/bottom/left/right, got {n.get('position')!r}")
            position = "TOP"
        bar = need_int(f"{p}.barSize", n.get("barSize"), 40)
        obj["tabviewPosition"] = position
        obj["tabviewSize"] = bar
        bind = self._bind(n, p, "tabview")
        if bind:
            obj["selectedTab"], obj["selectedTabType"] = bind[1], "expression"
        else:
            obj["selectedTab"] = need_int(f"{p}.selected", n.get("selected"), 0)
            obj["selectedTabType"] = "literal"
        # tab content area: bar eats one axis. 标签栏占掉一轴。
        cw = w - bar if position in ("LEFT", "RIGHT") else w
        ch = h if position in ("LEFT", "RIGHT") else h - bar
        tabs = n.get("tabs") or []
        if not tabs:
            self.err(f"{p}.tabs", "tabview needs at least one tab {title, children}")
        for i, t in enumerate(tabs):
            tp = f"{p}.tabs[{i}]"
            if not isinstance(t, dict):
                self.err(tp, "expected object {title, children}")
                continue
            title = need_str(f"{tp}.title", t.get("title"), f"Tab {i + 1}")
            tab = self.base("LVGLTabWidget", {"type": "tab"}, tp, 0, 0, cw, ch)
            tab["clickableFlag"] = True
            tab["tabName"], tab["tabNameType"] = title, "literal"
            # font for the tab title follows the tabview's font context so
            # widths/fonts stay consistent. 标题字体随 tabview 上下文。
            self.fill_children(tab, t, tp)
            obj["children"].append(tab)
        return obj

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
            flow_comps, flow_lines = self._page_flow(node, p, comps)
            comps.extend(flow_comps)

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
        # Regular pages support page-level flows too — same wiring as user
        # widgets (historical gap: only the user-widget branch wired them).
        # 普通页同样接页面级 flow（历史缺口：原来只有 user-widget 分支接线）。
        flow_comps, flow_lines = self._page_flow(node, p, [root])
        page: dict[str, Any] = {
            "objID": oid(),
            "components": [root] + flow_comps,
            "connectionLines": flow_lines,
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

    def _page_flow(self, node: dict[str, Any], p: str,
                   roots: list) -> tuple[list, list]:
        """Wire page-level triggers (node.flow): each wires a widget event pin
        (handlerType=flow) to a generated step chain. Returns (flow components,
        connection lines) for the caller to splice into the page.
        页面级 trigger 接线：事件引脚 → 步骤链组件 + 连线。"""
        comps: list[dict[str, Any]] = []
        lines: list[dict[str, Any]] = []
        if not (node.get("flow") or []):
            return comps, lines

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
            target_widget = find_by_id(roots, wid)
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
            lines.extend(flines)
        return comps, lines

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

        # texts section (upstream #1045): the flow engine resolves T"key" at
        # runtime from the assets languages table — without this the firmware
        # and simulator render empty strings / fail evaluation. Default
        # language first: g_selectedLanguage starts at 0.
        # texts 段：固件/模拟器运行时翻译的正源，默认语言必须排第一。
        texts: dict[str, Any] | None = None
        if self.strings:
            lang_order = [self.strings_default] + sorted(
                l for key in self.strings for l in self.strings[key]
                if l != self.strings_default)
            texts = {
                "languages": [{"languageID": l} for l in lang_order],
                "resources": [
                    {"resourceID": key,
                     "translations": [
                         {"languageID": lang, "text": self.strings[key].get(lang, "")}
                         for lang in lang_order]}
                    for key in sorted(self.strings)],
            }

        return assemble_project(pages, user_widgets,
                                list(self.vars.vars.values()), actions,
                                self.sw, self.sh, texts)

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
        if action == "anim":
            # Property animation → one of the seven EEZ anim* actions.
            # repeat: 0 = play once, -1 = infinite (maps to repeatCount,
            # wired to lv_anim_set_repeat_count in the exported framework).
            # 属性动画 → 七个 anim* 动作之一。repeat 0=播一次 -1=无限循环。
            anim_props = {
                "x": "animX", "y": "animY",
                "w": "animWidth", "width": "animWidth",
                "h": "animHeight", "height": "animHeight",
                "opacity": "animOpacity",
                "img_zoom": "animImageZoom", "img_angle": "animImageAngle",
            }
            anim_eases = {
                "linear": "LINEAR", "ease_in": "EASE_IN",
                "ease_out": "EASE_OUT", "ease_in_out": "EASE_IN_OUT",
                "overshoot": "OVERSHOOT", "bounce": "BOUNCE",
            }
            prop = need_str(f"{p}.prop", step.get("prop"))
            if prop not in anim_props:
                self.err(f"{p}.prop", f"must be one of {sorted(anim_props)}, got {prop!r}")
                prop = "x"
            ease = str(step.get("ease", "ease_in_out")).lower()
            if ease.upper() not in anim_eases.values():
                if ease not in anim_eases:
                    self.err(f"{p}.ease", f"must be one of {sorted(anim_eases)}, got {ease!r}")
                ease = "ease_in_out"
            repeat = need_int(f"{p}.repeat", step.get("repeat"), 0)
            if repeat < -1:
                self.err(f"{p}.repeat", "must be >= -1 (-1 = infinite)")
                repeat = 0
            playback = bool(step.get("playback", False))
            return {
                "objID": oid(),
                "action": anim_props[prop],
                "object": self._lvgl_action_target(step, p),
                "objectType": "literal",
                "start": need_int(f"{p}.from", step.get("from"), 0),
                "startType": "literal",
                "end": need_int(f"{p}.to", step.get("to"), 100),
                "endType": "literal",
                "delay": need_int(f"{p}.delay", step.get("delay"), 0),
                "delayType": "literal",
                "time": need_int(f"{p}.time", step.get("time"), 400),
                "timeType": "literal",
                "relative": bool(step.get("relative", False)),
                "relativeType": "literal",
                "instant": bool(step.get("instant", True)),
                "instantType": "literal",
                "path": anim_eases.get(ease, ease.upper()),
                "pathType": "literal",
                "repeatCount": repeat,
                "repeatCountType": "literal",
                "playback": playback,
                "playbackType": "literal",
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
                     actions: list, sw: int, sh: int,
                     texts: dict[str, Any] | None = None) -> dict[str, Any]:
    project: dict[str, Any] = {
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
                # Full upstream build-file template set (screens.c carries
                # LVGL_SCREENS_DEF → create_screens() with every widget
                # creation call; flow_def.c the assets + native vars table;
                # actions.h the flow handlers; screens.c also gets the i18n
                # T"key" listing from upstream #1045). Data lives in
                # lvgl-build-files.json, extracted from eez-open/
                # eez-project-templates "LVGL with EEZ Flow-9.0".
                # 完整上游构建模板：screens.c 生成 create_screens()（含全部部件创建），
                # 裸机固件链接不再缺符号；模板数据抽自上游官方模板仓。
                "files": [{
                    "objID": oid(),
                    "fileName": f["fileName"],
                    "template": f["template"],
                } for f in json.load(open(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "lvgl-build-files.json"), encoding="utf-8"))],
                "destinationFolder": ".",
                "separateFolderForImagesAndFonts": False,
                "imageExportMode": "source",
                "fontExportMode": "source",
                "lvglInclude": "lvgl/lvgl.h",
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
    if texts:
        texts["objID"] = oid()
        project["texts"] = texts
    return project


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

def compiler_meta(compiler: "Compiler") -> dict[str, Any]:
    """Side-car data the .eez-project cannot carry but the reverse importer
    (eez2ir) needs: strings default language, project font, rich-widget
    structure (table cols/rows/header, chart series, roller options live only
    in ui_ext.h). 反编译所需的伴生数据（这些不进工程文件）。"""
    meta: dict[str, Any] = {}
    if compiler.rich_widgets:
        meta["rich_widgets"] = compiler.rich_widgets
    if compiler.strings:
        meta["strings_default"] = compiler.strings_default
    if compiler.default_font:
        meta["default_font"] = compiler.default_font
    pname = (compiler.ir.get("project") or {}).get("name")
    if pname:
        meta["project_name"] = str(pname)
    return meta


def run_import(eez_path: str, out_path: str) -> int:
    """Reverse direction: .eez-project (+ side-cars) → IR → uixml. Writes only
    after a self-check (recompile must reproduce the project canonically)."""
    import shutil
    import eez2ir
    import uixml
    with open(eez_path, "r", encoding="utf-8") as f:
        project = json.load(f)
    meta, translations = eez2ir.load_sidecars(eez_path)
    try:
        ir = eez2ir.eez_to_ir(project, meta, translations)
    except eez2ir.EEZImportError as e:
        print(f"✗ import: {e}", file=sys.stderr)
        return 1
    try:
        recompiled = Compiler(ir).compile()
    except IRError as e:
        print(f"✗ recompiling the imported IR failed:\n  {e}", file=sys.stderr)
        return 1
    diffs = eez2ir.canonical_diff(project, recompiled)
    if diffs:
        print("✗ import self-check failed — the .eez-project differs from a "
              "recompile of the imported IR (out-of-subset edits?):", file=sys.stderr)
        for d in diffs[:30]:
            print(f"  - {d}", file=sys.stderr)
        return 1
    if os.path.exists(out_path):
        bak = out_path + ".bak"
        shutil.copyfile(out_path, bak)
        print(f"backup → {bak}")
    uixml.ir_to_xml(ir, out_path)
    print(f"✓ Imported {eez_path}")
    print(f"  → {out_path}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="IR(JSON) → EEZ Studio .eez-project (LVGL)")
    ap.add_argument("input", help="path to the source file (.uixml preferred, legacy .ir.json, or .eez-project to import)")
    ap.add_argument("-o", "--output", default=None, help="output file")
    args = ap.parse_args(argv)

    # Reverse channel: .eez-project → uixml (Studio hand-edits flow back to
    # the XML source). 反向通道：Studio 手改进度回流到 XML 源。
    if args.input.lower().endswith(".eez-project"):
        out = args.output or (os.path.splitext(args.input)[0] + ".uixml")
        return run_import(args.input, out)
    args.output = args.output or "out_ir.eez-project"

    # Dual source entry: .uixml (XML surface syntax, preferred) or legacy
    # .ir.json — both produce the same IR dict. 双入口：.uixml 为主，.ir.json 兼容。
    if args.input.lower().endswith((".uixml", ".xml")):
        import uixml
        try:
            ir = uixml.xml_to_ir(args.input)
        except uixml.UIXMLError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
    else:
        with open(args.input, "r", encoding="utf-8") as f:
            ir = json.load(f)
    if not isinstance(ir, dict):
        print("IR root must be a JSON object / <ui> element", file=sys.stderr)
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

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(project, f, ensure_ascii=False, indent=2)

    # Font sources next to the project: Studio resolves each font's relative
    # `fonts/...` paths against the PROJECT's directory and rebuilds glyphs
    # from the TTF/OTF on canvas render — without these files the canvas
    # silently falls back to a default font (CJK = tofu). Copy every source
    # the project references into <outdir>/fonts/. 字体源随工程落盘：Studio
    # 按工程目录解析 fonts/ 相对路径并现场铸字形，缺文件=画布静默回退。
    import shutil as _shutil
    outdir = os.path.dirname(os.path.abspath(args.output))
    _copied = set()
    for fobj in project.get("fonts", []):
        for rel in ([fobj.get("source", {}).get("filePath")] +
                    [s.get("filePath") for s in fobj.get("lvglAdditionalSources", [])]):
            if not rel or rel in _copied:
                continue
            src = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel.replace("/", os.sep))
            if os.path.isfile(src):
                dst = os.path.join(outdir, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                _shutil.copyfile(src, dst)
                _copied.add(rel)
            else:
                print(f"⚠ font source missing, canvas will fall back: {rel}", file=sys.stderr)

    # ir_meta.json side-car for the reverse importer (see compiler_meta).
    meta = compiler_meta(compiler)
    if meta:
        meta_path = os.path.splitext(args.output)[0] + ".ir_meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    # action.h: the native action list; firmware includes it and implements these callbacks (porting interface). action.h：native 动作清单，固件实现这些回调。
    if compiler.native_actions:
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

    # ui_ext.h/.c: rich-widget runtime setup. EEZ now emits the full template
    # set (screens.c with create_screens + the objects_t struct exposing every
    # widget as objects.<identifier>), so the generated helpers compile against
    # it: ui_ext_init() configures charts/tables, chart_<id>_push() feeds data.
    # ui_ext.h/.c：富数据部件运行时装配。screens.c 现在生成 objects.<id> 命名句柄，
    # 生成物直接可编译：ui_ext_init() 配置图表/表格，chart_<id>_push() 喂数据。
    if any(w["kind"] in ("chart", "table") for w in compiler.rich_widgets):
        ext_h = os.path.splitext(args.output)[0] + ".ui_ext.h"
        ext_c = os.path.splitext(args.output)[0] + ".ui_ext.c"
        H = [
            "// ui_ext.h — auto-generated by ir2eez (do not edit)",
            "// Rich-widget runtime setup: call ui_ext_init() once after ui_init().",
            "// Requires the built screens.h (objects.<id> handles) from an EEZ build.",
            "#pragma once",
            "#include <stdint.h>",
            "#include <lvgl/lvgl.h>",
            "",
        ]
        C = [
            "// ui_ext.c — auto-generated by ir2eez (do not edit)",
            '#include "ui_ext.h"',
            '#include "screens.h"',
            "",
        ]
        charts = [w for w in compiler.rich_widgets if w["kind"] == "chart" and w["id"]]
        for w in charts:
            C += [f"static lv_chart_series_t *{w['id']}_series[{len(w['series'])}];", ""]
        C += ["void ui_ext_init(void) {"]
        for w in compiler.rich_widgets:
            if not w["id"]:
                continue
            macro = w["id"].upper()
            if w["kind"] == "chart":
                ctype = "LV_CHART_TYPE_LINE" if w["chart_kind"] == "line" else "LV_CHART_TYPE_SCATTER"
                H += [
                    f"// chart {w['id']} — {w['chart_kind']}, {w['points']} points, "
                    f"range {w['min']}..{w['max']}, {len(w['series'])} series",
                    f"#define {macro}_POINTS {w['points']}",
                    f"#define {macro}_MIN {w['min']}",
                    f"#define {macro}_MAX {w['max']}",
                    f"#define {macro}_SERIES_CNT {len(w['series'])}",
                    f"void {w['id']}_push(int series_idx, int32_t value);",
                ]
                C += [
                    f"    // {w['id']}",
                    f"    lv_obj_t *{w['id']} = objects.{w['id']};",
                    f"    lv_chart_set_type({w['id']}, {ctype});",
                    f"    lv_chart_set_range({w['id']}, LV_CHART_AXIS_PRIMARY_Y, {macro}_MIN, {macro}_MAX);",
                    f"    lv_chart_set_point_count({w['id']}, {macro}_POINTS);",
                    f"    lv_chart_set_update_mode({w['id']}, LV_CHART_UPDATE_MODE_SHIFT);",
                ]
                for i, s in enumerate(w["series"]):
                    hex6 = s["color"].lstrip("#")
                    C.append(
                        f"    {w['id']}_series[{i}] = lv_chart_add_series({w['id']}, "
                        f"lv_color_hex(0x{hex6}), LV_CHART_AXIS_PRIMARY_Y);"
                        f"  // {s['name']}, width {s['width']}"
                    )
            elif w["kind"] == "table":
                H += [
                    f"// table {w['id']} — {w['cols']} cols x {w['rows']} rows",
                    f"#define {macro}_COLS {w['cols']}",
                    f"#define {macro}_ROWS {w['rows']}",
                ]
                if w["header"]:
                    joined = ", ".join(json.dumps(h, ensure_ascii=False) for h in w["header"])
                    H += [
                        f"static const char *const {macro}_HEADER[] = {{ {joined} }};",
                        f"#define {macro}_HEADER_LEN {len(w['header'])}",
                    ]
                C += [
                    f"    // {w['id']}",
                    f"    lv_table_set_col_cnt(objects.{w['id']}, {macro}_COLS);",
                    f"    lv_table_set_row_cnt(objects.{w['id']}, {macro}_ROWS);",
                ]
                if w["header"]:
                    C += [
                        f"    for (int i = 0; i < {macro}_HEADER_LEN; i++) {{",
                        f"        lv_table_set_cell_value(objects.{w['id']}, 0, i, {macro}_HEADER[i]);",
                        f"    }}",
                    ]
        C += ["}", ""]
        for w in charts:
            C += [
                f"void {w['id']}_push(int series_idx, int32_t value) {{",
                f"    if (series_idx < 0 || series_idx >= {w['id'].upper()}_SERIES_CNT) return;",
                f"    lv_chart_set_next_value(objects.{w['id']}, {w['id']}_series[series_idx], value);",
                "}",
                "",
            ]
        with open(ext_h, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(H))
        with open(ext_c, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(C))
        print(f"ui_ext.h/.c → {ext_h} ({len(compiler.rich_widgets)} rich widgets)")

    # translations.yaml (lv_i18n format): compile with the lv_i18n CLI into C,
    # firmware resolves T"key" at runtime via the translate hook (upstream #1045).
    # translations.yaml（lv_i18n 格式）：lv_i18n CLI 编译成 C，固件运行时经翻译钩子解析 T"key"。
    if compiler.tr_keys or compiler.strings:
        yaml_path = os.path.splitext(args.output)[0] + ".translations.yaml"
        unused = set(compiler.strings) - compiler.tr_keys
        if unused:
            print(f"⚠ strings.texts keys never referenced by tr: {sorted(unused)}")
        langs: dict[str, list[str]] = {}
        for key in sorted(compiler.tr_keys | unused):
            for lang, txt in compiler.strings.get(key, {}).items():
                langs.setdefault(lang, []).append(f"{yaml_str(key)}: {yaml_str(txt)}")
        with open(yaml_path, "w", encoding="utf-8", newline="\n") as f:
            for lang in sorted(langs):
                f.write(f"{lang}:\n")
                for line in langs[lang]:
                    f.write(f"  {line}\n")
        print(f"translations.yaml → {yaml_path} "
              f"({len(compiler.tr_keys | unused)} keys, {len(langs)} languages, default={compiler.strings_default})")

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
