#!/usr/bin/env python3
"""Rich data widgets demo: Roller (fully compiled — options + bound selected),
Table and Chart (bare LVGL objects; structure exported to ui_ext.h constants
for firmware runtime setup). 富数据部件演示：滚轮完整编译；表格/图表结构进 ui_ext.h。"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import uixml

W, H = 480, 640

ir = {
    "project": {"name": "richdata-demo", "width": W, "height": H, "font": "demo_16"},
    "variables": [
        {"name": "mode_idx", "type": "integer", "default": 1},
    ],
    "screens": [{
        "name": "main",
        "children": [{
            "type": "panel", "id": "root", "x": 0, "y": 0, "w": W, "h": H,
            "bg": "#101828",
            "children": [
                {"type": "label", "id": "title", "x": 24, "y": 20, "w": 300, "h": 24,
                 "text": "Rich Data Demo", "font": "demo_20", "color": "#E8EFFA"},

                # --- Roller: options compile into the project, selected is bound ---
                {"type": "label", "id": "lbl_mode", "x": 24, "y": 64, "w": 200, "h": 18,
                 "text": "Control mode (roller)", "color": "#8FA0BC"},
                {"type": "roller", "id": "mode", "bind": "mode_idx",
                 "options": ["Auto", "Manual", "Service", "Bootstrap"],
                 "x": 24, "y": 90, "w": 170, "h": 92, "bg": "#1A2438"},

                # --- Chart: bare object + ui_ext.h constants ---
                {"type": "label", "id": "lbl_chart", "x": 24, "y": 200, "w": 300, "h": 18,
                 "text": "Bus current (chart)", "color": "#8FA0BC"},
                {"type": "chart", "id": "bus", "kind": "line",
                 "min": 0, "max": 400, "points": 120,
                 "series": [
                     {"name": "Ibus", "color": "#5EE6C4", "width": 2},
                     {"name": "Limit", "color": "#F2B84B", "width": 1},
                 ],
                 "x": 24, "y": 226, "w": 432, "h": 160, "bg": "#0B1220"},

                # --- Table: bare object + ui_ext.h constants + header array ---
                {"type": "label", "id": "lbl_tbl", "x": 24, "y": 404, "w": 300, "h": 18,
                 "text": "Event log (table)", "color": "#8FA0BC"},
                {"type": "table", "id": "events",
                 "cols": 3, "rows": 5, "header": ["Time", "Code", "Message"],
                 "x": 24, "y": 430, "w": 432, "h": 150, "bg": "#0B1220"},

                {"type": "label", "id": "hint", "x": 24, "y": 600, "w": 432, "h": 18,
                 "text": "chart/table structure: runtime C via ui_ext.h", "color": "#5A6A86"},
            ],
        }],
    }],
    "actions": [
        {"name": "on_mode_change",
         "steps": [{"op": "set", "variable": "mode_idx", "value": "mode_idx"}]},
    ],
}

# wire the roller event (value_changed fires when the user scrolls)
ir["screens"][0]["children"][0]["children"][2]["events"] = {"value_changed": "on_mode_change"}

# --- screen 2: scale / calendar / spinbox / keyboard ---
ir["screens"].append({
    "name": "controls",
    "children": [{
        "type": "panel", "id": "root2", "x": 0, "y": 0, "w": W, "h": H,
        "bg": "#101828",
        "children": [
            {"type": "label", "id": "title2", "x": 24, "y": 16, "w": 300, "h": 24,
             "text": "Controls Demo", "font": "demo_20", "color": "#E8EFFA"},

            # --- Scale (LVGL 9 gauge; lv_meter replacement) ---
            {"type": "label", "id": "lbl_scale", "x": 24, "y": 52, "w": 200, "h": 18,
             "text": "RPM scale", "color": "#8FA0BC"},
            {"type": "scale", "id": "rpm", "mode": "round_inner",
             "min": 0, "max": 3000, "angle": 270, "rotate": 135,
             "ticks": 11, "major": 5,
             "sections": [
                 {"from": 0, "to": 2200, "color": "#3A4B66", "width": 8},
                 {"from": 2200, "to": 2600, "color": "#F2B84B", "width": 8},
                 {"from": 2600, "to": 3000, "color": "#E5484D", "width": 8}],
             "x": 24, "y": 74, "w": 180, "h": 180},

            # --- Calendar ---
            {"type": "calendar", "id": "cal", "today": "2026-09-01",
             "header": "arrow",
             "x": 240, "y": 74, "w": 210, "h": 210},

            # --- Spinbox (bound value) ---
            {"type": "label", "id": "lbl_count", "x": 24, "y": 286, "w": 200, "h": 18,
             "text": "Pulse count", "color": "#8FA0BC"},
            {"type": "spinbox", "id": "count", "bind": "mode_idx",
             "min": 0, "max": 9999, "digits": 4,
             "x": 24, "y": 308, "w": 130, "h": 46, "bg": "#1A2438"},

            # --- Keyboard + textarea ---
            {"type": "textarea", "id": "input", "text": "",
             "x": 24, "y": 374, "w": 432, "h": 44, "bg": "#0B1220"},
            {"type": "keyboard", "id": "kb", "textarea": "input",
             "mode": "number",
             "x": 24, "y": 426, "w": 432, "h": 190},
        ],
    }],
})
ir["variables"].append({"name": "pulse_count", "type": "integer", "default": 42})
ir["screens"][1]["children"][0]["children"][5]["bind"] = "pulse_count"

# --- screen 3: tabview (EEZ-native tabs, bindable selectedTab) ---
ir["screens"].append({
    "name": "settings",
    "children": [{
        "type": "panel", "id": "root3", "x": 0, "y": 0, "w": W, "h": H,
        "bg": "#101828",
        "children": [
            {"type": "tabview", "id": "cfg", "bind": "mode_idx",
             "position": "top", "barSize": 44,
             "x": 16, "y": 16, "w": 448, "h": 420,
             "events": {"value_changed": "on_mode_change"},
             "tabs": [
                 {"title": "Display", "children": [
                     {"type": "label", "id": "t1a", "x": 16, "y": 16, "w": 200, "h": 20,
                      "text": "Brightness", "color": "#8FA0BC"},
                     {"type": "slider", "id": "bright", "x": 16, "y": 44, "w": 320, "h": 12},
                     {"type": "label", "id": "t1b", "x": 16, "y": 76, "w": 200, "h": 20,
                      "text": "Theme: Dark", "color": "#5EE6C4"},
                 ]},
                 {"title": "Network", "children": [
                     {"type": "label", "id": "t2a", "x": 16, "y": 16, "w": 300, "h": 20,
                      "text": "Host: 192.168.1.10", "color": "#8FA0BC"},
                     {"type": "label", "id": "t2b", "x": 16, "y": 44, "w": 300, "h": 20,
                      "text": "Port: 502", "color": "#8FA0BC"},
                     {"type": "switch", "id": "dhcp", "x": 340, "y": 12, "w": 50, "h": 25},
                 ]},
             ]},
        ],
    }],
})

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "richdata.uixml")
uixml.ir_to_xml(ir, out)
print("written:", out)
