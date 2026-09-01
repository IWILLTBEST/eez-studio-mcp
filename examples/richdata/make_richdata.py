#!/usr/bin/env python3
"""Rich data widgets demo: Roller (fully compiled — options + bound selected),
Table and Chart (bare LVGL objects; structure exported to ui_ext.h constants
for firmware runtime setup). 富数据部件演示：滚轮完整编译；表格/图表结构进 ui_ext.h。"""
import json
import os

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

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "richdata.ir.json")
with open(out, "w", encoding="utf-8", newline="") as f:
    json.dump(ir, f, ensure_ascii=False, indent=1)
print("written:", out)
