# -*- coding: utf-8 -*-
"""Generate examples/glass/glass.ir.json — glassmorphism dashboard showcase.

玻璃拟态仪表盘示例 IR 生成器：半透明卡片 + 阴影 + 渐变背景 + 交错入场动画。
Cards animate in staggered; Replay button re-triggers; status LED breathes.
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import uixml

W, H = 1024, 600

# Icons confirmed in fonts/demo_16 meta icon ranges
ICONS = {
    "speed": "\uf0b0",     # wave-square
    "temp": "\uf2db",      # microchip
    "volt": "\uf0e7",      # bolt
    "curr": "\uf1e6",      # plug
    "power": "\uf00c",     # check
}

GLASS = {
    "bg": "#1B2436",
    "bgOpa": 150,
    "radius": 16,
    "lv": {
        "border_width": 1,
        "border_color": "#9FB2D8",
        "border_opa": 40,
        "shadow_width": 26,
        "shadow_spread": 3,
        "shadow_color": "#000000",
        "shadow_opa": 130,
    },
}

metrics = [
    ("speed", "Speed", "speed", "RPM", ICONS["speed"]),
    ("temp", "Core Temp", "temp", "\u00b0C", ICONS["temp"]),
    ("volt", "Bus Voltage", "volt", "V", ICONS["volt"]),
    ("curr", "Output Current", "curr", "A", ICONS["curr"]),
]

# ---- top metric cards ----
card_w, card_gap, card_y, card_h = 233, 12, 84, 148
cards = []
for i, (kid, ktitle, kvar, kunit, kicon) in enumerate(metrics):
    x = 24 + i * (card_w + card_gap)
    glass = dict(GLASS)
    glass["id"] = f"card_{kid}"
    cards.append({
        "type": "panel", "x": x, "y": card_y, "w": card_w, "h": card_h,
        **glass,
        "children": [
            {"type": "label", "id": f"icon_{kid}", "x": 18, "y": 16, "w": 30, "h": 24,
             "text": kicon, "color": "#5EE6C4", "font": "demo_16"},
            {"type": "label", "id": f"title_{kid}", "x": 56, "y": 18, "w": 160, "h": 20,
             "text": ktitle, "color": "#8FA0BC", "font": "demo_16"},
            {"type": "label", "id": f"val_{kid}", "bind": kvar, "x": 18, "y": 52, "w": 140, "h": 34,
             "font": "demo_20", "align": "left"},
            {"type": "label", "id": f"unit_{kid}", "x": 160, "y": 62, "w": 55, "h": 20,
             "text": kunit, "color": "#8FA0BC", "font": "demo_16"},
        ],
    })

# ---- center gauge panel ----
gauge_panel = {
    "type": "panel", "id": "panel_gauge", "x": 24, "y": 248, "w": 600, "h": 328,
    **GLASS,
    "children": [
        {"type": "label", "id": "gauge_title", "x": 24, "y": 18, "w": 240, "h": 20,
         "text": "MOTOR OUTPUT", "color": "#8FA0BC", "font": "demo_16"},
        {"type": "arc", "id": "arc_power", "bind": "power", "x": 200, "y": 52, "w": 200, "h": 200,
         "min": 0, "max": 100, "color": "#5EE6C4"},
        {"type": "label", "id": "val_power", "bind": "power", "x": 250, "y": 120, "w": 100, "h": 32,
         "font": "demo_20", "align": "center"},
        {"type": "label", "id": "unit_power", "x": 250, "y": 156, "w": 100, "h": 18,
         "text": "%", "color": "#8FA0BC", "font": "demo_16", "align": "center"},
        {"type": "label", "id": "gauge_hint", "x": 24, "y": 284, "w": 380, "h": 18,
         "text": "Long animations, glass cards, staggered entrance", "color": "#5A6A86",
         "font": "demo_16"},
    ],
}

# ---- right column: status + actions ----
status_panel = {
    "type": "panel", "id": "panel_status", "x": 636, "y": 248, "w": 364, "h": 168,
    **GLASS,
    "children": [
        {"type": "label", "id": "status_title", "x": 20, "y": 16, "w": 200, "h": 20,
         "text": "SYSTEM STATUS", "color": "#8FA0BC", "font": "demo_16"},
        {"type": "led", "id": "led_heart", "bind": "heartbeat", "x": 24, "y": 52, "w": 12, "h": 12,
         "color": "#5EE6C4"},
        {"type": "label", "id": "lbl_heart", "x": 48, "y": 48, "w": 200, "h": 20,
         "text": "Communication", "color": "#C7D3E8", "font": "demo_16"},
        {"type": "led", "id": "led_warn", "bind": "warning", "x": 24, "y": 84, "w": 12, "h": 12,
         "color": "#F0B24A"},
        {"type": "label", "id": "lbl_warn", "x": 48, "y": 80, "w": 200, "h": 20,
         "text": "Warnings", "color": "#C7D3E8", "font": "demo_16"},
        {"type": "led", "id": "led_err", "bind": "fault", "x": 24, "y": 116, "w": 12, "h": 12,
         "color": "#E8695E"},
        {"type": "label", "id": "lbl_err", "x": 48, "y": 112, "w": 200, "h": 20,
         "text": "Faults", "color": "#C7D3E8", "font": "demo_16"},
        {"type": "label", "id": "lbl_breath", "x": 220, "y": 112, "w": 130, "h": 20,
         "text": "breathing ->", "color": "#5A6A86", "font": "demo_16"},
    ],
}

action_panel = {
    "type": "panel", "id": "panel_action", "x": 636, "y": 428, "w": 364, "h": 148,
    **GLASS,
    "children": [
        {"type": "label", "id": "action_title", "x": 20, "y": 16, "w": 220, "h": 20,
         "text": "ENTRANCE", "color": "#8FA0BC", "font": "demo_16"},
        {"type": "button", "id": "replay", "x": 20, "y": 52, "w": 150, "h": 72,
         "text": "\uf01e\nReplay", "radius": 14,
         "bg": "#22D3A5", "bgOpa": 220,
         "lv": {"shadow_width": 18, "shadow_spread": 2, "shadow_color": "#22D3A5",
                "shadow_opa": 90},
         "states": {"PRESSED": {"bg": "#2AF0BE"}},
         "events": {"clicked": "entrance"}},
        {"type": "label", "id": "action_hint", "x": 190, "y": 60, "w": 160, "h": 60,
         "text": "Staggered slide + fade, 420ms, ease_out", "color": "#5A6A86",
         "font": "demo_16"},
    ],
}

# ---- background with vertical gradient ----
background = {
    "type": "panel", "id": "bg", "x": 0, "y": 0, "w": W, "h": H,
    "bg": "#0E1524", "radius": 0,
    "lv": {"bg_grad_color": "#05080F", "bg_grad_dir": "VER",
           "bg_main_stop": 60, "bg_grad_stop": 255},
    "children": [
        {"type": "label", "id": "title", "x": 28, "y": 22, "w": 420, "h": 30,
         "text": "Glass Dashboard", "font": "demo_20"},
        {"type": "label", "id": "subtitle", "x": 28, "y": 54, "w": 460, "h": 18,
         "text": "glassmorphism + entrance animation showcase", "color": "#5A6A86",
         "font": "demo_16"},
        *cards, gauge_panel, status_panel, action_panel,
    ],
}

# ---- entrance animation: staggered slide + fade ----
def slide(target, final_y, delay_ms):
    return {"op": "lvgl", "action": "anim", "target": target, "prop": "y",
            "from": final_y + 36, "to": final_y, "time": 420,
            "ease": "ease_out", "delay": delay_ms, "instant": True}

entrance_steps = [
    # background fades in
    {"op": "lvgl", "action": "anim", "target": "bg", "prop": "opacity",
     "from": 90, "to": 255, "time": 300, "ease": "ease_out", "instant": True},
    # metric cards stagger
    slide("card_speed", card_y, 60),
    slide("card_temp", card_y, 130),
    slide("card_volt", card_y, 200),
    slide("card_curr", card_y, 270),
    # main panels
    slide("panel_gauge", 248, 340),
    slide("panel_status", 248, 410),
    slide("panel_action", 428, 480),
    # breathing LED: ping-pong opacity pulse forever (firmware; simulator plays once)
    {"op": "lvgl", "action": "anim", "target": "led_heart", "prop": "opacity",
     "from": 255, "to": 70, "time": 900, "ease": "ease_in_out",
     "repeat": -1, "playback": True, "instant": True},
]

ir = {
    "project": {"name": "glass", "width": W, "height": H, "font": "demo_16"},
    "variables": [
        {"name": "speed", "type": "integer", "default": 1350},
        {"name": "temp", "type": "integer", "default": 52},
        {"name": "volt", "type": "integer", "default": 380},
        {"name": "curr", "type": "double", "default": 8.4},
        {"name": "power", "type": "integer", "default": 72},
        {"name": "heartbeat", "type": "integer", "default": 255},
        {"name": "warning", "type": "integer", "default": 0},
        {"name": "fault", "type": "integer", "default": 0},
    ],
    "widgets": [],
    "screens": [
        {"name": "main", "children": [background]}
    ],
    "actions": [
        {"name": "entrance", "steps": entrance_steps},
    ],
}

d = os.path.dirname(os.path.abspath(__file__))
# Qt-style split (no tr strings -> no strings plane): logic = project + vars +
# actions (firmware contract), screens = layout. Manifest stitches them.
os.makedirs(os.path.join(d, "screens"), exist_ok=True)
uixml.ir_to_xml(
    {"project": ir["project"], "variables": ir["variables"], "actions": ir["actions"]},
    os.path.join(d, "logic.uixml"))
uixml.ir_to_xml({"screens": ir["screens"]},
                os.path.join(d, "screens", "main.uixml"))
MANIFEST = "\n".join([
    '<?xml version="1.0" encoding="utf-8"?>',
    "<!-- glass demo, split form: logic (vars+actions) / screens stitched here. -->",
    "<ui>",
    '  <include src="logic.uixml"/>',
    '  <include src="screens/main.uixml"/>',
    "</ui>",
    "",
])
open(os.path.join(d, "project.uixml"), "w", encoding="utf-8", newline="\n").write(MANIFEST)
print("written: project.uixml + logic.uixml + screens/main.uixml")
