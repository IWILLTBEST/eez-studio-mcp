"""
eez2ir — .eez-project (EEZ Studio JSON) → IR reverse compiler.

Mirror of ir2eez.Compiler for the subset it generates: recognizes the shapes
the compiler emits and ERRORS on anything outside them (never silently drops
a field). Together with uixml.ir_to_xml this closes the loop:
    Studio 里手改 → 保存 .eez-project → eez_to_ir → IR → ir_to_xml → uixml

Side-cars (same basename as the .eez-project):
    *.ir_meta.json       strings default language, project default font,
                         rich-widget structure (table cols/rows/header, chart
                         series, roller options) — these never enter the
                         .eez-project itself
    *.translations.yaml  lv_i18n text table (written by ir2eez)

Usage:
    python ir2eez.py project.eez-project -o project.uixml   (--import implied)

eez2ir — EEZ 工程 JSON → IR 反编译器。只识别我们编译器生成的形状，
超纲内容一律报错拒绝（绝不静默丢字段）。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from ir2eez import _TYPE_PREFIX, BIND_TARGET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


class EEZImportError(Exception):
    pass


def _err(path: str, msg: str) -> None:
    raise EEZImportError(f"{path}: {msg}")


# ---------- type maps ----------

WTYPE_TO_IR: dict[str, str] = {
    "LVGLLabelWidget": "label",
    "LVGLButtonWidget": "button",
    "LVGLImageWidget": "image",
    "LVGLBarWidget": "bar",
    "LVGLSliderWidget": "slider",
    "LVGLTextareaWidget": "textarea",
    "LVGLDropdownWidget": "dropdown",
    "LVGLSwitchWidget": "switch",
    "LVGLCheckboxWidget": "checkbox",
    "LVGLArcWidget": "arc",
    "LVGLSpinnerWidget": "spinner",
    "LVGLRollerWidget": "roller",
    "LVGLTableWidget": "table",
    "LVGLChartWidget": "chart",
    "LVGLScaleWidget": "scale",
    "LVGLCalendarWidget": "calendar",
    "LVGLKeyboardWidget": "keyboard",
    "LVGLSpinboxWidget": "spinbox",
    "LVGLTabviewWidget": "tabview",
    "LVGLCanvasWidget": "canvas",
    "LVGLLineWidget": "line",
    "LVGLLedWidget": "led",
    "LVGLContainerWidget": "container",
    "LVGLPanelWidget": "panel",
}
SPECIAL_WTYPES = {"LVGLScreenWidget", "LVGLTabWidget", "LVGLUserWidgetWidget"}

IR_TO_PREFIX = {v: k for k, v in _TYPE_PREFIX.items()}  # label → LVGLLabelWidget

FLEX_FLOW_R = {"ROW": "row", "COLUMN": "col", "ROW_WRAP": "row-wrap",
               "ROW_REVERSE": "row-reverse", "COLUMN_REVERSE": "col-reverse"}
FLEX_JUSTIFY_R = {"START": "start", "END": "end", "CENTER": "center",
                  "SPACE_BETWEEN": "between", "SPACE_AROUND": "around",
                  "SPACE_EVENLY": "evenly"}
FLEX_ALIGN_R = {"START": "start", "END": "end", "CENTER": "center"}

# style props with a direct IR field; everything else lands in the lv dict
STYLE_TO_IR = {
    "text_font": "font", "text_color": "color", "bg_color": "bg",
    "radius": "radius", "bg_opa": "bgOpa",
}
FLEX_STYLE_KEYS = {"layout", "flex_flow", "flex_main_place", "flex_cross_place",
                   "pad_row", "pad_column"}
# per-type builder constants that must not surface in the decompiled IR
STYLE_CONSTS: dict[str, dict[str, Any]] = {
    "panel": {"pad_left": 0, "pad_top": 0, "pad_right": 0, "pad_bottom": 0,
              "border_width": 0},
    "container": {"pad_left": 0, "pad_top": 0, "pad_right": 0, "pad_bottom": 0,
                  "border_width": 0},
    "button": {"radius": 6, "bg_color": "#1C2333"},
    "led": {"shadow_width": 0},
}
LINE_COLOR_DEFAULT = "#2A3040"

ANIM_PROP_R = {"animX": "x", "animY": "y", "animWidth": "w", "animHeight": "h",
               "animOpacity": "opacity", "animImageZoom": "img_zoom",
               "animImageAngle": "img_angle"}


def strip_prefix(identifier: str, wtype: str) -> str:
    """Full identifier → IR short id (inverse of base()'s prefixing; recompiling
    the stripped id regenerates the same full identifier either way)."""
    prefix = _TYPE_PREFIX.get(wtype)
    if prefix and identifier.startswith(prefix):
        return identifier[len(prefix):]
    return identifier


# ---------- styles ----------

def parse_styles(obj: dict, path: str, ir_type: str,
                 default_font: str, ident2type: dict[str, str]) -> tuple[dict, dict | None]:
    """localStyles → (IR style fields, flex info | None). Remaining unknown
    props go into fields["lv"] as passthrough."""
    fields: dict[str, Any] = {}
    flex: dict[str, Any] | None = None
    ls = obj.get("localStyles") or {}
    definition = (ls.get("definition") or {}).get("MAIN") or {}
    props = dict(definition.get("DEFAULT") or {})

    if props.pop("layout", None) == "FLEX":
        flow = props.pop("flex_flow", None)
        if flow not in FLEX_FLOW_R:
            _err(f"{path}.styles", f"unsupported flex_flow {flow!r}")
        gap_row, gap_col = props.pop("pad_row", None), props.pop("pad_column", None)
        if gap_row != gap_col:
            _err(f"{path}.styles", "pad_row != pad_column (asymmetric gaps not in the IR subset)")
        justify = props.pop("flex_main_place", "START")
        cross = props.pop("flex_cross_place", "START")
        if justify not in FLEX_JUSTIFY_R or cross not in FLEX_ALIGN_R:
            _err(f"{path}.styles", f"unsupported flex justify/align {justify!r}/{cross!r}")
        flex = {"layout": FLEX_FLOW_R[flow], "gap": gap_row if gap_row is not None else 4,
                "justify": FLEX_JUSTIFY_R[justify], "align": FLEX_ALIGN_R[cross]}
    for k in FLEX_STYLE_KEYS:
        props.pop(k, None)

    for key, val in props.items():
        consts = STYLE_CONSTS.get(ir_type, {})
        if key in consts and val == consts[key]:
            continue
        if key == "text_align":
            if flex:
                # flex containers reuse the IR "align" for the cross axis —
                # styles_for mirrors the same value into text_align
                continue
            fields["align"] = str(val).lower()
        elif key == "line_color" and ir_type == "line":
            if val != LINE_COLOR_DEFAULT:
                fields["color"] = val
        elif key in STYLE_TO_IR:
            out = STYLE_TO_IR[key]
            if out == "font" and val == default_font:
                continue  # compiler-injected default font
            fields[out] = val
        else:
            fields.setdefault("lv", {})[key] = val

    # state styles: MAIN.CHECKED / MAIN.PRESSED / ... → IR states dict
    for state, sp in definition.items():
        if state == "DEFAULT" or not isinstance(sp, dict):
            continue
        st: dict[str, Any] = {}
        if "bg_color" in sp:
            st["bg"] = sp["bg_color"]
        if "text_color" in sp:
            st["color"] = sp["text_color"]
        if "radius" in sp:
            st["radius"] = sp["radius"]
        if not st:
            _err(f"{path}.styles[{state}]", "state style without bg/color/radius")
        unknown = set(sp) - {"bg_color", "text_color", "radius"}
        if unknown:
            _err(f"{path}.styles[{state}]",
                 f"style props {sorted(unknown)} in a state style are outside the subset")
        fields.setdefault("states", {})[state.lower()] = st
    return fields, flex


# ---------- widget parsing ----------

def _bind_of(obj: dict, ir_type: str) -> str | None:
    entry = BIND_TARGET.get(ir_type)
    if not entry:
        return None
    prop = entry[0]
    if obj.get(prop + "Type") == "expression":
        return str(obj.get(prop))
    return None


def parse_widget(obj: dict, path: str, ctx: "ImportCtx",
                 parent_flex: bool) -> dict[str, Any]:
    wtype = obj.get("type")
    if wtype == "LVGLUserWidgetWidget":
        node: dict[str, Any] = {"widget": obj.get("userWidgetPageName", "")}
    else:
        t = WTYPE_TO_IR.get(wtype)
        if t is None:
            _err(f"{path}", f"widget type {wtype!r} is outside the generated subset")
        node = {"type": t}

    if obj.get("identifier"):
        ctx.ident2type[obj["identifier"]] = wtype
        node["id"] = strip_prefix(obj["identifier"], wtype)

    if not parent_flex:
        node["x"], node["y"] = obj.get("left", 0), obj.get("top", 0)
    node["w"] = obj.get("width", 0)
    if not (wtype == "LVGLDropdownWidget" and obj.get("heightUnit") == "content"):
        node["h"] = obj.get("height", 0)

    if obj.get("hiddenFlag"):
        node["hidden"] = True

    # events (handlerType=action; flow handlers are resolved at page level)
    events = {h["eventName"].lower(): h["action"] for h in obj.get("eventHandlers") or []
              if h.get("handlerType") == "action"}
    if events:
        node["events"] = events

    bind = _bind_of(obj, node.get("type", ""))
    if bind:
        node["bind"] = bind
        # authored `preview` override: keep only when the rendered value
        # differs from what the compiler would derive from the variable
        pv, computed = obj.get("previewValue"), ctx.computed_preview(bind)
        if pv is not None and str(pv) != computed:
            node["preview"] = pv

    fields, flex = parse_styles(obj, path, node.get("type", ""), ctx.default_font,
                                ctx.ident2type)
    if flex:
        node.update(flex)
    node.update(fields)

    t = node.get("type")
    if t == "label":
        text, ttype = obj.get("text", ""), obj.get("textType", "literal")
        if ttype == "expression" and isinstance(text, str) and text.startswith('T"'):
            node["tr"] = text[2:-1] if text.endswith('"') else text[2:]
            node.pop("bind", None)
            expected = ctx.strings.get(node["tr"], {}).get(ctx.strings_default, node["tr"])
            if node.get("preview") is not None and node["preview"] == expected:
                del node["preview"]
        elif ttype == "literal" and not bind:
            if text and text != "":
                node["text"] = text
    elif t == "button":
        _collapse_button_label(obj, node, path, ctx)
    elif t == "image":
        node["src"] = obj.get("image", "")
    elif t in ("bar", "slider"):
        _skip_default(node, obj, "min", 0)
        _skip_default(node, obj, "max", 100)
        if not bind:
            _skip_default(node, obj, "value", 0, key="value", typ="literal")
    elif t == "textarea":
        if not bind and obj.get("textType") == "literal" and obj.get("text"):
            node["text"] = obj["text"]
        if obj.get("passwordMode"):
            node["password"] = True
    elif t == "dropdown":
        node["options"] = str(obj.get("options", "")).split("\n")
        _skip_default(node, obj, "selected", 0)
        if obj.get("direction") not in (None, "bottom"):
            node["direction"] = obj["direction"]
    elif t in ("switch", "checkbox"):
        if not bind and obj.get("checkedState"):
            node["checked"] = True
        if t == "checkbox" and obj.get("text"):
            node["text"] = obj["text"]
    elif t == "arc":
        _skip_default(node, obj, "rangeMin", 0, key="min")
        _skip_default(node, obj, "rangeMax", 100, key="max")
        if not bind:
            _skip_default(node, obj, "value", 25)
        for ir_key, obj_key, dflt in (("startAngle", "startAngle", 135),
                                      ("endAngle", "endAngle", 45),
                                      ("bgStartAngle", "bgStartAngle", 135),
                                      ("bgEndAngle", "bgEndAngle", 45),
                                      ("rotation", "rotation", 0)):
            if obj.get(obj_key) != dflt:
                node[ir_key] = obj.get(obj_key)
        if obj.get("mode") not in (None, "NORMAL"):
            node["mode"] = obj["mode"]
    elif t == "roller":
        node["options"] = str(obj.get("options", "")).split("\n")
        _skip_default(node, obj, "selected", 0)
        if obj.get("mode") not in (None, "NORMAL"):
            node["mode"] = obj["mode"].lower()
    elif t == "table":
        meta = ctx.rich_meta(obj.get("identifier", ""))
        if meta:
            node["cols"] = meta.get("cols", 3)
            node["rows"] = meta.get("rows", 4)
            if meta.get("header"):
                node["header"] = meta["header"]
        else:
            ctx.warn(f"{path}: table structure not in ir_meta.json — defaults 3×4, "
                     f"header lost (recompile first to write the meta side-car)")
    elif t == "chart":
        meta = ctx.rich_meta(obj.get("identifier", ""))
        if meta:
            node["kind"] = meta.get("chart_kind", "line")
            node["min"] = meta.get("min", 0)
            node["max"] = meta.get("max", 100)
            node["points"] = meta.get("points", 120)
            if meta.get("series"):
                node["series"] = meta["series"]
        else:
            ctx.warn(f"{path}: chart series not in ir_meta.json — defaults used")
    elif t == "scale":
        if obj.get("scaleMode") not in (None, "ROUND_INNER"):
            node["mode"] = str(obj["scaleMode"]).lower()
        _skip_default(node, obj, "minValue", 0, key="min")
        _skip_default(node, obj, "maxValue", 100, key="max")
        if obj.get("angleRange") != 270:
            node["angle"] = obj.get("angleRange")
        if obj.get("rotation") != 135:
            node["rotate"] = obj.get("rotation")
        if obj.get("totalTickCount") != 11:
            node["ticks"] = obj.get("totalTickCount")
        if obj.get("majorTickEvery") != 5:
            node["major"] = obj.get("majorTickEvery")
        if obj.get("showLabels") is False:
            node["labels"] = False
        if obj.get("labelTexts"):
            node["labelTexts"] = obj["labelTexts"]
        sections = []
        for s in obj.get("sections") or []:
            sec: dict[str, Any] = {"from": s.get("minValue"), "to": s.get("maxValue")}
            if s.get("mainColor") != "#E5484D":
                sec["color"] = s.get("mainColor")
            if s.get("mainWidth") != 6:
                sec["width"] = s.get("mainWidth")
            sections.append(sec)
        if sections:
            node["sections"] = sections
    elif t == "calendar":
        today = f"{obj.get('todayYear', 2026)}-{int(obj.get('todayMonth', 1)):02d}-{int(obj.get('todayDay', 1)):02d}"
        if today != "2026-01-01":
            node["today"] = today
        if obj.get("header") not in (None, "Arrow"):
            node["header"] = str(obj["header"]).lower()
        if obj.get("chineseMode"):
            node["chinese"] = True
    elif t == "keyboard":
        if obj.get("textarea"):
            node["textarea"] = strip_prefix(obj["textarea"], "LVGLTextareaWidget")
        if obj.get("mode") not in (None, "TEXT_LOWER"):
            node["mode"] = str(obj["mode"]).lower()
    elif t == "spinbox":
        _skip_default(node, obj, "min", -99999)
        _skip_default(node, obj, "max", 99999)
        if obj.get("digitCount") != 5:
            node["digits"] = obj.get("digitCount")
        if obj.get("separatorPosition") != 0:
            node["separator"] = obj.get("separatorPosition")
        if obj.get("rollover"):
            node["rollover"] = True
        if obj.get("step") != 1:
            node["step"] = obj.get("step")
        if not bind:
            _skip_default(node, obj, "value", 0)
    elif t == "tabview":
        if obj.get("tabviewPosition") not in (None, "TOP"):
            node["position"] = str(obj["tabviewPosition"]).lower()
        if obj.get("tabviewSize") != 40:
            node["barSize"] = obj.get("tabviewSize")
        if not bind:
            _skip_default(node, obj, "selectedTab", 0, key="selected")
        tabs = []
        for i, child in enumerate(obj.get("children") or []):
            if child.get("type") != "LVGLTabWidget":
                _err(f"{path}.children[{i}]", "tabview children must be tabs")
            tab: dict[str, Any] = {"title": child.get("tabName", f"Tab {i + 1}")}
            tab["children"] = ctx.parse_children(child, f"{path}.tabs[{i}]", False)
            tabs.append(tab)
        if tabs:
            node["tabs"] = tabs
        return node  # children consumed as tabs
    elif t == "line":
        points = str(obj.get("points", ""))
        node["dir"] = "v" if points.startswith("0,0 1,") else "h"
        if not obj.get("invertY", False):
            _err(f"{path}", "line invertY=False is outside the generated subset")
    elif t == "led":
        if obj.get("color") not in (None, "#0000FF"):
            node["color"] = obj.get("color")
        if not bind and obj.get("brightness") != 255:
            node["brightness"] = obj.get("brightness")
    elif t in ("panel", "container"):
        if "SCROLLABLE" in str(obj.get("widgetFlags", "")) and \
                str(obj.get("widgetFlags", "")).count("SCROLLABLE") > 0:
            node["scrollable"] = True
        if obj.get("clickableFlag") and not events and not node.get("scrollable"):
            node["clickable"] = True

    # children recursion (only container/panel carry children in the IR subset;
    # button's child label was already collapsed into text above)
    if t in ("panel", "container"):
        node["children"] = ctx.parse_children(obj, path, bool(flex))
        if not node["children"]:
            del node["children"]
    elif obj.get("children") and t != "button":
        _err(f"{path}", f"{t!r} with children is outside the IR subset")

    return node


def _skip_default(node: dict, obj: dict, obj_key: str, dflt: Any,
                  key: str | None = None, typ: str = "literal") -> None:
    """Copy obj[obj_key] into node under `key` when it is a literal that
    differs from the builder default."""
    if obj.get(obj_key + "Type", "literal") != "literal":
        return
    val = obj.get(obj_key)
    if val is not None and val != dflt:
        node[key or obj_key] = val


def _collapse_button_label(obj: dict, node: dict, path: str, ctx: "ImportCtx") -> None:
    """The compiler renders button text as a centered content-sized child label;
    fold it back into the button's text/font."""
    children = obj.get("children") or []
    if not children:
        return
    if len(children) > 1:
        _err(f"{path}", "button with more than one child is outside the subset")
    child = children[0]
    if child.get("type") != "LVGLLabelWidget" or child.get("identifier"):
        _err(f"{path}.children[0]", "non-label button child is outside the subset")
    d = ((child.get("localStyles") or {}).get("definition") or {}).get("MAIN", {}).get("DEFAULT", {})
    # allowed shape: object-align CENTER + optionally the button's own font
    # (the compiler projects the button font onto the child label)
    extra_style = set(d) - {"align", "text_font"}
    if d.get("align") != "CENTER" or extra_style:
        _err(f"{path}.children[0]", "styled button label is outside the subset")
    if child.get("textType") != "literal":
        _err(f"{path}.children[0]", "expression button label is outside the subset")
    node["text"] = child.get("text", "")
    if d.get("text_font") and d.get("text_font") != ctx.default_font:
        node["font"] = d["text_font"]


# ---------- flow steps ----------

def parse_step(comp: dict, path: str, ctx: "ImportCtx") -> dict[str, Any]:
    t = comp.get("type")
    if t == "DelayActionComponent":
        return {"op": "delay", "ms": int(comp.get("milliseconds", "100"))}
    if t == "SetVariableActionComponent":
        entries = comp.get("entries") or []
        if len(entries) != 1:
            _err(f"{path}", "SetVariable with multiple entries is outside the subset")
        e = entries[0]
        return {"op": "set", "variable": e.get("variable"), "value": e.get("value")}
    if t == "CallActionActionComponent":
        return {"op": "call", "action": comp.get("action")}
    if t == "LVGLActionComponent":
        return _parse_lvgl_action((comp.get("actions") or [None])[0], path, ctx)
    if t == "StartActionComponent":
        _err(f"{path}", "Start node in the middle of a chain")
    _err(f"{path}", f"flow component {t!r} is outside the subset")


def _short_target(target: str, ctx: "ImportCtx") -> str:
    wtype = ctx.ident2type.get(target)
    return strip_prefix(target, wtype) if wtype else target


def _parse_lvgl_action(item: dict | None, path: str, ctx: "ImportCtx") -> dict[str, Any]:
    if not item:
        _err(f"{path}", "LVGL action component without an action item")
    action = item.get("action")
    target = _short_target(item.get("object", ""), ctx)
    if action == "changeScreen":
        step: dict[str, Any] = {"op": "lvgl", "action": "changeScreen",
                                "screen": item.get("screen")}
        if item.get("fadeMode") != "FADE_IN":
            step["fade"] = item.get("fadeMode")
        if item.get("speed") != 200:
            step["speed"] = item.get("speed")
        if item.get("delay"):
            step["delay"] = item.get("delay")
        if item.get("useStack"):
            step["useStack"] = True
        return step
    if action in ANIM_PROP_R:
        step = {"op": "lvgl", "action": "anim", "prop": ANIM_PROP_R[action], "target": target}
        if item.get("start") != 0:
            step["from"] = item.get("start")
        if item.get("end") != 100:
            step["to"] = item.get("end")
        if item.get("delay"):
            step["delay"] = item.get("delay")
        if item.get("time") != 400:
            step["time"] = item.get("time")
        if item.get("relative"):
            step["relative"] = True
        if item.get("instant") is not True:
            step["instant"] = item.get("instant")
        if str(item.get("path", "")).lower() != "ease_in_out":
            step["ease"] = str(item.get("path")).lower()
        if item.get("repeatCount"):
            step["repeat"] = item.get("repeatCount")
        if item.get("playback"):
            step["playback"] = True
        return step
    if action in ("objAddState", "objClearState", "objAddFlag", "objClearFlag"):
        key = "flag" if "Flag" in action else "state"
        step = {"op": "lvgl", "action": action, "target": target}
        val = item.get(key)
        if val not in (None, "CHECKED" if key == "state" else "HIDDEN"):
            step[key] = str(val).lower()
        return step
    if action == "labelSetText":
        step = {"op": "lvgl", "action": "labelSetText", "target": target}
        if item.get("text"):
            step["text"] = item.get("text")
        return step
    if action == "objSetY":
        step = {"op": "lvgl", "action": "objSetY", "target": target}
        if item.get("y") != 0:
            step["y"] = item.get("y")
        return step
    _err(f"{path}", f"lvgl action {action!r} is outside the subset")


# ---------- pages / project ----------

class ImportCtx:
    def __init__(self, default_font: str, rich: list[dict]):
        self.default_font = default_font
        self.rich = rich
        self.ident2type: dict[str, str] = {}
        self.warnings: list[str] = []
        self.vars: dict[str, Any] = {}          # name → typed default
        self.strings: dict[str, dict[str, str]] = {}
        self.strings_default = "en"

    def computed_preview(self, expr: str) -> str:
        """What the compiler's _preview would render for a bare variable name
        (mirrors ir2eez's fmt); used to tell an authored `preview` override
        from the derived default. 镜像编译端预览求值，识别手写 preview。"""
        v = self.vars.get(expr)
        if v is None:
            return expr
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)

    def rich_meta(self, identifier: str) -> dict | None:
        for w in self.rich:
            if w.get("id") == identifier:
                return w
        return None

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def parse_children(self, obj: dict, path: str, parent_flex: bool) -> list[dict]:
        return [parse_widget(c, f"{path}.children[{i}]", self, parent_flex)
                for i, c in enumerate(obj.get("children") or [])]


def parse_flow_entries(page: dict, path: str, ctx: ImportCtx) -> list[dict]:
    """Page-level flow: widget event pins (handlerType=flow) connected to step
    chains — the reverse of build_page's flow loop."""
    lines = page.get("connectionLines") or []
    comps = page.get("components") or []
    flow_comps = {c["objID"]: c for c in comps
                  if str(c.get("type", "")).endswith("ActionComponent")}
    out: list[dict] = []

    def walk_widgets(objs: list):
        for o in objs:
            yield o
            yield from walk_widgets(o.get("children") or [])

    widgets = list(walk_widgets(comps))
    for w in widgets:
        for h in w.get("eventHandlers") or []:
            if h.get("handlerType") != "flow":
                continue
            evt = h.get("eventName", "")
            entry_line = next((l for l in lines
                               if l.get("source") == w["objID"] and l.get("output") == evt), None)
            steps: list[dict] = []
            if entry_line:
                cur = flow_comps.get(entry_line.get("target"))
                while cur is not None:
                    steps.append(parse_step(cur, f"{path}.flow", ctx))
                    nxt = next((l for l in lines if l.get("source") == cur["objID"]
                                and l.get("output") == "@seqout"), None)
                    cur = flow_comps.get(nxt.get("target")) if nxt else None
            ident = w.get("identifier", "")
            out.append({"when": {"id": strip_prefix(ident, w.get("type", "")),
                                 "event": evt.lower()},
                        "steps": steps})
    return out


def parse_page(page: dict, path: str, ctx: ImportCtx,
               is_user_widget: bool) -> dict[str, Any]:
    comps = page.get("components") or []
    if not comps:
        _err(path, "page without components")

    if is_user_widget:
        children = list(comps)
        node: dict[str, Any] = {"width": page.get("width", 100),
                                "height": page.get("height", 50)}
        first = comps[0]
        if (first.get("type") == "LVGLPanelWidget" and not first.get("identifier")
                and first.get("left") == 0 and first.get("top") == 0
                and first.get("width") == node["width"]
                and first.get("height") == node["height"]
                and not (first.get("children") or [])):
            d = ((first.get("localStyles") or {}).get("definition") or {})
            props = (d.get("MAIN") or {}).get("DEFAULT") or {}
            if set(props) <= {"bg_color", "pad_left", "pad_top", "pad_right",
                              "pad_bottom", "border_width"} and props.get("bg_color"):
                node["bg"] = props["bg_color"]
                children = comps[1:]
        node["children"] = [parse_widget(c, f"{path}.children[{i}]", ctx, False)
                            for i, c in enumerate(children)]
        if not node["children"]:
            del node["children"]
        return node

    root = comps[0]
    if root.get("type") != "LVGLScreenWidget":
        _err(f"{path}", "page root is not LVGLScreenWidget (outside the subset)")
    node = {}
    fields, flex = parse_styles(root, f"{path}", "screen", ctx.default_font,
                                ctx.ident2type)
    node.update(fields)
    if flex:
        node.update(flex)
    node["children"] = ctx.parse_children(root, path, bool(flex))
    if not node["children"]:
        del node["children"]
    flows = parse_flow_entries(page, path, ctx)
    if flows:
        node["flow"] = flows
    return node


def parse_action(action: dict, ctx: ImportCtx) -> dict[str, Any]:
    name = action.get("name", "")
    if action.get("implementationType") == "native":
        return {"name": name}
    comps = action.get("components") or []
    lines = action.get("connectionLines") or []
    flow_comps = {c["objID"]: c for c in comps
                  if str(c.get("type", "")).endswith("ActionComponent")}
    if not comps or comps[0].get("type") != "StartActionComponent":
        _err(f"actions[{name!r}]", "flow action without a Start node (outside the subset)")
    steps: list[dict] = []
    nxt = next((l for l in lines if l.get("source") == comps[0]["objID"]
                and l.get("output") == "@seqout"), None)
    cur = flow_comps.get(nxt.get("target")) if nxt else None
    while cur is not None:
        steps.append(parse_step(cur, f"actions[{name!r}]", ctx))
        nxt = next((l for l in lines if l.get("source") == cur["objID"]
                    and l.get("output") == "@seqout"), None)
        cur = flow_comps.get(nxt.get("target")) if nxt else None
    return {"name": name, "steps": steps}


def parse_variable(v: dict) -> dict[str, Any]:
    out: dict[str, Any] = {"name": v.get("name", ""), "type": v.get("type", "string")}
    dflt = v.get("defaultValue")
    if v.get("type") == "string":
        try:
            out["default"] = json.loads(dflt) if dflt else ""
        except (json.JSONDecodeError, TypeError):
            out["default"] = str(dflt or "").strip('"')
    else:
        if dflt in ("true", "false"):
            out["default"] = dflt == "true"
        else:
            try:
                out["default"] = float(dflt) if "." in str(dflt) else int(dflt)
            except (TypeError, ValueError):
                out["default"] = dflt
    if v.get("native") is False:
        out["native"] = False
    return out


def load_translations(path: str) -> dict[str, dict[str, str]]:
    """lv_i18n yaml subset written by ir2eez: `lang:` blocks of `  key: text`."""
    texts: dict[str, dict[str, str]] = {}
    lang = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#"):
                continue
            if not line.startswith(" ") and line.endswith(":"):
                lang = line[:-1]
                continue
            if lang and line.startswith("  "):
                kv = line.strip()
                if ":" not in kv:
                    continue
                key, _, txt = kv.partition(":")
                key, txt = key.strip(), txt.strip()
                if len(txt) >= 2 and txt[0] == '"' and txt[-1] == '"':
                    try:
                        txt = json.loads(txt)
                    except json.JSONDecodeError:
                        txt = txt[1:-1]
                texts.setdefault(key, {})[lang] = txt
    return texts


def eez_to_ir(project: dict[str, Any],
              meta: dict[str, Any] | None = None,
              translations: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    meta = meta or {}
    ctx = ImportCtx(str(meta.get("default_font") or ""), meta.get("rich_widgets") or [])
    gen = project.get("settings", {}).get("general", {})
    ir: dict[str, Any] = {
        "project": {
            "width": gen.get("displayWidth", 1024),
            "height": gen.get("displayHeight", 600),
        }
    }
    if meta.get("default_font"):
        ir["project"]["font"] = meta["default_font"]
    if meta.get("project_name"):
        ir["project"]["name"] = meta["project_name"]

    gvars = (project.get("variables") or {}).get("globalVariables") or []
    if gvars:
        ir["variables"] = [parse_variable(v) for v in gvars]
        for v in ir["variables"]:
            ctx.vars[v["name"]] = v.get("default")

    if translations or meta.get("strings_default"):
        strings: dict[str, Any] = {"texts": translations or {}}
        if meta.get("strings_default"):
            strings["default"] = meta["strings_default"]
            ctx.strings_default = meta["strings_default"]
        ctx.strings = translations or {}
        if strings["texts"] or strings.get("default"):
            ir["strings"] = strings

    widgets = {}
    for uw in project.get("userWidgets") or []:
        widgets[uw.get("name", "")] = parse_page(uw, f"widgets[{uw.get('name')!r}]",
                                                 ctx, is_user_widget=True)
    if widgets:
        ir["widgets"] = widgets

    screens = []
    for pg in project.get("userPages") or []:
        node = parse_page(pg, f"screens[{pg.get('name')!r}]", ctx, is_user_widget=False)
        node = {"name": pg.get("name", ""), **node}
        screens.append(node)
    if screens:
        ir["screens"] = screens

    actions = [parse_action(a, ctx) for a in project.get("actions") or []]
    if actions:
        ir["actions"] = actions

    if ctx.warnings:
        for w in ctx.warnings:
            print(f"⚠ {w}", file=sys.stderr)
    return ir


def load_sidecars(eez_path: str) -> tuple[dict, dict]:
    """meta + translations next to the .eez-project (missing files are OK)."""
    base = os.path.splitext(eez_path)[0]
    meta: dict = {}
    if os.path.exists(base + ".ir_meta.json"):
        meta = json.load(open(base + ".ir_meta.json", encoding="utf-8"))
    translations: dict[str, dict[str, str]] = {}
    if os.path.exists(base + ".translations.yaml"):
        translations = load_translations(base + ".translations.yaml")
    return meta, translations


# ---------- canonical comparison (oracle for the round trip) ----------

def canon(value: Any) -> Any:
    """Replace every objID with its first-seen sequence index so two compiles
    of semantically identical IR compare equal despite random uuids. String
    references (connectionLines source/target) map through the same table —
    components are always emitted before their lines."""
    seen: dict[str, str] = {}

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            out: dict[str, Any] = {}
            for k, val in v.items():
                if k == "objID" and isinstance(val, str):
                    if val not in seen:
                        seen[val] = f"#o{len(seen)}"
                    out[k] = seen[val]
                else:
                    out[k] = walk(val)
            return out
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str) and v in seen:
            return seen[v]
        return v

    return walk(value)


def canonical_diff(a: dict, b: dict) -> list[str]:
    return _cdiff(canon(a), canon(b), "$")


def _cdiff(a: Any, b: Any, path: str) -> list[str]:
    if type(a) is not type(b):
        return [f"{path}: {type(a).__name__}({a!r}) vs {type(b).__name__}({b!r})"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: only in recompiled output")
            elif k not in b:
                out.append(f"{path}.{k}: lost on import")
            else:
                out += _cdiff(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} items vs {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b))
                for d in _cdiff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: {a!r} vs {b!r}"]
