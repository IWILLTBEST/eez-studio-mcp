"""UIXML — the XML surface syntax of the IR.

`xxx.uixml` replaces `xxx.ir.json` as the human/AI-editable source format.
This module converts losslessly in both directions:

    xml_to_ir(path)  -> IR dict   (consumed by ir2eez.Compiler, unchanged)
    ir_to_xml(ir, path)           (migration of legacy .ir.json + generators)

Schema is OUR OWN vocabulary (bind/tr/anim/lv passthrough, widget names) and
deliberately INDEPENDENT of the LVGL XML Specification — that spec's license
forbids third-party editors/generators; this is not that format. Runtime is
and stays compiled C.

Conventions
-----------
- attributes map 1:1 onto IR fields (x/y/w/h/text/bind/... see TYPE_TABLE)
- `xmlns:lv="urn:eez:lv"` prefixed attributes are style passthrough:
  lv:shadow-width="26" -> lv: {"shadow_width": 26}
- children nest as elements; repeated items are child elements
  (<series/>, <section/>, <tab/>, step verbs inside <action>)
- events as attributes: on-clicked="ack"  on-value-changed="on_mode"
- roller/dropdown `options` and table `header` accept comma strings
  (commas inside an option: use <options><o>…</o></options> explicit form)
- XML comments are native documentation, dropped on parse
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Any

LV_NS = "urn:eez:lv"

# IR field name -> python type for lossless round-tripping. Fields not listed
# use heuristic coercion (int -> bool -> str); the compiler's own validators
# remain the safety net for anything unexpected.
TYPE_TABLE: dict[str, str] = {
    # geometry
    "x": "int", "y": "int", "w": "int", "h": "int",
    "width": "int", "height": "int", "gap": "int",
    # common visuals
    "radius": "int", "bgOpa": "int",
    # bar/slider/arc/spinbox/scale/chart
    "min": "int", "max": "int", "value": "int",
    "points": "int", "digits": "int", "separator": "int", "step": "int",
    "angle": "int", "rotate": "int", "ticks": "int", "major": "int",
    # roller/tabview
    "selected": "int", "barSize": "int",
    # table
    "cols": "int", "rows": "int",
    # anim / steps
    "from": "int", "to": "int", "time": "int", "delay": "int",
    "repeat": "int", "ms": "int", "speed": "int",
    # booleans
    "hidden": "bool", "checked": "bool", "labels": "bool",
    "rollover": "bool", "playback": "bool", "instant": "bool",
    "relative": "bool", "chinese": "bool", "useStack": "bool",
    "password": "bool", "native": "bool",
    # strings
    "type": "str", "id": "str", "text": "str", "preview": "str",
    "font": "str", "color": "str", "bg": "str", "align": "str",
    "bind": "str", "mode": "str", "kind": "str", "ease": "str",
    "prop": "str", "target": "str", "variable": "str", "action": "str",
    "name": "str", "screen": "str", "fade": "str", "src": "str",
    "today": "str", "header": "str", "labelTexts": "str",
    "layout": "str", "justify": "str", "value_expr": "str",
}

_BOOL = {"true": True, "false": False}


class UIXMLError(Exception):
    pass


def _err(elem: ET.Element, msg: str) -> None:
    line = getattr(elem, "sourceline", "?")
    raise UIXMLError(f"line {line}: <{_unns(elem.tag)}> {msg}")


def _unns(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _coerce(key: str, raw: str) -> Any:
    """Attribute string -> typed IR value. Table first, heuristic fallback."""
    t = TYPE_TABLE.get(key)
    if t == "int":
        try:
            return int(raw)
        except ValueError:
            return raw  # compiler validator produces the real error message
    if t == "bool":
        if raw.lower() in _BOOL:
            return _BOOL[raw.lower()]
        return raw
    if t == "float":
        try:
            return float(raw)
        except ValueError:
            return raw
    if t == "str":
        return raw
    # heuristic for unlisted fields
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    if raw.lower() in _BOOL:
        return _BOOL[raw.lower()]
    return raw


# attribute names on the wire use dashes; IR keys use underscores
def _dash_to_key(name: str) -> str:
    return name.replace("-", "_")


def _key_to_dash(key: str) -> str:
    return key.replace("_", "-")


def _parse_attrs(elem: ET.Element, out: dict, *, events: bool = True) -> None:
    for name, raw in elem.attrib.items():
        if name.startswith("{"):
            ns, local = name[1:].split("}", 1)
            if ns == LV_NS:
                out.setdefault("lv", {})[_dash_to_key(local)] = _coerce(_dash_to_key(local), raw)
            # unknown namespaces are ignored
        elif name == "on" or name.startswith("on-"):
            if not events:
                _err(elem, f"event attribute {name!r} not allowed here")
            out.setdefault("events", {})[name[3:] if name.startswith("on-") else ""] = raw
        elif name == "xmlns" or name.startswith("xmlns:"):
            continue
        else:
            out[_dash_to_key(name)] = _coerce(_dash_to_key(name), raw)


def _parse_step_or_widget_attrs(elem: ET.Element, out: dict) -> None:
    _parse_attrs(elem, out, events=False)


def _parse_widget(elem: ET.Element) -> dict:
    tag = _unns(elem.tag)
    # user widget instance: {"widget": "NavBar", x, y, ...} — no type field
    if tag == "instance":
        node_i: dict[str, Any] = {}
        _parse_attrs(elem, node_i)
        if "widget" not in node_i:
            _err(elem, "<instance> needs a widget attribute (the user widget name)")
        return node_i
    node: dict[str, Any] = {"type": tag}
    _parse_attrs(elem, node)
    kids: list[dict] = []
    for child in elem:
        tag = _unns(child.tag)
        if tag in ("series", "section", "tab", "options", "header"):
            _collect_named_list(node, child)
        elif tag == "state":
            # selected-state styles: <state name="PRESSED" bg="#..."/>
            st: dict = {}
            _parse_step_or_widget_attrs(child, st)
            name = st.pop("name", None)
            if not name:
                _err(child, "<state> needs a name attribute (e.g. PRESSED)")
            node.setdefault("states", {})[name] = st
        elif tag == "trigger":
            node.setdefault("flow", []).append(_parse_trigger(child))
        else:
            kids.append(_parse_widget(child))
    if kids:
        node["children"] = kids
    # comma-string attributes become lists (writer side of the convention).
    # options is a list on roller/dropdown; header is a list on table but a
    # plain enum string ("arrow"/"none"/"dropdown") on calendar.
    if isinstance(node.get("options"), str) and node["type"] in ("roller", "dropdown"):
        node["options"] = node["options"].split(",")
    if isinstance(node.get("header"), str) and node["type"] == "table":
        node["header"] = node["header"].split(",")
    return node


def _collect_named_list(node: dict, elem: ET.Element) -> None:
    tag = _unns(elem.tag)
    if tag == "series":
        s: dict = {}
        _parse_step_or_widget_attrs(elem, s)
        node.setdefault("series", []).append(s)
    elif tag == "section":
        s: dict = {}
        _parse_step_or_widget_attrs(elem, s)
        node.setdefault("sections", []).append(s)
    elif tag == "tab":
        tab: dict = {}
        _parse_attrs(elem, tab)
        tab["children"] = _parse_children(elem)
        node.setdefault("tabs", []).append(tab)
    elif tag in ("options", "header"):
        vals = [c.text if _unns(c.tag) == "o" else _unns(c.tag) for c in elem]
        node[tag] = vals


def _parse_children(elem: ET.Element) -> list[dict]:
    return _parse_children_list(list(elem))


def _parse_children_list(elems: list) -> list[dict]:
    return [_parse_widget(c) for c in elems
            if _unns(c.tag) not in ("series", "section", "tab", "options", "header")]


# step verbs: element tag -> fixed IR fields
_STEP_VERBS = {
    "change-screen": {"op": "lvgl", "action": "changeScreen"},
    "anim": {"op": "lvgl", "action": "anim"},
    "label-set-text": {"op": "lvgl", "action": "labelSetText"},
    "obj-set-y": {"op": "lvgl", "action": "objSetY"},
    "obj-add-state": {"op": "lvgl", "action": "objAddState"},
    "obj-clear-state": {"op": "lvgl", "action": "objClearState"},
    "obj-add-flag": {"op": "lvgl", "action": "objAddFlag"},
    "obj-clear-flag": {"op": "lvgl", "action": "objClearFlag"},
    "set": {"op": "set"},
    "delay": {"op": "delay"},
    "call": {"op": "call"},
    "lvgl": {"op": "lvgl"},  # generic: action= attribute carries the op name
}


def _parse_action(elem: ET.Element) -> dict:
    act: dict[str, Any] = {}
    _parse_attrs(elem, act, events=False)
    steps = []
    for child in elem:
        tag = _unns(child.tag)
        if tag not in _STEP_VERBS:
            _err(elem, f"unknown step verb <{tag}> (expected one of {sorted(_STEP_VERBS)})")
        step: dict[str, Any] = dict(_STEP_VERBS[tag])
        _parse_step_or_widget_attrs(child, step)
        steps.append(_fix_step(step))
    if steps:  # empty steps = native action (no steps key in IR)
        act["steps"] = steps
    return act


def xml_to_ir(path: str) -> dict[str, Any]:
    """Parse a .uixml file into the IR dict. The file may be a complete
    single-file project OR a manifest with <include src="..."/> elements —
    included fragments (same <ui> root) are spliced in order, which enables
    the Qt-style split: project.uixml + logic.uixml + strings.uixml +
    screens/*.uixml. Includes resolve relative to the including file."""
    elems, _ = _resolve(path, set())
    return _elements_to_ir(elems, path)


def _resolve(path: str, seen: set[str]) -> tuple[list, list]:
    """Parse one file, recursively splicing <include> children.
    Returns (elements, seen-files) for the duplicate check."""
    absp = os.path.abspath(path)
    if absp in seen:
        raise UIXMLError(f"{path}: include cycle detected at {absp}")
    seen = seen | {absp}
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        raise UIXMLError(f"{path}: XML parse error: {e}") from e
    root = tree.getroot()
    if _unns(root.tag) != "ui":
        raise UIXMLError(f"{path}: root element must be <ui>, got <{_unns(root.tag)}>")
    out: list = []
    for child in root:
        if _unns(child.tag) == "include":
            src = child.get("src")
            if not src:
                raise UIXMLError(f"{path}: <include> needs a src attribute")
            frag = os.path.join(os.path.dirname(os.path.abspath(path)), src)
            if not os.path.exists(frag):
                raise UIXMLError(f"{path}: include not found: {src}")
            out += _resolve(frag, seen)[0]
        else:
            out.append(child)
    return out, seen


def _elements_to_ir(elems: list, path: str) -> dict[str, Any]:
    ir: dict[str, Any] = {}
    for child in elems:
        tag = _unns(child.tag)
        if tag == "project":
            proj: dict = {}
            _parse_step_or_widget_attrs(child, proj)
            ir["project"] = proj
        elif tag == "var":
            v: dict = {}
            _parse_step_or_widget_attrs(child, v)
            ir.setdefault("variables", []).append(v)
        elif tag == "strings":
            s: dict = {}
            _parse_step_or_widget_attrs(child, s)
            texts: dict = {}
            for t in child:
                if _unns(t.tag) != "text":
                    _err(child, f"unexpected <{_unns(t.tag)}> inside <strings>")
                key = t.get("key")
                if not key:
                    _err(t, "missing key attribute")
                langs = {}
                for l in t:
                    if _unns(l.tag) != "l":
                        _err(t, f"unexpected <{_unns(l.tag)}> inside <text>")
                    langs[l.get("lang", "")] = (l.text or "").strip()
                texts[key] = langs
            s["texts"] = texts
            ir["strings"] = s
        elif tag == "widget":
            name = child.get("name")
            if not name:
                _err(child, "<widget> needs a name attribute")
            w = _parse_container(child)
            w.pop("name", None)  # the name lives in the map key, not the def
            ir.setdefault("widgets", {})[name] = w
        elif tag == "screen":
            name = child.get("name")
            if not name:
                _err(child, "<screen> needs a name attribute")
            scr = _parse_container(child)
            scr["name"] = name
            ir.setdefault("screens", []).append(scr)
        elif tag == "action":
            name = child.get("name")
            if not name:
                _err(child, "<action> needs a name attribute")
            act = _parse_action(child)
            act["name"] = name
            ir.setdefault("actions", []).append(act)
        else:
            _err(root, f"unknown element <{tag}> (expected "
                       "project/var/strings/widget/screen/action)")
    return ir


def _parse_container(elem: ET.Element) -> dict:
    """<screen>/<widget> body: attributes + children (+ page-level <trigger>
    flows), no type."""
    node: dict = {}
    _parse_attrs(elem, node)
    rest = [c for c in elem if _unns(c.tag) != "trigger"]
    flow = [_parse_trigger(c) for c in elem if _unns(c.tag) == "trigger"]
    kids = _parse_children_list(rest)
    if kids:
        node["children"] = kids
    if flow:
        node["flow"] = flow
    return node


def _fix_step(step: dict) -> dict:
    """Steps parsed through the generic attr coercion need a few type repairs:
    set.value is an EEZ *expression* (string) even when it looks numeric."""
    if step.get("op") == "set" and "value" in step and not isinstance(step["value"], str):
        step["value"] = str(step["value"])
    return step


def _parse_trigger(elem: ET.Element) -> dict:
    # page-level flow: a widget event pin wired to a step chain
    # (compiles to handlerType=flow + connectionLines). 页面级流。
    when: dict[str, Any] = {}
    _parse_attrs(elem, when)
    if "id" not in when:
        _err(elem, "<trigger> needs an id attribute (the widget id)")
    when.setdefault("event", "clicked")
    trig: dict[str, Any] = {"when": when, "steps": []}
    for sc in elem:
        stag = _unns(sc.tag)
        if stag not in _STEP_VERBS:
            _err(sc, f"unknown step verb <{stag}> inside <trigger>")
        step: dict[str, Any] = dict(_STEP_VERBS[stag])
        _parse_step_or_widget_attrs(sc, step)
        trig["steps"].append(_fix_step(step))
    return trig


# ---------------- IR -> XML ----------------

def _fmt_attr(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


_SPECIAL_KEYS = ("type", "children", "series", "sections", "tabs", "events",
                 "lv", "options", "header", "states", "flow")


def _widget_to_elem(node: dict, tag: str) -> ET.Element:
    elem = ET.Element(tag)
    for k, v in node.items():
        if k in _SPECIAL_KEYS:
            continue
        elem.set(_key_to_dash(k), _fmt_attr(v))
    for evt, act in (node.get("events") or {}).items():
        elem.set(f"on-{evt}", str(act))
    for k, v in (node.get("lv") or {}).items():
        elem.set(f"{{{LV_NS}}}{_key_to_dash(k)}", _fmt_attr(v))
    for name, st in (node.get("states") or {}).items():
        se = ET.SubElement(elem, "state", {"name": name})
        for k, v in st.items():
            se.set(_key_to_dash(k), _fmt_attr(v))
    for s in node.get("series") or []:
        se = ET.SubElement(elem, "series")
        for k, v in s.items():
            se.set(_key_to_dash(k), _fmt_attr(v))
    for s in node.get("sections") or []:
        se = ET.SubElement(elem, "section")
        for k, v in s.items():
            se.set(_key_to_dash(k), _fmt_attr(v))
    for t in node.get("tabs") or []:
        te = ET.SubElement(elem, "tab")
        for k, v in t.items():
            if k != "children":
                te.set(_key_to_dash(k), _fmt_attr(v))
        for c in t.get("children") or []:
            child_tag = str(c["type"]) if "type" in c else "instance"
            te.append(_widget_to_elem(c, child_tag))
    for trig in node.get("flow") or []:
        tre = ET.SubElement(elem, "trigger")
        when = trig.get("when") or {}
        tre.set("id", str(when.get("id", "")))
        tre.set("event", str(when.get("event", "clicked")))
        for step in trig.get("steps") or []:
            tre.append(_step_to_elem(step))
    for k in ("options", "header"):
        vals = node.get(k)
        if isinstance(vals, list):
            elem.set(k, ",".join(str(v) for v in vals))
        elif vals is not None:
            elem.set(k, str(vals))
    for c in node.get("children") or []:
        child_tag = str(c["type"]) if "type" in c else "instance"
        elem.append(_widget_to_elem(c, child_tag))
    return elem


_VERB_TO_TAG = {(v.get("action") or v["op"]): tag for tag, v in _STEP_VERBS.items()}


def _step_to_elem(step: dict) -> ET.Element:
    op = step.get("op")
    key = step.get("action") if op == "lvgl" else op
    tag = _VERB_TO_TAG.get(key, "lvgl")
    se = ET.Element(tag)
    for k, v in step.items():
        if k == "op":
            continue
        if k == "action" and tag != "lvgl":
            continue  # implied by the verb element
        se.set(_key_to_dash(k), _fmt_attr(v))
    return se


def _action_to_elem(act: dict) -> ET.Element:
    elem = ET.Element("action", {"name": str(act["name"])})
    for step in act.get("steps") or []:
        elem.append(_step_to_elem(step))
    return elem


def ir_to_xml(ir: dict[str, Any], path: str) -> None:
    # register_namespace makes ET emit xmlns:lv automatically on the first
    # element carrying an {urn:eez:lv} attribute — declaring it manually as
    # well produces a duplicate-attribute parse error.
    ET.register_namespace("lv", LV_NS)
    root = ET.Element("ui")

    proj = ir.get("project") or {}
    if proj:
        pe = ET.SubElement(root, "project")
        for k, v in proj.items():
            pe.set(_key_to_dash(k), _fmt_attr(v))

    for v in ir.get("variables") or []:
        ve = ET.SubElement(root, "var")
        for k, val in v.items():
            ve.set(_key_to_dash(k), _fmt_attr(val))

    strings = ir.get("strings") or {}
    if strings.get("texts"):
        se = ET.SubElement(root, "strings", {"default": str(strings.get("default", "en"))})
        for key, langs in strings["texts"].items():
            te = ET.SubElement(se, "text", {"key": key})
            for lang, text in langs.items():
                le = ET.SubElement(te, "l", {"lang": lang})
                le.text = text

    for name, w in (ir.get("widgets") or {}).items():
        we = _widget_to_elem(w, "widget")
        we.set("name", name)
        root.append(we)

    for scr in ir.get("screens") or []:
        body = {k: v for k, v in scr.items() if k != "name"}
        se = _widget_to_elem(body, "screen")
        se.set("name", str(scr["name"]))
        root.append(se)

    for act in ir.get("actions") or []:
        root.append(_action_to_elem(act))

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


# ---------------- structural diff (migration safety net) ----------------

def roundtrip_equal(ir_a: dict, ir_b: dict) -> list[str]:
    """Structural diff used by the migration check; empty list = identical."""
    return _diff(ir_a, ir_b, "$", "$")


def _diff(a: Any, b: Any, pa: str, pb: str) -> list[str]:
    if type(a) is not type(b):
        return [f"{pa}: {type(a).__name__}({a!r}) vs {type(b).__name__}({b!r})"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{pb}.{k} missing in first")
            elif k not in b:
                out.append(f"{pa}.{k} missing in second")
            else:
                out += _diff(a[k], b[k], f"{pa}.{k}", f"{pb}.{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{pa}: {len(a)} items vs {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff(x, y, f"{pa}[{i}]", f"{pb}[{i}]")
        return out
    return [] if a == b else [f"{pa}={a!r} vs {pb}={b!r}"]
