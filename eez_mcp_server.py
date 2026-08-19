"""
eez_mcp_server — EEZ Studio 的 MCP (Model Context Protocol) 服务器

让任何 MCP 客户端（Claude Desktop / Cursor / Continue / DSH 等）操控 EEZ Studio。

架构：
    MCP 客户端 ↔ stdio/SSE ↔ 本服务器 ↔ HTTP ↔ EEZ Studio 桥(17620)

用法（Claude Desktop 的 claude_desktop_config.json）：
    {
      "mcpServers": {
        "eez-studio": {
          "command": "python",
          "args": ["<repo>/eez_mcp_server.py"]
        }
      }
    }

依赖：pip install mcp
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

# 2026-07-28 协议：服务端事件经 subscriptions/listen 流下发，发布到 bus
SUBSCRIPTION_BUS = InMemorySubscriptionBus()

server = Server(
    "eez-studio",
    on_subscriptions_listen=ListenHandler(bus=SUBSCRIPTION_BUS),
)

###############################################################################
# 桥调用

async def call_bridge(tool: str, args: dict | None = None) -> Any:
    # trust_env=False：桥是 127.0.0.1 回环，绝不走系统代理。开着 VPN/代理时
    # 默认行为会把本机请求也送去代理解析——每次调用多 ~1.7s，且代理解析在
    # 事件循环里同步阻塞，会饿死进度通知等后台任务
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
# 工具定义

TOOLS = [
    Tool(
        name="read_ir",
        description="读取当前 IR JSON 全文（EEZ Studio 工程的界面描述源）",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="write_ir",
        description="写入完整的新版 IR JSON（全量覆盖）。必须是合法 JSON。",
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "完整的新 IR JSON 文本"}
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="compile",
        description="编译 IR → .eez-project。退出码非 0 = 失败（校验/字形检查不过），输出含具体报错。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="reload",
        description="让 EEZ Studio 重新加载工程文件（编译成功后必须调用才能在编辑器里看到新画面）。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="navigate",
        description="切换 EEZ Studio 到指定屏幕（打开它的编辑器，screenshot 截的就是它）。",
        inputSchema={
            "type": "object",
            "properties": {"screen": {"type": "string", "description": "屏幕名"}},
            "required": ["screen"],
        },
    ),
    Tool(
        name="screenshot",
        description="截取 EEZ Studio 当前屏幕的 LVGL 预览图（PNG，返回图片内容块供视觉模型查看）。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="ping",
        description="检查 EEZ Studio 桥是否在线（返回工程状态）。",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ---- Output / Checks（构建与检查错误） ----
    Tool(
        name="read_output",
        description=(
            "读取 EEZ Studio 底部面板的 Checks/Output section 消息。"
            "checks=实时后台检查（随编辑即时更新），output=上次构建的输出。"
            "每条消息含 type(error/warning/info)、text、object(出错对象路径)。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["checks", "output"],
                    "description": "读哪个 section，默认 checks",
                }
            },
        },
    ),
    Tool(
        name="check",
        description="触发一次完整工程检查（等结束），返回 Output section 的错误/警告。改完样式或 project JSON 后用它验证。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="build_project",
        description="触发 EEZ Studio 完整构建（Ctrl+B，LVGL 工程会生成 C 源码到构建目录），等结束返回 Output section 消息。耗时可能较长。",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ---- 样式 / 主题 ----
    Tool(
        name="list_styles",
        description=(
            "列出工程的 LVGL 样式（含完整 definition: part/state/属性）、经典样式名、"
            "主题颜色矩阵（每主题各颜色的实际值）。改样式前先看它。"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="update_style",
        description=(
            "修改 LVGL 样式属性：definition[part][state] 下设置 properties 键值"
            "（如 {'bg_color': '#ff0000', 'text_color': 'COLOR_ID_XXX'}）。"
            "颜色值可以是 #rrggbb 或主题颜色名；值为 null 表示删除该属性。"
            "part 默认 MAIN，state 默认 DEFAULT（自动转大写）。改完自动保存并即时生效。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "style": {"type": "string", "description": "LVGL 样式名"},
                "part": {"type": "string", "description": "部件，如 MAIN/SCROLLBAR"},
                "state": {"type": "string", "description": "状态，如 DEFAULT/CHECKED/PRESSED"},
                "properties": {
                    "type": "object",
                    "description": "属性名 → 值（null = 删除）",
                },
            },
            "required": ["style", "properties"],
        },
    ),
    Tool(
        name="create_style",
        description="新建 LVGL 样式（forWidgetType 默认 LVGLPanelWidget），再用 update_style 设属性。",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "forWidgetType": {"type": "string", "description": "默认 LVGLPanelWidget"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="delete_style",
        description="删除 LVGL 样式（引用它的部件会在 check 里报错）。",
        inputSchema={
            "type": "object",
            "properties": {"style": {"type": "string"}},
            "required": ["style"],
        },
    ),
    Tool(
        name="set_theme_color",
        description=(
            "设置主题颜色的值。theme 省略 = 所有主题一起改。"
            "引用该颜色名的样式/部件（颜色值 = 颜色名而非 #hex）会即时变色。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "color": {"type": "string", "description": "颜色槽位名（list_styles 可查）"},
                "value": {"type": "string", "description": "#rrggbb"},
                "theme": {"type": "string", "description": "主题名，省略 = 全部主题"},
            },
            "required": ["color", "value"],
        },
    ),
    Tool(
        name="add_color",
        description="新增主题颜色槽位并在所有主题里赋初值，之后样式里可用颜色名引用它。",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "新颜色槽位名"},
                "value": {"type": "string", "description": "初始值 #rrggbb"},
            },
            "required": ["name"],
        },
    ),
    # ---- .eez-project JSON 直读直写（绕过 IR） ----
    Tool(
        name="read_project_json",
        description="直接读当前 .eez-project 文件全文（绕过 IR 管线，手术式微调工程结构/部件属性时用）。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="write_project_json",
        description=(
            "直接写回 .eez-project 文件（全量覆盖，必须合法 JSON），随后自动 reload 工程让编辑器生效。"
            "注意：以磁盘文件为准重载，编辑器里未保存的改动会丢失。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "完整的新 .eez-project JSON"},
                "reload": {"type": "boolean", "description": "默认 true，写完立即重载工程"},
            },
            "required": ["content"],
        },
    ),
    # ---- 多工程 ----
    Tool(
        name="list_projects",
        description="列出 EEZ Studio 里打开的所有工程 tab（索引/路径/是否活动/是否已加载/运行时状态）。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="select_project",
        description="切换活动工程 tab（之后的工具都作用于它）。参数可以是索引、文件名或完整路径。",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {
                    "type": ["string", "integer"],
                    "description": "索引 / 文件名 / 完整路径",
                }
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="open_project",
        description="打开一个 .eez-project 文件（新 tab；已打开则复用并切换），等加载完成。",
        inputSchema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": ".eez-project 完整路径"}},
            "required": ["path"],
        },
    ),
    # ---- 运行时调试 ----
    Tool(
        name="debug_start",
        description=(
            "启动运行时（LVGL 工程为本地 wasm 模拟器）。mode=debug 调试模式（可 pause/单步），"
            "mode=run 纯运行。启动要构建资产，可能耗时几十秒。启动后用 screenshot 看模拟画面。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["debug", "run"], "description": "默认 debug"}
            },
        },
    ),
    Tool(
        name="debug_stop",
        description="停止运行时，回到编辑模式。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="debug_control",
        description="调试控制：pause 暂停 / resume 继续 / step_over|step_into|step_out 单步 / restart 重启（保持调试）。",
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
        description="查询运行时状态（运行/暂停/单步）和最近日志尾部。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="read_variable",
        description="读工程全局变量（调试/运行模式下与模拟器双向同步）。",
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    ),
    Tool(
        name="write_variable",
        description="写工程全局变量。value 可以是任意 JSON 值（数字/字符串/布尔/对象）。",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "value": {"description": "任意 JSON 值"},
            },
            "required": ["name", "value"],
        },
    ),
    # ---- 对象级编辑（部件/页面按路径 CRUD + undo） ----
    Tool(
        name="list_objects",
        description=(
            "列对象树：不给参数=页面总览；screen=页面名→该页部件树；"
            "path=对象路径→该容器子树。节点含 path/type/几何/文本/样式引用，path 可直接用于 "
            "get_object/update_object/delete_object。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "screen": {"type": "string", "description": "页面名"},
                "path": {"type": "string", "description": "对象路径（如 /userPages/0）"},
            },
        },
    ),
    Tool(
        name="get_object",
        description=(
            "按路径或 objID 读对象子树（所有持久化属性，depth 层深度默认 2，用尽给路径）。"
            "路径如 /userPages/0/components/0/children/3；也可传 objID（裸 GUID 或 objID:前缀，"
            "list_objects/get_object 输出里有）——增删部件会让路径索引漂移，objID 恒定。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "对象路径或 objID"},
                "depth": {"type": "integer", "description": "嵌套展开层数，默认 2"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="update_object",
        description=(
            "手术式改对象属性（可 undo，自动保存）。path 支持路径或 objID（objID 不受索引漂移影响）。"
            "属性平铺：left/top/width/height/text/useStyle/hiddenFlag/clickableFlag/value 等；"
            "支持一层点路径（data.text）。改完 navigate+screenshot 看效果，或 read_output(checks) 验证。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "对象路径或 objID"},
                "properties": {"type": "object", "description": "属性名 → 新值"},
            },
            "required": ["path", "properties"],
        },
    ),
    Tool(
        name="create_widget",
        description=(
            "新建 LVGL 部件。type 如 LVGLLabelWidget/LVGLButtonWidget/LVGLPanelWidget/"
            "LVGLSliderWidget（类型不对会返回完整可用列表）；parent=页面名/页面路径/容器部件路径；"
            "properties 覆盖默认值（left/top/width/height/text...）。自动落进页面的 ScreenWidget。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "部件类名（LVGL 前缀）"},
                "parent": {"type": "string", "description": "页面名或对象路径"},
                "properties": {"type": "object", "description": "初始属性"},
            },
            "required": ["type", "parent"],
        },
    ),
    Tool(
        name="delete_object",
        description="按路径或 objID 删对象（部件或整个页面，可 undo，自动保存）。",
        inputSchema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "对象路径或 objID"}},
            "required": ["path"],
        },
    ),
    Tool(
        name="create_screen",
        description="新建页面（自动带 LVGLScreenWidget 根；尺寸缺省取工程显示设置）。",
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
        description="撤销上一次编辑（部件/样式/主题的对象级操作都可回滚），并同步保存。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="redo",
        description="重做被撤销的编辑。",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="goto_object",
        description="在编辑器里选中并定位到对象（path 支持路径或 objID；check 报错的 object 路径直接跳过去看）。",
        inputSchema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "对象路径或 objID"}},
            "required": ["path"],
        },
    ),
    Tool(
        name="get_selection",
        description=(
            "读两级选中：editorSelection=页面编辑器里选中的部件（goto_object 后看这个），"
            "panelSelection=导航面板选中（属性面板优先用它）。人在回路：用户点一个部件，你拿到路径/objID。"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="screenshot_object",
        description=(
            "截取单个部件的特写（页面截图按部件绝对矩形裁剪，返回图片块）。"
            "path 支持路径或 objID（get_selection 拿到你选中的部件后直接用它）。"
            "padding 为四周留白像素（默认 8）。仅支持 px 定位的部件。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "部件路径或 objID"},
                "padding": {"type": "integer", "description": "四周留白像素，默认 8"},
            },
            "required": ["path"],
        },
    ),
    # ---- 模拟器输入 / 主题预览 / 新建工程 / 安全补丁 ----
    Tool(
        name="send_input",
        description=(
            "向运行中的模拟器注入指针输入（须先 debug_start 且未暂停）。"
            "op=click/press/release/swipe；x/y 为页面坐标（与部件 left/top 同坐标系，list_objects 可查）；"
            "swipe 附带 dx/dy 位移。用于验证按钮跳转/滚动等交互——点击后 screenshot 或 debug_status(selectedPage) 看结果。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["click", "press", "release", "swipe"]},
                "x": {"type": "integer", "description": "页面坐标 X"},
                "y": {"type": "integer", "description": "页面坐标 Y"},
                "dx": {"type": "integer", "description": "swipe 水平位移"},
                "dy": {"type": "integer", "description": "swipe 垂直位移"},
            },
            "required": ["op", "x", "y"],
        },
    ),
    Tool(
        name="set_preview_theme",
        description="切换主题预览（编辑态立即变色，运行态在 wasm 内切换）。配合 screenshot 逐主题验证配色。",
        inputSchema={
            "type": "object",
            "properties": {"theme": {"type": "string", "description": "主题名（list_styles 可查）"}},
            "required": ["theme"],
        },
    ),
    Tool(
        name="create_project",
        description=(
            "程序化新建最小 LVGL 工程（自动带 Default 主题和 Main 页）并打开新 tab。"
            "lvglVersion 可选 8.4.0/9.2.2/9.3.0/9.4.0/9.5.0（默认 9.5.0），尺寸默认 800x480。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "新工程完整路径（.eez-project 结尾，不能已存在）"},
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
            "列资产：自定义字体（bpp/字号/ranges/symbols/来源文件）、内置 Montserrat 保留名"
            "（MONTSERRAT_8..48 可直接作 text_font，无需建字体）、位图（含 bpp/来源）。"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="add_font",
        description=(
            "从 TTF 新建 LVGL 字体（Studio 内建 lv_font_conv，无需外部命令）。"
            "ranges 如 '32-127'（逗号分隔多段），symbols 为逐字符（中文/图标字面量）。"
            "建好后样式 text_font 填字体名。耗时可能十几秒（支持进度通知）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "字体名（唯一）"},
                "ttf_path": {"type": "string", "description": "TTF 文件完整路径"},
                "size": {"type": "integer", "description": "字号（像素），默认 16"},
                "bpp": {"type": "integer", "enum": [1, 2, 4, 8], "description": "默认 4"},
                "ranges": {"type": "string", "description": "Unicode 区间，默认 32-127"},
                "symbols": {"type": "string", "description": "逐字符集，如 '温度转速报警'"},
            },
            "required": ["name", "ttf_path", "size"],
        },
    ),
    Tool(
        name="add_image",
        description=(
            "导入图片（PNG/JPG/BMP/GIF）为工程位图；非 embed 模式自动拷进工程 images/ 目录。"
            "返回 name，create_widget(LVGLImageWidget) 的 image 属性填它。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "图片文件完整路径"},
                "name": {"type": "string", "description": "位图名（缺省用文件名，自动去重）"},
                "bpp": {"type": "integer", "description": "LVGL 色彩格式，缺省 32 (CF_TRUE_COLOR_ALPHA)"},
            },
            "required": ["image_path"],
        },
    ),
    Tool(
        name="patch_project_json",
        description=(
            "对当前工程的 .eez-project JSON 打补丁后写回并自动 reload（比 write_project_json 全量覆盖安全）。"
            "mode=merge（默认，RFC 7396 深合并：给对象嵌套合并、null 删键、数组整体替换）或 "
            "mode=jsonpatch（RFC 6902 操作数组：add/remove/replace/move/copy/test，path 用 JSON Pointer）。"
            "适合大结构批量变更；小改动优先用 update_object。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "patch": {
                    "description": "merge 模式=补丁对象；jsonpatch 模式=操作数组",
                },
                "mode": {"type": "string", "enum": ["merge", "jsonpatch"], "description": "默认 merge"},
            },
            "required": ["patch"],
        },
    ),
]

###############################################################################
# 活资源（eez://）：按需从桥取实时内容，可订阅（变更即推送 resources/updated）

LIVE_RESOURCES = {
    "eez://checks": ("Checks 实时检查", "错误/警告计数与消息（订阅后变更即推送）"),
    "eez://debug": ("运行时状态", "调试器状态、当前页与日志尾部（订阅后变更即推送）"),
    "eez://state": ("工程状态", "活动工程与选中对象（订阅后变更即推送）"),
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
    raise ValueError(f"未知资源: {uri}")

async def _resource_watcher() -> None:
    """轮询活资源，内容变化时发布 ResourceUpdated 事件（bus → subscriptions/listen 流）"""
    try:
        while True:
            await asyncio.sleep(2)
            has_bus_listeners = bool(getattr(SUBSCRIPTION_BUS, "_listeners", None))
            if not (has_bus_listeners or _subscribed_uris):
                continue
            # 只轮询需要的 URI：legacy 订阅的 + （有 bus 监听时）全部。
            # 注意每个桥调用 ~2.5s，用 gather 并行压周期。
            uris = set(_subscribed_uris)
            if has_bus_listeners:
                uris.update(LIVE_RESOURCES.keys())
            results = await asyncio.gather(
                *[live_resource_content(u) for u in uris], return_exceptions=True
            )
            for uri, content in zip(uris, results):
                if isinstance(content, BaseException):
                    continue  # 桥不在线等场景
                h = hashlib.md5(content.encode("utf-8", "ignore")).hexdigest()
                prev = _resource_hashes.get(uri)
                _resource_hashes[uri] = h
                if prev is not None and prev != h:
                    _dbg(f"{uri} 变化，发布事件")
                    try:
                        await SUBSCRIPTION_BUS.publish(ResourceUpdated(uri=uri))
                    except Exception as e:
                        _dbg(f"bus 发布失败: {e}")
                    # 兼容旧协议客户端（resources/subscribe 路径）
                    if uri in _subscribed_uris:
                        session = _session_ref["session"]
                        if session:
                            try:
                                await session.send_resource_updated(uri)
                            except Exception as e:
                                _dbg(f"legacy 推送失败: {e}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _dbg(f"watcher 崩溃: {type(e).__name__} {e}")


def _dbg(msg: str) -> None:
    """调试日志到 stderr（stdio 协议走 stdout，stderr 安全）"""
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
    """server/discover（2026-07-28 入口）：宣告支持新版协议，让客户端能协商
    到 subscriptions/listen 流。低层 Server 不自动注册，这里补上。"""
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
# 资源定义

async def list_resources() -> list[Resource]:
    ir_path = Path(WORKDIR) / "sg8.ir.json"
    resources = [
        Resource(
            uri=f"file://{ir_path}",
            name="当前 IR JSON",
            description="EEZ Studio 工程的界面描述源文件",
            mimeType="application/json",
        ),
        Resource(
            uri=f"file://{WORKDIR}/IR_SCHEMA.md",
            name="IR 格式规范",
            description="IR JSON 的结构定义与 EEZ 约束",
            mimeType="text/markdown",
        ),
        Resource(
            uri=f"file://{WORKDIR}/SKILL.md",
            name="EEZ Studio 技能文档",
            description="EEZ Studio LVGL 界面生成的经验规则",
            mimeType="text/markdown",
        ),
    ]
    for uri, (name, desc) in LIVE_RESOURCES.items():
        resources.append(
            Resource(uri=uri, name=name, description=desc, mimeType="application/json")
        )
    return resources

###############################################################################
# 提示词定义

async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="modify_ui",
            description="修改 EEZ Studio LVGL 界面",
            arguments=[
                PromptArgument(name="requirement", description="要改什么", required=True),
            ],
        ),
        Prompt(
            name="create_ui",
            description="从设计稿 HTML 创建新的 EEZ Studio LVGL 界面",
            arguments=[
                PromptArgument(name="html_path", description="设计稿 HTML 文件路径", required=True),
            ],
        ),
    ]

async def get_prompt(name: str, arguments: dict) -> GetPromptResult:
    schema_text = Path(WORKDIR, "IR_SCHEMA.md").read_text(encoding="utf-8")[:5000]
    skill_text = Path(WORKDIR, "SKILL.md").read_text(encoding="utf-8")[:3000]

    common = f"""== EEZ Studio LVGL 工具（经 MCP 桥调用）==

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

== IR 规范（摘要）==
{schema_text}

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
# JSON Patch（RFC 6902）与 Merge Patch（RFC 7396）的最小实现（无第三方依赖）

def _json_pointer_tokens(ptr: str) -> list[str]:
    if ptr == "":
        return []
    if not ptr.startswith("/"):
        raise ValueError(f"非法 JSON Pointer（须以 / 开头或为空）: {ptr}")
    return [t.replace("~1", "/").replace("~0", "~") for t in ptr.split("/")[1:]]

def _walk(doc, tokens):
    """走到倒数第二级，返回 (parent, last_token)"""
    cur = doc
    for t in tokens[:-1]:
        if isinstance(cur, list):
            cur = cur[int(t)]
        elif isinstance(cur, dict):
            cur = cur[t]
        else:
            raise ValueError(f"路径穿越非容器: {t}")
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
        raise ValueError("jsonpatch 模式的 patch 必须是操作数组")
    for op in ops:
        kind = op.get("op")
        tokens = _json_pointer_tokens(op.get("path", ""))
        if kind == "add":
            doc = _add_at(doc, tokens, copy.deepcopy(op.get("value")))
        elif kind == "replace":
            _get_at(doc, tokens)  # 不存在会抛 KeyError/IndexError
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
                raise ValueError(f"test 失败: {op.get('path')}")
        else:
            raise ValueError(f"未知 op: {kind}")
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
        raise RuntimeError("EEZ Studio 里没有打开的工程")

    doc = json.loads(Path(project_file).read_text(encoding="utf-8"))
    if mode == "merge":
        if not isinstance(patch, dict):
            raise ValueError("merge 模式的 patch 必须是对象")
        result = apply_merge_patch(doc, patch)
    elif mode == "jsonpatch":
        result = apply_json_patch(doc, patch)
    else:
        raise ValueError(f"未知 mode: {mode}（merge 或 jsonpatch）")

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
# 工具执行

async def call_tool(name: str, arguments: dict) -> list:
    if name == "ping":
        result = await call_bridge("ping")
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    if name == "screenshot":
        result = await call_bridge("screenshot")
        data_url = result["dataUrl"]
        base64_data = data_url.split(",", 1)[1] if "," in data_url else data_url
        return [
            TextContent(type="text", text=f"截图完成: {result['file']}"),
            ImageContent(type="image", data=base64_data, mimeType="image/png"),
        ]

    if name == "read_ir":
        result = await call_bridge("read_ir")
        text = str(result)[:50000]  # 防超长
        return [TextContent(type="text", text=text)]

    if name == "write_ir":
        result = await call_bridge("write_ir", {"content": arguments["content"]})
        return [TextContent(type="text", text=str(result))]

    if name == "compile":
        result = await call_bridge("compile")
        ok = result.get("ok", False)
        output = result.get("output", "")
        status = "✓ 编译成功" if ok else "✗ 编译失败"
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
                text=f"部件截图 {result['rect']}: {result['file']}",
            ),
            ImageContent(type="image", data=base64_data, mimeType="image/png"),
        ]

    if name == "read_project_json":
        result = await call_bridge("read_project_json")
        text = str(result)[:50000]  # 防超长
        return [TextContent(type="text", text=text)]

    if name == "write_project_json":
        result = await call_bridge(
            "write_project_json",
            {"content": arguments["content"], "reload": arguments.get("reload", True)},
        )
        return [TextContent(type="text", text=str(result))]

    if name == "patch_project_json":
        return await patch_project_json_tool(arguments)

    # 其余工具：参数透传给桥，结果 JSON 序列化返回
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

    return [TextContent(type="text", text=f"未知工具: {name}")]

###############################################################################
# MCP 服务器处理器（mcp 2.0 低层 API：add_request_handler，handler 签名 (ctx, params)）

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
            text = f"读取失败: {e}"
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
            contents=[TextResourceContents(uri=params.uri, mimeType="text/plain", text=f"读取失败: {e}")]
        )

async def handle_get_prompt(ctx, params: GetPromptRequestParams) -> GetPromptResult:
    return await get_prompt(params.name, dict(params.arguments or {}))

###############################################################################
# 长操作的进度通知（客户端在 _meta.progressToken 里给 token 即启用）

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
    # 客户端把 _meta 归一化为 snake_case 的 progress_token（也可能收到 camelCase）
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
                        token, elapsed, message=f"{name}: 已等待 {elapsed}s"
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
            error_hint = "\n提示：EEZ Studio 可能未启动，请先启动 EEZ Studio。"
        return CallToolResult(
            content=[TextContent(type="text", text=f"错误: {e}{error_hint}")],
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
# 主入口

async def main():
    watcher = asyncio.create_task(_resource_watcher())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
    watcher.cancel()

if __name__ == "__main__":
    asyncio.run(main())
