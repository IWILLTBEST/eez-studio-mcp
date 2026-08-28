"""
eez_mcp_server — MCP (Model Context Protocol) server for EEZ Studio

Lets any MCP client (Claude Desktop / Cursor / Continue / DSH, etc.) drive EEZ Studio.

Architecture:
    MCP client ↔ stdio/SSE ↔ this server ↔ HTTP ↔ EEZ Studio bridge (17620)

Usage (Claude Desktop's claude_desktop_config.json):
    {
      "mcpServers": {
        "eez-studio": {
          "command": "python",
          "args": ["<repo>/eez_mcp_server.py"]
        }
      }
    }

Dependencies: pip install mcp

中文：EEZ Studio 的 MCP 服务器，让任何 MCP 客户端操控 EEZ Studio；架构为 MCP 客户端 ↔ stdio/SSE ↔ 本服务器 ↔ HTTP ↔ EEZ Studio 桥(17620)；依赖 pip install mcp。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    EmptyResult,
    GetPromptRequestParams,
    GetPromptResult,
    ImageContent,
    ListPromptsResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    Prompt,
    PromptArgument,
    PromptMessage,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    SubscribeRequestParams,
    TextContent,
    TextResourceContents,
    Tool,
    UnsubscribeRequestParams,
)

from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    ResourceUpdated,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BRIDGE_URL = os.environ.get("EEZ_BRIDGE_URL", "http://127.0.0.1:17620")
WORKDIR = os.environ.get("EEZ_WORKDIR", os.path.dirname(os.path.abspath(__file__)))

# 2026-07-28 protocol: server events are delivered via the subscriptions/listen stream and published to the bus. 2026-07-28 协议：服务端事件经 subscriptions/listen 流下发。
SUBSCRIPTION_BUS = InMemorySubscriptionBus()

server = Server(
    "eez-studio",
    on_subscriptions_listen=ListenHandler(bus=SUBSCRIPTION_BUS),
)

###############################################################################
# Bridge calls 桥调用

async def call_bridge(tool: str, args: dict | None = None) -> Any:
    # trust_env=False: the bridge is 127.0.0.1 loopback; never go through the system proxy.
    # With a VPN/proxy on, local requests get routed to the proxy (~1.7s slower per call) and
    # proxy resolution blocks the event loop, starving background tasks. 中文：仅回环不走代理；开代理时默认多 ~1.7s 且阻塞事件循环。
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        resp = await client.post(
            f"{BRIDGE_URL}/tool",
            json={"tool": tool, "args": args or {}},
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "bridge error"))
        return data.get("result")

async def bridge_health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            resp = await client.get(f"{BRIDGE_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False

###############################################################################
# Tool definitions 工具定义

TOOLS = [
    Tool(
        name="read_ir",
        description="Read the full current IR JSON (the UI description source of the EEZ Studio project). 读取当前 IR JSON 全文。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="write_ir",
        description="Write a complete new IR JSON (full overwrite); must be valid JSON. 写入完整新版 IR JSON（全量覆盖）。",
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Full new IR JSON text 完整的新 IR JSON 文本"}
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="compile",
        description="Compile IR to .eez-project; non-zero exit = failure (validation/glyph check), output contains the errors. 编译 IR → .eez-project，非 0 退出码即失败。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="reload",
        description="Reload the project file in EEZ Studio; required after a successful compile to see the new screen in the editor. 让 EEZ Studio 重新加载工程文件。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="navigate",
        description="Switch EEZ Studio to the given screen (opens its editor; screenshot captures it). 切换到指定屏幕。",
        inputSchema={
            "type": "object",
            "properties": {"screen": {"type": "string", "description": "Screen name 屏幕名"}},
            "required": ["screen"],
        },
    ),
    Tool(
        name="screenshot",
        description="Capture the LVGL preview of the current screen (PNG, returned as an image content block). 截取当前屏幕 LVGL 预览图。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="ping",
        description="Check whether the EEZ Studio bridge is online (returns project status). 检查桥是否在线。",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ---- Output / Checks (build & check errors 构建与检查错误) ----
    Tool(
        name="read_output",
        description=(
            "Read Checks/Output panel messages in EEZ Studio: checks=live background checks "
            "(updated as you edit), output=last build output; each message has "
            "type(error/warning/info), text and object (path of the offending object). "
            "读取底部面板 Checks/Output 消息。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["checks", "output"],
                    "description": "Which section to read, default checks 读哪个 section，默认 checks",
                }
            },
        },
    ),
    Tool(
        name="check",
        description="Run a full project check (waits until done) and return Output-section errors/warnings; use it to verify after editing styles or the project JSON. 触发完整工程检查并返回错误/警告。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="build_project",
        description="Trigger a full EEZ Studio build (Ctrl+B; LVGL projects generate C sources into the build dir), wait for it and return Output-section messages; may take a while. 触发完整构建并等结束，可能较慢。",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ---- Styles / Themes 样式 / 主题 ----
    Tool(
        name="list_styles",
        description=(
            "List the project's LVGL styles (with full definition: part/state/properties), "
            "classic style names and the theme color matrix (actual value of each color per "
            "theme); read this before editing styles. 列出样式/经典样式名/主题颜色矩阵。"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="update_style",
        description=(
            "Update LVGL style properties: set property key-values under "
            "definition[part][state] (e.g. {'bg_color': '#ff0000', 'text_color': "
            "'COLOR_ID_XXX'}); color values may be #rrggbb or theme color names, and null "
            "deletes the property; part defaults to MAIN, state to DEFAULT (auto-uppercased); "
            "auto-saved and effective immediately. 修改样式属性（null 删除，即时生效）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "style": {"type": "string", "description": "LVGL style name LVGL 样式名"},
                "part": {"type": "string", "description": "Widget part, e.g. MAIN/SCROLLBAR 部件，如 MAIN/SCROLLBAR"},
                "state": {"type": "string", "description": "State, e.g. DEFAULT/CHECKED/PRESSED 状态，如 DEFAULT/CHECKED/PRESSED"},
                "properties": {
                    "type": "object",
                    "description": "Property name → value (null = delete) 属性名 → 值（null = 删除）",
                },
            },
            "required": ["style", "properties"],
        },
    ),
    Tool(
        name="create_style",
        description="Create a new LVGL style (forWidgetType defaults to LVGLPanelWidget), then set its properties with update_style. 新建 LVGL 样式。",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "forWidgetType": {"type": "string", "description": "Defaults to LVGLPanelWidget 默认 LVGLPanelWidget"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="delete_style",
        description="Delete an LVGL style (widgets referencing it will fail in check). 删除 LVGL 样式。",
        inputSchema={
            "type": "object",
            "properties": {"style": {"type": "string"}},
            "required": ["style"],
        },
    ),
    Tool(
        name="set_theme_color",
        description=(
            "Set a theme color's value; omitting theme updates all themes at once; styles/"
            "widgets referencing that color name (value = color name instead of #hex) "
            "recolor instantly. 设置主题颜色值（省略 theme 则全部主题一起改）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "color": {"type": "string", "description": "Color slot name (see list_styles) 颜色槽位名"},
                "value": {"type": "string", "description": "#rrggbb"},
                "theme": {"type": "string", "description": "Theme name; omitted = all themes 主题名，省略 = 全部主题"},
            },
            "required": ["color", "value"],
        },
    ),
    Tool(
        name="add_color",
        description="Add a new theme color slot with an initial value in all themes; styles can then reference it by name. 新增主题颜色槽位并赋初值。",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "New color slot name 新颜色槽位名"},
                "value": {"type": "string", "description": "Initial value #rrggbb 初始值 #rrggbb"},
            },
            "required": ["name"],
        },
    ),
    # ---- Direct .eez-project JSON read/write (bypasses IR) 直读直写 ----
    Tool(
        name="read_project_json",
        description="Read the full current .eez-project file directly (bypasses the IR pipeline; for surgical tweaks to project structure/widget properties). 直读 .eez-project 全文。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="write_project_json",
        description=(
            "Write the .eez-project file directly (full overwrite, must be valid JSON), then "
            "auto-reload the project; note: reload trusts the disk file, so unsaved editor "
            "changes are lost. 直写 .eez-project（全量覆盖，自动 reload）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Full new .eez-project JSON 完整的新 .eez-project JSON"},
                "reload": {"type": "boolean", "description": "Default true; reload the project right after writing 默认 true，写完立即重载"},
            },
            "required": ["content"],
        },
    ),
    # ---- Multiple projects 多工程 ----
    Tool(
        name="list_projects",
        description="List all project tabs open in EEZ Studio (index/path/active/loaded/runtime state). 列出打开的工程 tab。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="select_project",
        description="Switch the active project tab (subsequent tools act on it); the parameter may be an index, file name or full path. 切换活动工程 tab。",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {
                    "type": ["string", "integer"],
                    "description": "Index / file name / full path 索引 / 文件名 / 完整路径",
                }
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="open_project",
        description="Open a .eez-project file (new tab; reuses and switches to it if already open) and wait until loaded. 打开 .eez-project 并等加载完成。",
        inputSchema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Full path to the .eez-project file .eez-project 完整路径"}},
            "required": ["path"],
        },
    ),
    # ---- Runtime debugging 运行时调试 ----
    Tool(
        name="debug_start",
        description=(
            "Start the runtime (a local wasm simulator for LVGL projects); mode=debug enables "
            "pause/stepping, mode=run is plain run; startup builds assets and may take tens "
            "of seconds, then view it with screenshot. 启动运行时（可能耗时几十秒）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["debug", "run"], "description": "Default debug 默认 debug"}
            },
        },
    ),
    Tool(
        name="debug_stop",
        description="Stop the runtime and return to edit mode. 停止运行时，回到编辑模式。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="debug_control",
        description="Debug control: pause / resume / step_over|step_into|step_out stepping / restart (stays debugging). 调试控制（暂停/继续/单步/重启）。",
        inputSchema={
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["pause", "resume", "step_over", "step_into", "step_out", "restart"],
                }
            },
            "required": ["op"],
        },
    ),
    Tool(
        name="debug_status",
        description="Query the runtime state (running/paused/stepping) and the tail of recent logs. 查询运行时状态和日志尾部。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="read_variable",
        description="Read a project global variable (two-way synced with the simulator in debug/run mode). 读工程全局变量。",
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    ),
    Tool(
        name="write_variable",
        description="Write a project global variable; value may be any JSON value (number/string/bool/object). 写工程全局变量。",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "value": {"description": "Any JSON value 任意 JSON 值"},
            },
            "required": ["name", "value"],
        },
    ),
    # ---- Object-level editing (path-based CRUD on widgets/pages + undo) 对象级编辑 ----
    Tool(
        name="list_objects",
        description=(
            "List the object tree: no args = page overview; screen=<page name> = that page's "
            "widget tree; path=<object path> = that container's subtree. Nodes carry path/"
            "type/geometry/text/style refs, and path works directly in get_object/"
            "update_object/delete_object. 列对象树（无参=页面总览，screen/path 定位子树）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "screen": {"type": "string", "description": "Page name 页面名"},
                "path": {"type": "string", "description": "Object path (e.g. /userPages/0) 对象路径"},
            },
        },
    ),
    Tool(
        name="get_object",
        description=(
            "Read an object subtree by path or objID (all persisted properties; depth "
            "defaults to 2, deeper levels are given as paths). Path e.g. /userPages/0/"
            "components/0/children/3; objID (bare GUID or objID: prefix, shown in "
            "list_objects/get_object output) is stable while path indices drift when "
            "widgets are added/removed. 按路径或 objID 读对象子树。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Object path or objID 对象路径或 objID"},
                "depth": {"type": "integer", "description": "Nesting levels to expand, default 2 嵌套展开层数，默认 2"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="update_object",
        description=(
            "Surgically update object properties (undoable, auto-saved); path accepts path "
            "or objID (objID is drift-proof). Properties are flat: left/top/width/height/"
            "text/useStyle/hiddenFlag/clickableFlag/value etc., plus one level of dotted "
            "paths (data.text). Verify with navigate+screenshot or read_output(checks). "
            "手术式改对象属性（可 undo，自动保存）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Object path or objID 对象路径或 objID"},
                "properties": {"type": "object", "description": "Property name → new value 属性名 → 新值"},
            },
            "required": ["path", "properties"],
        },
    ),
    Tool(
        name="create_widget",
        description=(
            "Create an LVGL widget; type e.g. LVGLLabelWidget/LVGLButtonWidget/"
            "LVGLPanelWidget/LVGLSliderWidget (an invalid type returns the full list of "
            "valid ones); parent = page name/page path/container widget path; properties "
            "override defaults (left/top/width/height/text...). The widget lands in the "
            "page's ScreenWidget. 新建 LVGL 部件（类型不对会返回可用列表）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Widget class name (LVGL- prefixed) 部件类名"},
                "parent": {"type": "string", "description": "Page name or object path 页面名或对象路径"},
                "properties": {"type": "object", "description": "Initial properties 初始属性"},
            },
            "required": ["type", "parent"],
        },
    ),
    Tool(
        name="delete_object",
        description="Delete an object by path or objID (a widget or a whole page; undoable, auto-saved). 按路径或 objID 删对象。",
        inputSchema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Object path or objID 对象路径或 objID"}},
            "required": ["path"],
        },
    ),
    Tool(
        name="create_screen",
        description="Create a page (auto LVGLScreenWidget root; size defaults to the project display settings). 新建页面。",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="undo",
        description="Undo the last edit (object-level ops on widgets/styles/themes all roll back) and save. 撤销上一次编辑并保存。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="redo",
        description="Redo the undone edit. 重做被撤销的编辑。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="goto_object",
        description="Select and scroll to an object in the editor (path accepts path or objID; jump straight to object paths reported by check). 在编辑器里选中并定位对象。",
        inputSchema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Object path or objID 对象路径或 objID"}},
            "required": ["path"],
        },
    ),
    Tool(
        name="get_selection",
        description=(
            "Read the two-level selection: editorSelection = widget selected in the page "
            "editor (check this after goto_object); panelSelection = navigation panel "
            "selection (the property panel prefers it). Human-in-the-loop: the user clicks "
            "a widget, you get its path/objID. 读两级选中。"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="screenshot_object",
        description=(
            "Screenshot a single widget close-up (the page screenshot cropped to the "
            "widget's absolute rect, returned as an image block); path accepts path or "
            "objID (use it right after get_selection); padding is margin in px on all "
            "sides (default 8); only px-positioned widgets are supported. "
            "截取单个部件特写（padding 默认 8）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Widget path or objID 部件路径或 objID"},
                "padding": {"type": "integer", "description": "Margin in px on all sides, default 8 四周留白像素，默认 8"},
            },
            "required": ["path"],
        },
    ),
    # ---- Simulator input / theme preview / new project / safe patching 模拟器输入/主题预览/新建工程/安全补丁 ----
    Tool(
        name="send_input",
        description=(
            "Inject pointer input into the running simulator (requires debug_start and not "
            "paused); op=click/press/release/swipe; x/y are page coordinates (same frame "
            "as widget left/top, see list_objects); swipe takes dx/dy deltas. Use it to "
            "verify button navigation/scrolling, then check with screenshot or "
            "debug_status(selectedPage). 向模拟器注入指针输入。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["click", "press", "release", "swipe"]},
                "x": {"type": "integer", "description": "Page X coordinate 页面坐标 X"},
                "y": {"type": "integer", "description": "Page Y coordinate 页面坐标 Y"},
                "dx": {"type": "integer", "description": "Swipe horizontal delta swipe 水平位移"},
                "dy": {"type": "integer", "description": "Swipe vertical delta swipe 垂直位移"},
            },
            "required": ["op", "x", "y"],
        },
    ),
    Tool(
        name="set_preview_theme",
        description="Switch the preview theme (edit mode recolors instantly; runtime switches inside wasm); combine with screenshot to verify colors per theme. 切换主题预览。",
        inputSchema={
            "type": "object",
            "properties": {"theme": {"type": "string", "description": "Theme name (see list_styles) 主题名"}},
            "required": ["theme"],
        },
    ),
    Tool(
        name="create_project",
        description=(
            "Programmatically create a minimal LVGL project (with Default theme and Main "
            "page) and open it in a new tab; lvglVersion: 8.4.0/9.2.2/9.3.0/9.4.0/9.5.0 "
            "(default 9.5.0), size defaults to 800x480. 程序化新建最小 LVGL 工程并打开。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Full path of the new project (must end with .eez-project and must not exist) 新工程完整路径"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "lvglVersion": {"type": "string"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="list_assets",
        description=(
            "List assets: custom fonts (bpp/size/ranges/symbols/source file), reserved "
            "built-in Montserrat names (MONTSERRAT_8..48 usable directly as text_font, no "
            "font build needed), and bitmaps (with bpp/source). 列出字体/内置 Montserrat/位图。"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="add_font",
        description=(
            "Create an LVGL font from a TTF (Studio bundles lv_font_conv, no external "
            "command); ranges e.g. '32-127' (comma-separated segments); symbols is a "
            "literal per-character string (Chinese/icon glyphs); afterwards set the "
            "style's text_font to the font name; may take a dozen seconds (progress "
            "notifications supported). 从 TTF 新建 LVGL 字体。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Font name (unique) 字体名（唯一）"},
                "ttf_path": {"type": "string", "description": "Full path to the TTF file TTF 文件完整路径"},
                "size": {"type": "integer", "description": "Font size in pixels, default 16 字号（像素），默认 16"},
                "bpp": {"type": "integer", "enum": [1, 2, 4, 8], "description": "Default 4 默认 4"},
                "ranges": {"type": "string", "description": "Unicode ranges, default 32-127 Unicode 区间，默认 32-127"},
                "symbols": {"type": "string", "description": "Literal per-character string, e.g. '温度转速报警' 逐字符集"},
            },
            "required": ["name", "ttf_path", "size"],
        },
    ),
    Tool(
        name="add_image",
        description=(
            "Import an image (PNG/JPG/BMP/GIF) as a project bitmap; in non-embed mode it "
            "is copied into the project's images/ dir; returns the name to fill into "
            "create_widget(LVGLImageWidget)'s image property. 导入图片为工程位图。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Full path to the image file 图片文件完整路径"},
                "name": {"type": "string", "description": "Bitmap name (defaults to the file name, auto-dedup) 位图名"},
                "bpp": {"type": "integer", "description": "LVGL color format, default 32 (CF_TRUE_COLOR_ALPHA) LVGL 色彩格式，缺省 32"},
            },
            "required": ["image_path"],
        },
    ),
    Tool(
        name="patch_project_json",
        description=(
            "Patch the current project's .eez-project JSON, write it back and auto-reload "
            "(safer than write_project_json's full overwrite); mode=merge (default, "
            "RFC 7396 deep merge: objects merge recursively, null deletes a key, arrays "
            "are replaced wholesale) or mode=jsonpatch (RFC 6902 op array: add/remove/"
            "replace/move/copy/test, paths use JSON Pointer); suited to large structural "
            "changes — prefer update_object for small edits. 对工程 JSON 打补丁并自动 reload。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patch": {
                    "description": "merge mode = patch object; jsonpatch mode = op array merge 模式=补丁对象；jsonpatch 模式=操作数组",
                },
                "mode": {"type": "string", "enum": ["merge", "jsonpatch"], "description": "Default merge 默认 merge"},
            },
            "required": ["patch"],
        },
    ),
]

###############################################################################
# Live resources (eez://): fetched on demand from the bridge, subscribable (resources/updated pushed on change) 活资源，可订阅

LIVE_RESOURCES = {
    "eez://checks": ("Live checks 实时检查", "Error/warning counts and messages (pushed on change once subscribed) 错误/警告计数与消息（订阅后变更即推送）"),
    "eez://debug": ("Runtime state 运行时状态", "Debugger state, current page and log tail (pushed on change once subscribed) 调试器状态、当前页与日志尾部（订阅后变更即推送）"),
    "eez://state": ("Project state 工程状态", "Active project and selection (pushed on change once subscribed) 活动工程与选中对象（订阅后变更即推送）"),
}

_subscribed_uris: set[str] = set()
_resource_hashes: dict[str, str] = {}
_session_ref: dict = {"session": None}

async def live_resource_content(uri: str) -> str:
    if uri == "eez://checks":
        r = await call_bridge("read_output", {"section": "checks"})
        return json.dumps(r, ensure_ascii=False, default=str)
    if uri == "eez://debug":
        r = await call_bridge("debug_status")
        return json.dumps(r, ensure_ascii=False, default=str)
    if uri == "eez://state":
        r = await call_bridge("ping")
        sel = await call_bridge("get_selection")
        return json.dumps({"ping": r, "selection": sel}, ensure_ascii=False, default=str)
    raise ValueError(f"Unknown resource: {uri}")

async def _resource_watcher() -> None:
    """Poll live resources and publish ResourceUpdated events on content change (bus → subscriptions/listen stream).

    轮询活资源，内容变化时发布 ResourceUpdated 事件。"""
    try:
        while True:
            await asyncio.sleep(2)
            has_bus_listeners = bool(getattr(SUBSCRIPTION_BUS, "_listeners", None))
            if not (has_bus_listeners or _subscribed_uris):
                continue
            # Only poll the URIs we need: legacy subscriptions + all (when bus listeners exist). 仅轮询需要的 URI。
            # Each bridge call takes ~2.5s; gather parallelizes to cut the cycle. 每次桥调用 ~2.5s，用 gather 并行。
            uris = set(_subscribed_uris)
            if has_bus_listeners:
                uris.update(LIVE_RESOURCES.keys())
            results = await asyncio.gather(
                *[live_resource_content(u) for u in uris], return_exceptions=True
            )
            for uri, content in zip(uris, results):
                if isinstance(content, BaseException):
                    continue  # e.g. bridge offline 桥不在线等场景
                h = hashlib.md5(content.encode("utf-8", "ignore")).hexdigest()
                prev = _resource_hashes.get(uri)
                _resource_hashes[uri] = h
                if prev is not None and prev != h:
                    _dbg(f"{uri} changed, publishing event")
                    try:
                        await SUBSCRIPTION_BUS.publish(ResourceUpdated(uri=uri))
                    except Exception as e:
                        _dbg(f"bus publish failed: {e}")
                    # Legacy-protocol clients (resources/subscribe path) 兼容旧协议客户端
                    if uri in _subscribed_uris:
                        session = _session_ref["session"]
                        if session:
                            try:
                                await session.send_resource_updated(uri)
                            except Exception as e:
                                _dbg(f"legacy push failed: {e}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _dbg(f"watcher crashed: {type(e).__name__} {e}")


def _dbg(msg: str) -> None:
    """Debug log to stderr (the stdio protocol uses stdout; stderr is safe). 调试日志到 stderr。"""
    try:
        print(f"[watcher] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass

async def handle_subscribe(ctx, params: SubscribeRequestParams) -> EmptyResult:
    _session_ref["session"] = ctx.session
    _subscribed_uris.add(str(params.uri))
    return EmptyResult()

async def handle_unsubscribe(ctx, params: UnsubscribeRequestParams) -> EmptyResult:
    uri = str(params.uri)
    _subscribed_uris.discard(uri)
    _resource_hashes.pop(uri, None)
    return EmptyResult()

async def handle_discover(ctx, params) -> Any:
    """server/discover (2026-07-28 entry point): declare support for the new protocol so
    clients can negotiate the subscriptions/listen stream. The low-level Server does not
    register it automatically, so we add it here.

    宣告支持新版协议（subscriptions/listen 流）；低层 Server 不自动注册，这里补上。"""
    from mcp.types import (
        Capabilities,
        DiscoverResult,
        ResourcesCapability,
        ServerCapabilities,
        ToolsCapability,
    )

    return DiscoverResult(
        supported_versions=["2026-07-28"],
        capabilities=ServerCapabilities(
            tools=ToolsCapability(list_changed=False),
            resources=ResourcesCapability(subscribe=True, list_changed=False),
        ),
    )

###############################################################################
# Resource definitions 资源定义

async def list_resources() -> list[Resource]:
    ir_path = Path(WORKDIR) / "sg8.ir.json"
    resources = [
        Resource(
            uri=f"file://{ir_path}",
            name="Current IR JSON 当前 IR JSON",
            description="UI description source file of the EEZ Studio project 工程界面描述源文件",
            mimeType="application/json",
        ),
        Resource(
            uri=f"file://{WORKDIR}/IR_SCHEMA.md",
            name="IR format spec IR 格式规范",
            description="Structure and EEZ constraints of the IR JSON IR 结构定义与约束",
            mimeType="text/markdown",
        ),
        Resource(
            uri=f"file://{WORKDIR}/SKILL.md",
            name="EEZ Studio skill doc EEZ Studio 技能文档",
            description="Heuristic rules for generating EEZ Studio LVGL UIs LVGL 界面生成经验规则",
            mimeType="text/markdown",
        ),
    ]
    for uri, (name, desc) in LIVE_RESOURCES.items():
        resources.append(
            Resource(uri=uri, name=name, description=desc, mimeType="application/json")
        )
    return resources

###############################################################################
# Prompt definitions 提示词定义

async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="modify_ui",
            description="Modify an EEZ Studio LVGL UI 修改 EEZ Studio LVGL 界面",
            arguments=[
                PromptArgument(name="requirement", description="What to change 要改什么", required=True),
            ],
        ),
        Prompt(
            name="create_ui",
            description="Create a new EEZ Studio LVGL UI from a design HTML 从设计稿 HTML 创建 LVGL 界面",
            arguments=[
                PromptArgument(name="html_path", description="Path to the design HTML file 设计稿 HTML 文件路径", required=True),
            ],
        ),
    ]

async def get_prompt(name: str, arguments: dict) -> GetPromptResult:
    schema_text = Path(WORKDIR, "IR_SCHEMA.md").read_text(encoding="utf-8")[:5000]
    skill_text = Path(WORKDIR, "SKILL.md").read_text(encoding="utf-8")[:3000]

    common = f"""== EEZ Studio LVGL Tools (via MCP bridge) ==
== EEZ Studio LVGL 工具（经 MCP 桥调用）==

工作流：read_ir → 修改 → compile → reload → navigate → screenshot

其它能力：
- 错误诊断：read_output(checks/output)、check、build_project、goto_object（跳到出错对象）
- 部件级编辑：list_objects → get_object → update_object / create_widget / delete_object / create_screen（按路径手术式改，undo 可回滚；小改动优先用这组，别全量重写）
- 样式/主题：list_styles、update_style、create_style、delete_style、set_theme_color、add_color（改完即时生效，无需 reload）
- 工程文件直改：read_project_json / write_project_json（绕过 IR，写完自动 reload；仅大结构变更用）
- 多工程：list_projects、select_project、open_project（工具作用于活动 tab）
- 运行时调试：debug_start → send_input 点击/滑动验证交互、screenshot 看模拟画面、debug_control(pause/resume/单步)、read/write_variable、debug_status、debug_stop
- 主题预览：set_preview_theme + screenshot 逐主题验证配色
- 新工程：create_project（最小 LVGL 模板）；结构补丁：patch_project_json（merge/jsonpatch）

== IR Spec (excerpt) ==
== IR 规范（摘要）==
{schema_text}

== Heuristic Rules (excerpt) ==
== 经验规则（摘要）==
{skill_text}"""

    if name == "modify_ui":
        req = arguments.get("requirement", "")
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"{common}\n\n用户需求：{req}\n\n先用 read_ir 读当前 IR，然后用内置 edit 工具做手术式修改（小改动），改完 compile → reload → navigate → screenshot 自查。",
                    ),
                )
            ]
        )
    elif name == "create_ui":
        html_path = arguments.get("html_path", "")
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"{common}\n\n设计稿：{html_path}\n\n读取设计稿 HTML，分析布局/颜色/文字/交互，从零创建 IR JSON（每个卡片区域用 panel 包裹），然后 compile → reload → navigate → screenshot 自查。",
                    ),
                )
            ]
        )
    return GetPromptResult(messages=[])

###############################################################################
# Minimal JSON Patch (RFC 6902) & Merge Patch (RFC 7396) implementations (no third-party deps) 最小实现

def _json_pointer_tokens(ptr: str) -> list[str]:
    if ptr == "":
        return []
    if not ptr.startswith("/"):
        raise ValueError(f"Invalid JSON Pointer (must start with / or be empty): {ptr}")
    return [t.replace("~1", "/").replace("~0", "~") for t in ptr.split("/")[1:]]

def _walk(doc, tokens):
    """Walk down to the second-to-last level and return (parent, last_token). 走到倒数第二级，返回 (parent, last_token)"""
    cur = doc
    for t in tokens[:-1]:
        if isinstance(cur, list):
            cur = cur[int(t)]
        elif isinstance(cur, dict):
            cur = cur[t]
        else:
            raise ValueError(f"Path traverses a non-container: {t}")
    return cur, tokens[-1]

def _get_at(doc, tokens):
    if not tokens:
        return doc
    parent, last = _walk(doc, tokens)
    if isinstance(parent, list):
        return parent[int(last)]
    return parent[last]

def _remove_at(doc, tokens):
    parent, last = _walk(doc, tokens)
    if isinstance(parent, list):
        parent.pop(int(last))
    else:
        del parent[last]

def _add_at(doc, tokens, value):
    if not tokens:
        return value
    parent, last = _walk(doc, tokens)
    if isinstance(parent, list):
        idx = len(parent) if last == "-" else int(last)
        parent.insert(idx, value)
    else:
        parent[last] = value
    return doc

def apply_json_patch(doc, ops: list) -> Any:
    import copy

    if not isinstance(ops, list):
        raise ValueError("patch in jsonpatch mode must be an array of operations")
    for op in ops:
        kind = op.get("op")
        tokens = _json_pointer_tokens(op.get("path", ""))
        if kind == "add":
            doc = _add_at(doc, tokens, copy.deepcopy(op.get("value")))
        elif kind == "replace":
            _get_at(doc, tokens)  # raises KeyError/IndexError if absent 不存在会抛 KeyError/IndexError
            doc = _add_at(doc, tokens, copy.deepcopy(op.get("value")))
        elif kind == "remove":
            _remove_at(doc, tokens)
        elif kind in ("move", "copy"):
            src_tokens = _json_pointer_tokens(op.get("from", ""))
            value = copy.deepcopy(_get_at(doc, src_tokens))
            if kind == "move":
                _remove_at(doc, src_tokens)
            doc = _add_at(doc, tokens, value)
        elif kind == "test":
            if _get_at(doc, tokens) != op.get("value"):
                raise ValueError(f"test failed: {op.get('path')}")
        else:
            raise ValueError(f"Unknown op: {kind}")
    return doc

def apply_merge_patch(target, patch) -> Any:
    from copy import deepcopy

    if not isinstance(patch, dict):
        return deepcopy(patch)
    if not isinstance(target, dict):
        target = {}
    for k, v in patch.items():
        if v is None:
            target.pop(k, None)
        else:
            target[k] = apply_merge_patch(target.get(k), v)
    return target

async def patch_project_json_tool(arguments: dict) -> list:
    mode = arguments.get("mode", "merge")
    patch = arguments.get("patch")
    ping = await call_bridge("ping")
    project_file = ping.get("projectFile") if isinstance(ping, dict) else None
    if not project_file:
        raise RuntimeError("No project is open in EEZ Studio")

    doc = json.loads(Path(project_file).read_text(encoding="utf-8"))
    if mode == "merge":
        if not isinstance(patch, dict):
            raise ValueError("patch in merge mode must be an object")
        result = apply_merge_patch(doc, patch)
    elif mode == "jsonpatch":
        result = apply_json_patch(doc, patch)
    else:
        raise ValueError(f"Unknown mode: {mode} (merge or jsonpatch)")

    content = json.dumps(result, ensure_ascii=False, indent=2)
    bridge_result = await call_bridge(
        "write_project_json", {"content": content}
    )
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "ok": True,
                    "mode": mode,
                    "projectFile": project_file,
                    "bytes": len(content),
                    "detail": bridge_result,
                },
                ensure_ascii=False,
            ),
        )
    ]

###############################################################################
# Tool execution 工具执行

async def call_tool(name: str, arguments: dict) -> list:
    if name == "ping":
        result = await call_bridge("ping")
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    if name == "screenshot":
        result = await call_bridge("screenshot")
        data_url = result["dataUrl"]
        base64_data = data_url.split(",", 1)[1] if "," in data_url else data_url
        return [
            TextContent(type="text", text=f"screenshot saved: {result['file']}"),
            ImageContent(type="image", data=base64_data, mimeType="image/png"),
        ]

    if name == "read_ir":
        result = await call_bridge("read_ir")
        text = str(result)[:50000]  # Cap length 防超长
        return [TextContent(type="text", text=text)]

    if name == "write_ir":
        result = await call_bridge("write_ir", {"content": arguments["content"]})
        return [TextContent(type="text", text=str(result))]

    if name == "compile":
        result = await call_bridge("compile")
        ok = result.get("ok", False)
        output = result.get("output", "")
        status = "✓ compile OK" if ok else "✗ compile FAILED"
        return [TextContent(type="text", text=f"{status}\n{output[:5000]}")]

    if name == "reload":
        result = await call_bridge("reload")
        return [TextContent(type="text", text=str(result))]

    if name == "navigate":
        result = await call_bridge("navigate", {"screen": arguments["screen"]})
        return [TextContent(type="text", text=str(result))]

    if name == "screenshot_object":
        result = await call_bridge(
            "screenshot_object",
            {
                "path": arguments["path"],
                "padding": arguments.get("padding", 8),
            },
        )
        data_url = result["dataUrl"]
        base64_data = data_url.split(",", 1)[1] if "," in data_url else data_url
        return [
            TextContent(
                type="text",
                text=f"widget screenshot {result['rect']}: {result['file']}",
            ),
            ImageContent(type="image", data=base64_data, mimeType="image/png"),
        ]

    if name == "read_project_json":
        result = await call_bridge("read_project_json")
        text = str(result)[:50000]  # Cap length 防超长
        return [TextContent(type="text", text=text)]

    if name == "write_project_json":
        result = await call_bridge(
            "write_project_json",
            {"content": arguments["content"], "reload": arguments.get("reload", True)},
        )
        return [TextContent(type="text", text=str(result))]

    if name == "patch_project_json":
        return await patch_project_json_tool(arguments)

    # Remaining tools: pass arguments through to the bridge, return the JSON-serialized result 其余工具参数透传给桥
    passthrough = {
        "read_output": ["section"],
        "check": [],
        "build_project": [],
        "list_styles": [],
        "update_style": ["style", "part", "state", "properties"],
        "create_style": ["name", "forWidgetType"],
        "delete_style": ["style"],
        "set_theme_color": ["color", "value", "theme"],
        "add_color": ["name", "value"],
        "list_projects": [],
        "select_project": ["project"],
        "open_project": ["path"],
        "debug_start": ["mode"],
        "debug_stop": [],
        "debug_control": ["op"],
        "debug_status": [],
        "read_variable": ["name"],
        "write_variable": ["name", "value"],
        "list_objects": ["screen", "path"],
        "get_object": ["path", "depth"],
        "update_object": ["path", "properties"],
        "create_widget": ["type", "parent", "properties"],
        "delete_object": ["path"],
        "create_screen": ["name", "width", "height"],
        "undo": [],
        "redo": [],
        "goto_object": ["path"],
        "get_selection": [],
        "send_input": ["op", "x", "y", "dx", "dy"],
        "set_preview_theme": ["theme"],
        "create_project": ["path", "width", "height", "lvglVersion"],
        "list_assets": [],
        "add_font": ["name", "ttf_path", "size", "bpp", "ranges", "symbols"],
        "add_image": ["image_path", "name", "bpp"],
    }
    if name in passthrough:
        args = {k: arguments[k] for k in passthrough[name] if k in arguments}
        result = await call_bridge(name, args)
        return [
            TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False, default=str)
            )
        ]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]

###############################################################################
# MCP server handlers (mcp 2.0 low-level API: add_request_handler, handler signature (ctx, params)) MCP 服务器处理器

async def handle_list_tools(ctx, params) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)

async def handle_list_resources(ctx, params) -> ListResourcesResult:
    return ListResourcesResult(resources=await list_resources())

async def handle_list_prompts(ctx, params) -> ListPromptsResult:
    return ListPromptsResult(prompts=await list_prompts())

async def handle_read_resource(ctx, params: ReadResourceRequestParams) -> ReadResourceResult:
    _session_ref["session"] = ctx.session
    uri = str(params.uri)
    if uri.startswith("eez://"):
        try:
            text = await live_resource_content(uri)
        except Exception as e:
            text = f"read failed: {e}"
        return ReadResourceResult(
            contents=[
                TextResourceContents(uri=params.uri, mimeType="application/json", text=text[:50000])
            ]
        )
    path = uri.replace("file:///", "").replace("file://", "")
    try:
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=params.uri, mimeType="text/plain",
                    text=Path(path).read_text(encoding="utf-8")[:50000],
                )
            ]
        )
    except Exception as e:
        return ReadResourceResult(
            contents=[TextResourceContents(uri=params.uri, mimeType="text/plain", text=f"read failed: {e}")]
        )

async def handle_get_prompt(ctx, params: GetPromptRequestParams) -> GetPromptResult:
    return await get_prompt(params.name, dict(params.arguments or {}))

###############################################################################
# Progress notifications for long operations (enabled when the client passes a token in _meta.progressToken) 长操作进度通知

LONG_TOOLS = {
    "check",
    "build_project",
    "debug_start",
    "compile",
    "write_project_json",
    "patch_project_json",
    "add_font",
}

def _extract_progress_token(params: CallToolRequestParams):
    # The client may normalize _meta into snake_case progress_token (camelCase can also arrive) 客户端会把 _meta 归一化为 progress_token
    meta = getattr(params, "meta", None)
    if isinstance(meta, dict):
        return meta.get("progressToken") or meta.get("progress_token")
    return getattr(meta, "progress_token", None) if meta is not None else None

async def _run_with_progress(ctx, token, name: str, coro):
    async def ticker():
        start = time.monotonic()
        last_sent = 0
        while True:
            await asyncio.sleep(0.3)
            elapsed = max(1, int(time.monotonic() - start))
            if elapsed > last_sent:
                last_sent = elapsed
                try:
                    await ctx.session.send_progress_notification(
                        token, elapsed, message=f"{name}: waited {elapsed}s"
                    )
                except Exception:
                    pass

    task = asyncio.create_task(ticker())
    try:
        return await coro
    finally:
        task.cancel()

async def handle_call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
    _session_ref["session"] = ctx.session
    name = params.name
    arguments = dict(params.arguments or {})
    try:
        coro = call_tool(name, arguments)
        token = _extract_progress_token(params)
        if name in LONG_TOOLS and token is not None:
            contents = await _run_with_progress(ctx, token, name, coro)
        else:
            contents = await coro
        return CallToolResult(content=contents)
    except Exception as e:
        error_hint = ""
        if "Connect" in str(e) or "connect" in str(e):
            error_hint = "\nHint: EEZ Studio may not be running; please start EEZ Studio first."
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {e}{error_hint}")],
            isError=True,
        )


server.add_request_handler("tools/list", PaginatedRequestParams, handle_list_tools)
server.add_request_handler("tools/call", CallToolRequestParams, handle_call_tool)
server.add_request_handler("resources/list", PaginatedRequestParams, handle_list_resources)
server.add_request_handler("resources/read", ReadResourceRequestParams, handle_read_resource)
server.add_request_handler("resources/subscribe", SubscribeRequestParams, handle_subscribe)
server.add_request_handler("resources/unsubscribe", UnsubscribeRequestParams, handle_unsubscribe)
server.add_request_handler("server/discover", PaginatedRequestParams, handle_discover)
server.add_request_handler("prompts/list", PaginatedRequestParams, handle_list_prompts)
server.add_request_handler("prompts/get", GetPromptRequestParams, handle_get_prompt)

###############################################################################
# Main entry 主入口

async def main():
    watcher = asyncio.create_task(_resource_watcher())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
    watcher.cancel()

if __name__ == "__main__":
    asyncio.run(main())
