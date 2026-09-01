#!/usr/bin/env python3
"""Minimal i18n demo: labels with tr:"key" compile to T"key" expressions
(lvgl i18n via upstream eez-open/studio#1045); the canvas preview shows the
default-language text; translations.yaml (lv_i18n format) lands next to the
.eez-project. Change strings.default to "zh" and recompile to preview the
other language — the firmware stays key-driven.

最小 i18n 演示：tr 标签 → T"key" 表达式，画布显示默认语言译文；
translations.yaml 为 lv_i18n 格式。切换 strings.default 重编译即可预览另一语言。
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import uixml

W, H = 480, 320

ir = {
    "project": {"name": "i18n-demo", "width": W, "height": H, "font": "cn_24"},
    "strings": {
        "default": "en",
        "texts": {
            "title":       {"en": "Motor Controller", "zh": "电机控制器"},
            "speed":       {"en": "Speed", "zh": "转速"},
            "temperature": {"en": "Temperature", "zh": "温度"},
            "start":       {"en": "Start", "zh": "启动"},
            "stop":        {"en": "Stop", "zh": "停止"},
            "status_ok":   {"en": "All systems nominal", "zh": "系统一切正常"},
        },
    },
    "variables": [
        {"name": "speed_val", "type": "integer", "default": 1350},
    ],
    "screens": [{
        "name": "main",
        "children": [{
            "type": "panel", "id": "root", "x": 0, "y": 0, "w": W, "h": H,
            "bg": "#101828", "radius": 0,
            "children": [
                {"type": "label", "id": "title", "tr": "title",
                 "x": 24, "y": 20, "w": 360, "h": 34, "font": "cn_24",
                 "color": "#E8EFFA"},
                {"type": "label", "id": "row_speed", "tr": "speed",
                 "x": 24, "y": 84, "w": 150, "h": 26, "color": "#8FA0BC"},
                {"type": "label", "id": "val_speed", "bind": "speed_val",
                 "x": 210, "y": 84, "w": 120, "h": 26, "color": "#5EE6C4"},
                {"type": "label", "id": "row_temp", "tr": "temperature",
                 "x": 24, "y": 124, "w": 150, "h": 26, "color": "#8FA0BC"},
                {"type": "label", "id": "status", "tr": "status_ok",
                 "x": 24, "y": 168, "w": 400, "h": 26, "color": "#5A6A86"},
                {"type": "button", "id": "btn_start", "text": "",
                 "x": 24, "y": 220, "w": 90, "h": 48, "radius": 10,
                 "bg": "#1E7F5C", "color": "#FFFFFF"},
                {"type": "label", "id": "btn_start_lbl", "tr": "start",
                 "x": 24, "y": 233, "w": 90, "h": 24, "color": "#FFFFFF",
                 "align": "center", "font": "cn_24"},
                {"type": "button", "id": "btn_stop", "text": "",
                 "x": 130, "y": 220, "w": 90, "h": 48, "radius": 10,
                 "bg": "#7A2E3A", "color": "#FFFFFF"},
                {"type": "label", "id": "btn_stop_lbl", "tr": "stop",
                 "x": 130, "y": 233, "w": 90, "h": 24, "color": "#FFFFFF",
                 "align": "center", "font": "cn_24"},
            ],
        }],
    }],
}

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n.uixml")
uixml.ir_to_xml(ir, out)
print("written:", out)
