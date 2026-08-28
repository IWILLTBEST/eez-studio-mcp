#!/usr/bin/env node
/**
 * mcp-server — MCP (Model Context Protocol) server for EEZ Studio (Node.js, zero-dependency).
 *
 * Lets any MCP client (Claude Desktop / Cursor / ZCode / Continue / DSH, ...) drive EEZ Studio.
 *
 * Architecture:
 *     MCP client <-> stdio (newline-delimited JSON-RPC) <-> this server <-> HTTP <-> EEZ Studio bridge (17620)
 *
 * Usage (Claude Desktop's claude_desktop_config.json):
 *     {
 *       "mcpServers": {
 *         "eez-studio": {
 *           "command": "node",
 *           "args": ["<repo>/mcp-server.mjs"]
 *         }
 *       }
 *     }
 *
 * No npm dependencies: JSON-RPC over stdio is implemented by hand (one JSON document per line).
 * Requires Node.js 18+. stdout carries protocol messages only; all logs go to stderr.
 *
 * 中文：EEZ Studio 的 MCP 服务器（Node.js 零依赖版）。架构为 MCP 客户端 <-> stdio（换行分隔
 * JSON-RPC）<-> 本服务器 <-> HTTP <-> EEZ Studio 桥(17620)。无任何 npm 依赖，手写 JSON-RPC，
 * stdout 只走协议、日志全部走 stderr，Node 18+ 直接 `node mcp-server.mjs` 运行。
 */
import path from "node:path";
import fs from "node:fs";
import crypto from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";

// The bridge is 127.0.0.1 loopback; never let proxy env vars route loopback fetches
// through a system proxy (adds ~1.7s per call and can starve the event loop under VPN).
// 中文：桥是回环地址；清掉代理环境变量，避免开 VPN 时 fetch 走系统代理（每次 +1.7s 且阻塞）。
for (const k of ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]) {
    delete process.env[k];
}

const BRIDGE_URL = process.env.EEZ_BRIDGE_URL || "http://127.0.0.1:17620";
const WORKDIR = process.env.EEZ_WORKDIR || path.dirname(fileURLToPath(import.meta.url));

const SERVER_NAME = "eez-studio";
const SERVER_VERSION = "1.0.0";
// Latest protocol version this server speaks; older client-requested versions are echoed back.
// 中文：本服务器支持的最新协议版本；客户端请求旧版本时回显其版本。
const LATEST_PROTOCOL_VERSION = "2025-11-25";
const SUPPORTED_PROTOCOL_VERSIONS = new Set(["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]);

const HTTP_TIMEOUT_MS = 180000; // bridge calls take ~0.3-3s each; long ops need headroom 桥调用超时
const MAX_TEXT_LEN = 50000; // cap read_ir / read_project_json / resource payloads 截断长度

/** Log to stderr (stdout is reserved for the protocol). 中文：日志走 stderr。 */
function log(msg) {
    try {
        process.stderr.write(`[mcp] ${msg}\n`);
    } catch {
        /* ignore */
    }
}

// ----------------------------------------------------------------------------
// stdout JSON-RPC writer（stdout 只写协议，一次一行，串行化避免交错）
// ----------------------------------------------------------------------------

let writeChain = Promise.resolve();

/** Serialize one JSON-RPC message per line to stdout. 按行串行写协议消息。 */
function send(message) {
    const line = JSON.stringify(message) + "\n";
    writeChain = writeChain.then(
        () =>
            new Promise((resolve) => {
                process.stdout.write(line, "utf8", resolve);
            })
    ).catch((e) => log(`stdout write failed: ${e?.message || e}`));
    return writeChain;
}

function sendResult(id, result) {
    return send({ jsonrpc: "2.0", id, result });
}

function sendError(id, code, message) {
    return send({ jsonrpc: "2.0", id, error: { code, message } });
}

process.stdout.on("error", (e) => {
    // Client went away (EPIPE) — nothing left to serve. 中文：客户端断开，直接退出。
    if (e?.code === "EPIPE") process.exit(0);
    log(`stdout error: ${e?.message || e}`);
});

// ----------------------------------------------------------------------------
// Bridge calls 桥调用
// ----------------------------------------------------------------------------

/**
 * POST {"tool", "args"} to the EEZ Studio bridge and unwrap {ok, result|error}.
 * 中文：调用 EEZ Studio 桥并解开 ok/result/error 封包。
 */
async function callBridge(tool, args) {
    let resp;
    try {
        resp = await fetch(`${BRIDGE_URL}/tool`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tool, args: args || {} }),
            signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
        });
    } catch (e) {
        // Surface the undici cause (e.g. "connect ECONNREFUSED ...") so callers can hint.
        // 中文：透出底层原因（如 connect ECONNREFUSED），便于上层给出提示。
        const cause = e?.cause?.message || "";
        throw new Error(`bridge request failed: ${e?.message || e}${cause ? `: ${cause}` : ""}`);
    }
    let data;
    try {
        data = await resp.json();
    } catch (e) {
        throw new Error(`bridge returned non-JSON (HTTP ${resp.status}): ${e?.message || e}`);
    }
    if (!data || data.ok !== true) {
        throw new Error((data && data.error) || "bridge error");
    }
    return data.result;
}

// ----------------------------------------------------------------------------
// Tool definitions (mirrors eez_mcp_server.py TOOLS) 工具定义（与 Python 版对齐）
// ----------------------------------------------------------------------------

const TOOLS = [
    {
        name: "read_ir",
        description:
            "Read the full current IR JSON (the UI description source of the EEZ Studio project). 读取当前 IR JSON 全文。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "write_ir",
        description:
            "Write a complete new IR JSON (full overwrite); must be valid JSON. 写入完整新版 IR JSON（全量覆盖）。",
        inputSchema: {
            type: "object",
            properties: {
                content: { type: "string", description: "Full new IR JSON text 完整的新 IR JSON 文本" },
            },
            required: ["content"],
        },
    },
    {
        name: "compile",
        description:
            "Compile IR to .eez-project; non-zero exit = failure (validation/glyph check), output contains the errors. 编译 IR → .eez-project，非 0 退出码即失败。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "reload",
        description:
            "Reload the project file in EEZ Studio; required after a successful compile to see the new screen in the editor. 让 EEZ Studio 重新加载工程文件。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "navigate",
        description:
            "Switch EEZ Studio to the given screen (opens its editor; screenshot captures it). 切换到指定屏幕。",
        inputSchema: {
            type: "object",
            properties: { screen: { type: "string", description: "Screen name 屏幕名" } },
            required: ["screen"],
        },
    },
    {
        name: "screenshot",
        description:
            "Capture the LVGL preview of the current screen (PNG, returned as an image content block). 截取当前屏幕 LVGL 预览图。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "ping",
        description:
            "Check whether the EEZ Studio bridge is online (returns project status). 检查桥是否在线。",
        inputSchema: { type: "object", properties: {} },
    },
    // ---- Output / Checks (build & check errors 构建与检查错误) ----
    {
        name: "read_output",
        description:
            "Read Checks/Output panel messages in EEZ Studio: checks=live background checks " +
            "(updated as you edit), output=last build output; each message has " +
            "type(error/warning/info), text and object (path of the offending object). " +
            "读取底部面板 Checks/Output 消息。",
        inputSchema: {
            type: "object",
            properties: {
                section: {
                    type: "string",
                    enum: ["checks", "output"],
                    description: "Which section to read, default checks 读哪个 section，默认 checks",
                },
            },
        },
    },
    {
        name: "check",
        description:
            "Run a full project check (waits until done) and return Output-section errors/warnings; use it to verify after editing styles or the project JSON. 触发完整工程检查并返回错误/警告。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "build_project",
        description:
            "Trigger a full EEZ Studio build (Ctrl+B; LVGL projects generate C sources into the build dir), wait for it and return Output-section messages; may take a while. 触发完整构建并等结束，可能较慢。",
        inputSchema: { type: "object", properties: {} },
    },
    // ---- Styles / Themes 样式 / 主题 ----
    {
        name: "list_styles",
        description:
            "List the project's LVGL styles (with full definition: part/state/properties), " +
            "classic style names and the theme color matrix (actual value of each color per " +
            "theme); read this before editing styles. 列出样式/经典样式名/主题颜色矩阵。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "update_style",
        description:
            "Update LVGL style properties: set property key-values under " +
            "definition[part][state] (e.g. {'bg_color': '#ff0000', 'text_color': " +
            "'COLOR_ID_XXX'}); color values may be #rrggbb or theme color names, and null " +
            "deletes the property; part defaults to MAIN, state to DEFAULT (auto-uppercased); " +
            "auto-saved and effective immediately. 修改样式属性（null 删除，即时生效）。",
        inputSchema: {
            type: "object",
            properties: {
                style: { type: "string", description: "LVGL style name LVGL 样式名" },
                part: { type: "string", description: "Widget part, e.g. MAIN/SCROLLBAR 部件，如 MAIN/SCROLLBAR" },
                state: { type: "string", description: "State, e.g. DEFAULT/CHECKED/PRESSED 状态，如 DEFAULT/CHECKED/PRESSED" },
                properties: {
                    type: "object",
                    description: "Property name → value (null = delete) 属性名 → 值（null = 删除）",
                },
            },
            required: ["style", "properties"],
        },
    },
    {
        name: "create_style",
        description:
            "Create a new LVGL style (forWidgetType defaults to LVGLPanelWidget), then set its properties with update_style. 新建 LVGL 样式。",
        inputSchema: {
            type: "object",
            properties: {
                name: { type: "string" },
                forWidgetType: { type: "string", description: "Defaults to LVGLPanelWidget 默认 LVGLPanelWidget" },
            },
            required: ["name"],
        },
    },
    {
        name: "delete_style",
        description:
            "Delete an LVGL style (widgets referencing it will fail in check). 删除 LVGL 样式。",
        inputSchema: {
            type: "object",
            properties: { style: { type: "string" } },
            required: ["style"],
        },
    },
    {
        name: "set_theme_color",
        description:
            "Set a theme color's value; omitting theme updates all themes at once; styles/" +
            "widgets referencing that color name (value = color name instead of #hex) " +
            "recolor instantly. 设置主题颜色值（省略 theme 则全部主题一起改）。",
        inputSchema: {
            type: "object",
            properties: {
                color: { type: "string", description: "Color slot name (see list_styles) 颜色槽位名" },
                value: { type: "string", description: "#rrggbb" },
                theme: { type: "string", description: "Theme name; omitted = all themes 主题名，省略 = 全部主题" },
            },
            required: ["color", "value"],
        },
    },
    {
        name: "add_color",
        description:
            "Add a new theme color slot with an initial value in all themes; styles can then reference it by name. 新增主题颜色槽位并赋初值。",
        inputSchema: {
            type: "object",
            properties: {
                name: { type: "string", description: "New color slot name 新颜色槽位名" },
                value: { type: "string", description: "Initial value #rrggbb 初始值 #rrggbb" },
            },
            required: ["name", "value"],
        },
    },
    // ---- Direct .eez-project JSON read/write (bypasses IR) 直读直写 ----
    {
        name: "read_project_json",
        description:
            "Read the full current .eez-project file directly (bypasses the IR pipeline; for surgical tweaks to project structure/widget properties). 直读 .eez-project 全文。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "write_project_json",
        description:
            "Write the .eez-project file directly (full overwrite, must be valid JSON), then " +
            "auto-reload the project; note: reload trusts the disk file, so unsaved editor " +
            "changes are lost. 直写 .eez-project（全量覆盖，自动 reload）。",
        inputSchema: {
            type: "object",
            properties: {
                content: { type: "string", description: "Full new .eez-project JSON 完整的新 .eez-project JSON" },
                reload: { type: "boolean", description: "Default true; reload the project right after writing 默认 true，写完立即重载" },
            },
            required: ["content"],
        },
    },
    // ---- Multiple projects 多工程 ----
    {
        name: "list_projects",
        description:
            "List all project tabs open in EEZ Studio (index/path/active/loaded/runtime state). 列出打开的工程 tab。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "select_project",
        description:
            "Switch the active project tab (subsequent tools act on it); the parameter may be an index, file name or full path. 切换活动工程 tab。",
        inputSchema: {
            type: "object",
            properties: {
                project: {
                    type: ["string", "integer"],
                    description: "Index / file name / full path 索引 / 文件名 / 完整路径",
                },
            },
            required: ["project"],
        },
    },
    {
        name: "open_project",
        description:
            "Open a .eez-project file (new tab; reuses and switches to it if already open) and wait until loaded. 打开 .eez-project 并等加载完成。",
        inputSchema: {
            type: "object",
            properties: { path: { type: "string", description: "Full path to the .eez-project file .eez-project 完整路径" } },
            required: ["path"],
        },
    },
    // ---- Runtime debugging 运行时调试 ----
    {
        name: "debug_start",
        description:
            "Start the runtime (a local wasm simulator for LVGL projects); mode=debug enables " +
            "pause/stepping, mode=run is plain run; startup builds assets and may take tens " +
            "of seconds, then view it with screenshot. 启动运行时（可能耗时几十秒）。",
        inputSchema: {
            type: "object",
            properties: {
                mode: { type: "string", enum: ["debug", "run"], description: "Default debug 默认 debug" },
            },
        },
    },
    {
        name: "debug_stop",
        description:
            "Stop the runtime and return to edit mode. 停止运行时，回到编辑模式。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "debug_control",
        description:
            "Debug control: pause / resume / step_over|step_into|step_out stepping / restart (stays debugging). 调试控制（暂停/继续/单步/重启）。",
        inputSchema: {
            type: "object",
            properties: {
                op: {
                    type: "string",
                    enum: ["pause", "resume", "step_over", "step_into", "step_out", "restart"],
                },
            },
            required: ["op"],
        },
    },
    {
        name: "debug_status",
        description:
            "Query the runtime state (running/paused/stepping) and the tail of recent logs. 查询运行时状态和日志尾部。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "read_variable",
        description:
            "Read a project global variable (two-way synced with the simulator in debug/run mode). 读工程全局变量。",
        inputSchema: {
            type: "object",
            properties: { name: { type: "string" } },
            required: ["name"],
        },
    },
    {
        name: "write_variable",
        description:
            "Write a project global variable; value may be any JSON value (number/string/bool/object). 写工程全局变量。",
        inputSchema: {
            type: "object",
            properties: {
                name: { type: "string" },
                value: { description: "Any JSON value 任意 JSON 值" },
            },
            required: ["name", "value"],
        },
    },
    // ---- Object-level editing (path-based CRUD on widgets/pages + undo) 对象级编辑 ----
    {
        name: "list_objects",
        description:
            "List the object tree: no args = page overview; screen=<page name> = that page's " +
            "widget tree; path=<object path> = that container's subtree. Nodes carry path/" +
            "type/geometry/text/style refs, and path works directly in get_object/" +
            "update_object/delete_object. 列对象树（无参=页面总览，screen/path 定位子树）。",
        inputSchema: {
            type: "object",
            properties: {
                screen: { type: "string", description: "Page name 页面名" },
                path: { type: "string", description: "Object path (e.g. /userPages/0) 对象路径" },
            },
        },
    },
    {
        name: "get_object",
        description:
            "Read an object subtree by path or objID (all persisted properties; depth " +
            "defaults to 2, deeper levels are given as paths). Path e.g. /userPages/0/" +
            "components/0/children/3; objID (bare GUID or objID: prefix, shown in " +
            "list_objects/get_object output) is stable while path indices drift when " +
            "widgets are added/removed. 按路径或 objID 读对象子树。",
        inputSchema: {
            type: "object",
            properties: {
                path: { type: "string", description: "Object path or objID 对象路径或 objID" },
                depth: { type: "integer", description: "Nesting levels to expand, default 2 嵌套展开层数，默认 2" },
            },
            required: ["path"],
        },
    },
    {
        name: "update_object",
        description:
            "Surgically update object properties (undoable, auto-saved); path accepts path " +
            "or objID (objID is drift-proof). Properties are flat: left/top/width/height/" +
            "text/useStyle/hiddenFlag/clickableFlag/value etc., plus one level of dotted " +
            "paths (data.text). Verify with navigate+screenshot or read_output(checks). " +
            "手术式改对象属性（可 undo，自动保存）。",
        inputSchema: {
            type: "object",
            properties: {
                path: { type: "string", description: "Object path or objID 对象路径或 objID" },
                properties: { type: "object", description: "Property name → new value 属性名 → 新值" },
            },
            required: ["path", "properties"],
        },
    },
    {
        name: "create_widget",
        description:
            "Create an LVGL widget; type e.g. LVGLLabelWidget/LVGLButtonWidget/" +
            "LVGLPanelWidget/LVGLSliderWidget (an invalid type returns the full list of " +
            "valid ones); parent = page name/page path/container widget path; properties " +
            "override defaults (left/top/width/height/text...). The widget lands in the " +
            "page's ScreenWidget. 新建 LVGL 部件（类型不对会返回可用列表）。",
        inputSchema: {
            type: "object",
            properties: {
                type: { type: "string", description: "Widget class name (LVGL- prefixed) 部件类名" },
                parent: { type: "string", description: "Page name or object path 页面名或对象路径" },
                properties: { type: "object", description: "Initial properties 初始属性" },
            },
            required: ["type", "parent"],
        },
    },
    {
        name: "delete_object",
        description:
            "Delete an object by path or objID (a widget or a whole page; undoable, auto-saved). 按路径或 objID 删对象。",
        inputSchema: {
            type: "object",
            properties: { path: { type: "string", description: "Object path or objID 对象路径或 objID" } },
            required: ["path"],
        },
    },
    {
        name: "create_screen",
        description:
            "Create a page (auto LVGLScreenWidget root; size defaults to the project display settings). 新建页面。",
        inputSchema: {
            type: "object",
            properties: {
                name: { type: "string" },
                width: { type: "integer" },
                height: { type: "integer" },
            },
            required: ["name"],
        },
    },
    {
        name: "undo",
        description:
            "Undo the last edit (object-level ops on widgets/styles/themes all roll back) and save. 撤销上一次编辑并保存。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "redo",
        description: "Redo the undone edit. 重做被撤销的编辑。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "goto_object",
        description:
            "Select and scroll to an object in the editor (path accepts path or objID; jump straight to object paths reported by check). 在编辑器里选中并定位对象。",
        inputSchema: {
            type: "object",
            properties: { path: { type: "string", description: "Object path or objID 对象路径或 objID" } },
            required: ["path"],
        },
    },
    {
        name: "get_selection",
        description:
            "Read the two-level selection: editorSelection = widget selected in the page " +
            "editor (check this after goto_object); panelSelection = navigation panel " +
            "selection (the property panel prefers it). Human-in-the-loop: the user clicks " +
            "a widget, you get its path/objID. 读两级选中。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "screenshot_object",
        description:
            "Screenshot a single widget close-up (the page screenshot cropped to the " +
            "widget's absolute rect, returned as an image block); path accepts path or " +
            "objID (use it right after get_selection); padding is margin in px on all " +
            "sides (default 8); only px-positioned widgets are supported. " +
            "截取单个部件特写（padding 默认 8）。",
        inputSchema: {
            type: "object",
            properties: {
                path: { type: "string", description: "Widget path or objID 部件路径或 objID" },
                padding: { type: "integer", description: "Margin in px on all sides, default 8 四周留白像素，默认 8" },
            },
            required: ["path"],
        },
    },
    // ---- Simulator input / theme preview / new project / safe patching 模拟器输入/主题预览/新建工程/安全补丁 ----
    {
        name: "send_input",
        description:
            "Inject pointer input into the running simulator (requires debug_start and not " +
            "paused); op=click/press/release/swipe; x/y are page coordinates (same frame " +
            "as widget left/top, see list_objects); swipe takes dx/dy deltas. Use it to " +
            "verify button navigation/scrolling, then check with screenshot or " +
            "debug_status(selectedPage). 向模拟器注入指针输入。",
        inputSchema: {
            type: "object",
            properties: {
                op: { type: "string", enum: ["click", "press", "release", "swipe"] },
                x: { type: "integer", description: "Page X coordinate 页面坐标 X" },
                y: { type: "integer", description: "Page Y coordinate 页面坐标 Y" },
                dx: { type: "integer", description: "Swipe horizontal delta swipe 水平位移" },
                dy: { type: "integer", description: "Swipe vertical delta swipe 垂直位移" },
            },
            required: ["op", "x", "y"],
        },
    },
    {
        name: "set_preview_theme",
        description:
            "Switch the preview theme (edit mode recolors instantly; runtime switches inside wasm); combine with screenshot to verify colors per theme. 切换主题预览。",
        inputSchema: {
            type: "object",
            properties: { theme: { type: "string", description: "Theme name (see list_styles) 主题名" } },
            required: ["theme"],
        },
    },
    {
        name: "create_project",
        description:
            "Programmatically create a minimal LVGL project (with Default theme and Main " +
            "page) and open it in a new tab; lvglVersion: 8.4.0/9.2.2/9.3.0/9.4.0/9.5.0 " +
            "(default 9.5.0), size defaults to 800x480. 程序化新建最小 LVGL 工程并打开。",
        inputSchema: {
            type: "object",
            properties: {
                path: { type: "string", description: "Full path of the new project (must end with .eez-project and must not exist) 新工程完整路径" },
                width: { type: "integer" },
                height: { type: "integer" },
                lvglVersion: { type: "string" },
            },
            required: ["path"],
        },
    },
    {
        name: "list_assets",
        description:
            "List assets: custom fonts (bpp/size/ranges/symbols/source file), reserved " +
            "built-in Montserrat names (MONTSERRAT_8..48 usable directly as text_font, no " +
            "font build needed), and bitmaps (with bpp/source). 列出字体/内置 Montserrat/位图。",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "add_font",
        description:
            "Create an LVGL font from a TTF (Studio bundles lv_font_conv, no external " +
            "command); ranges e.g. '32-127' (comma-separated segments); symbols is a " +
            "literal per-character string (Chinese/icon glyphs); afterwards set the " +
            "style's text_font to the font name; may take a dozen seconds (progress " +
            "notifications supported). 从 TTF 新建 LVGL 字体。",
        inputSchema: {
            type: "object",
            properties: {
                name: { type: "string", description: "Font name (unique) 字体名（唯一）" },
                ttf_path: { type: "string", description: "Full path to the TTF file TTF 文件完整路径" },
                size: { type: "integer", description: "Font size in pixels, default 16 字号（像素），默认 16" },
                bpp: { type: "integer", enum: [1, 2, 4, 8], description: "Default 4 默认 4" },
                ranges: { type: "string", description: "Unicode ranges, default 32-127 Unicode 区间，默认 32-127" },
                symbols: { type: "string", description: "Literal per-character string, e.g. '温度转速报警' 逐字符集" },
            },
            required: ["name", "ttf_path", "size"],
        },
    },
    {
        name: "add_image",
        description:
            "Import an image (PNG/JPG/BMP/GIF) as a project bitmap; in non-embed mode it " +
            "is copied into the project's images/ dir; returns the name to fill into " +
            "create_widget(LVGLImageWidget)'s image property. 导入图片为工程位图。",
        inputSchema: {
            type: "object",
            properties: {
                image_path: { type: "string", description: "Full path to the image file 图片文件完整路径" },
                name: { type: "string", description: "Bitmap name (defaults to the file name, auto-dedup) 位图名" },
                bpp: { type: "integer", description: "LVGL color format, default 32 (CF_TRUE_COLOR_ALPHA) LVGL 色彩格式，缺省 32" },
            },
            required: ["image_path"],
        },
    },
    {
        name: "patch_project_json",
        description:
            "Patch the current project's .eez-project JSON, write it back and auto-reload " +
            "(safer than write_project_json's full overwrite); mode=merge (default, " +
            "RFC 7396 deep merge: objects merge recursively, null deletes a key, arrays " +
            "are replaced wholesale) or mode=jsonpatch (RFC 6902 op array: add/remove/" +
            "replace/move/copy/test, paths use JSON Pointer); suited to large structural " +
            "changes — prefer update_object for small edits. 对工程 JSON 打补丁并自动 reload。",
        inputSchema: {
            type: "object",
            properties: {
                patch: {
                    description: "merge mode = patch object; jsonpatch mode = op array merge 模式=补丁对象；jsonpatch 模式=操作数组",
                },
                mode: { type: "string", enum: ["merge", "jsonpatch"], description: "Default merge 默认 merge" },
            },
            required: ["patch"],
        },
    },
];

// ----------------------------------------------------------------------------
// Minimal JSON Patch (RFC 6902) & Merge Patch (RFC 7396), zero-dependency
// 最小实现（与 Python 版逐语义对齐）
// ----------------------------------------------------------------------------

/** Parse a JSON Pointer into unescaped tokens. 解析 JSON Pointer 为 token 数组。 */
function jsonPointerTokens(ptr) {
    if (ptr === "") return [];
    if (!ptr.startsWith("/")) {
        throw new Error(`Invalid JSON Pointer (must start with / or be empty): ${ptr}`);
    }
    return ptr
        .slice(1)
        .split("/")
        .map((t) => t.replace(/~1/g, "/").replace(/~0/g, "~"));
}

/** Walk down to the second-to-last level and return [parent, lastToken]. 走到倒数第二级。 */
function walk(doc, tokens) {
    let cur = doc;
    for (let i = 0; i < tokens.length - 1; i++) {
        const t = tokens[i];
        if (Array.isArray(cur)) cur = cur[parseInt(t, 10)];
        else if (cur !== null && typeof cur === "object") cur = cur[t];
        else throw new Error(`Path traverses a non-container: ${t}`);
    }
    return [cur, tokens[tokens.length - 1]];
}

function getAt(doc, tokens) {
    if (tokens.length === 0) return doc;
    const [parent, last] = walk(doc, tokens);
    if (Array.isArray(parent)) {
        const idx = parseInt(last, 10);
        // Python list[int] raises IndexError out of range; mirror it (RFC 6902 requires the
        // target to exist for replace/test). 中文：越界/缺 key 必须报错（对齐 Python 与 RFC 6902）。
        if (!(idx >= 0 && idx < parent.length)) throw new Error(`Index out of range: ${last}`);
        return parent[idx];
    }
    if (parent === null || typeof parent !== "object") {
        throw new Error(`Path traverses a non-container: ${last}`);
    }
    if (!Object.prototype.hasOwnProperty.call(parent, last)) {
        throw new Error(`Key not found: ${last}`);
    }
    return parent[last];
}

function removeAt(doc, tokens) {
    const [parent, last] = walk(doc, tokens);
    if (Array.isArray(parent)) parent.splice(parseInt(last, 10), 1);
    else delete parent[last];
}

function addAt(doc, tokens, value) {
    if (tokens.length === 0) return value;
    const [parent, last] = walk(doc, tokens);
    if (Array.isArray(parent)) {
        const idx = last === "-" ? parent.length : parseInt(last, 10);
        parent.splice(idx, 0, value);
    } else {
        parent[last] = value;
    }
    return doc;
}

/** Deep equality for plain JSON values (for the "test" op). JSON 值深度相等（test 用）。 */
function jsonDeepEqual(a, b) {
    if (a === b) return true;
    if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
    const aArr = Array.isArray(a);
    const bArr = Array.isArray(b);
    if (aArr !== bArr) return false;
    if (aArr) {
        if (a.length !== b.length) return false;
        for (let i = 0; i < a.length; i++) if (!jsonDeepEqual(a[i], b[i])) return false;
        return true;
    }
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    for (const k of ka) {
        if (!Object.prototype.hasOwnProperty.call(b, k) || !jsonDeepEqual(a[k], b[k])) return false;
    }
    return true;
}

/** Apply an RFC 6902 operation array. 应用 RFC 6902 操作数组。 */
function applyJsonPatch(doc, ops) {
    if (!Array.isArray(ops)) {
        throw new Error("patch in jsonpatch mode must be an array of operations");
    }
    for (const op of ops) {
        if (op === null || typeof op !== "object") {
            throw new Error("patch in jsonpatch mode must be an array of operations");
        }
        const kind = op.op;
        const tokens = jsonPointerTokens(op.path || "");
        if (kind === "add") {
            doc = addAt(doc, tokens, structuredClone(op.value));
        } else if (kind === "replace") {
            getAt(doc, tokens); // throws if absent 不存在会抛错
            doc = addAt(doc, tokens, structuredClone(op.value));
        } else if (kind === "remove") {
            removeAt(doc, tokens);
        } else if (kind === "move" || kind === "copy") {
            const srcTokens = jsonPointerTokens(op.from || "");
            const value = structuredClone(getAt(doc, srcTokens));
            if (kind === "move") removeAt(doc, srcTokens);
            doc = addAt(doc, tokens, value);
        } else if (kind === "test") {
            if (!jsonDeepEqual(getAt(doc, tokens), op.value)) {
                throw new Error(`test failed: ${op.path}`);
            }
        } else {
            throw new Error(`Unknown op: ${kind}`);
        }
    }
    return doc;
}

/** Apply an RFC 7396 merge patch (mutates target like the Python version). 应用 RFC 7396 合并补丁。 */
function applyMergePatch(target, patch) {
    if (patch === null || typeof patch !== "object" || Array.isArray(patch)) {
        return structuredClone(patch);
    }
    if (target === null || typeof target !== "object" || Array.isArray(target)) {
        target = {};
    }
    for (const [k, v] of Object.entries(patch)) {
        if (v === null) delete target[k];
        else target[k] = applyMergePatch(target[k], v);
    }
    return target;
}

// ----------------------------------------------------------------------------
// Tool execution 工具执行
// ----------------------------------------------------------------------------

/** Stringify a bridge result like Python's str()/json.dumps(): strings stay raw, others JSON-serialize.
 * 中文：结果为字符串则原样返回，否则 JSON 序列化（等价 Python str()/json.dumps()）。 */
function asText(result) {
    return typeof result === "string" ? result : JSON.stringify(result);
}

/** Fetch a required argument or raise (matches Python's KeyError behavior). 取必填参数，缺失即报错。 */
function reqArg(arguments_, key) {
    if (!(key in arguments_) || arguments_[key] === undefined) {
        throw new Error(`missing required argument: ${key}`);
    }
    return arguments_[key];
}

async function patchProjectJsonTool(arguments_) {
    const mode = arguments_.mode ?? "merge";
    const patch = arguments_.patch;
    const ping = await callBridge("ping");
    const projectFile = ping && typeof ping === "object" ? ping.projectFile : null;
    if (!projectFile) {
        throw new Error("No project is open in EEZ Studio");
    }

    const raw = await fs.promises.readFile(projectFile, "utf8");
    const doc = JSON.parse(raw);
    let result;
    if (mode === "merge") {
        if (patch === null || typeof patch !== "object" || Array.isArray(patch)) {
            throw new Error("patch in merge mode must be an object");
        }
        result = applyMergePatch(doc, patch);
    } else if (mode === "jsonpatch") {
        result = applyJsonPatch(doc, patch);
    } else {
        throw new Error(`Unknown mode: ${mode} (merge or jsonpatch)`);
    }

    const content = JSON.stringify(result, null, 2);
    const bridgeResult = await callBridge("write_project_json", { content });
    return [
        {
            type: "text",
            text: JSON.stringify({
                ok: true,
                mode,
                projectFile,
                bytes: content.length,
                detail: bridgeResult,
            }),
        },
    ];
}

async function callTool(name, arguments_) {
    if (name === "ping") {
        const result = await callBridge("ping");
        return [{ type: "text", text: JSON.stringify(result) }];
    }

    if (name === "screenshot" || name === "screenshot_object") {
        // Bridge returns a dataUrl ("data:image/png;base64,...."); split off the base64 payload.
        // 中文：桥返回 dataUrl，拆出 base64，附 PNG 图片内容块。
        const result =
            name === "screenshot"
                ? await callBridge("screenshot")
                : await callBridge("screenshot_object", {
                      path: reqArg(arguments_, "path"),
                      padding: arguments_.padding ?? 8,
                  });
        const dataUrl = result.dataUrl;
        const base64Data = dataUrl.includes(",") ? dataUrl.split(",")[1] : dataUrl;
        const text =
            name === "screenshot"
                ? `screenshot saved: ${result.file}`
                : `widget screenshot ${result.rect}: ${result.file}`;
        return [
            { type: "text", text },
            { type: "image", data: base64Data, mimeType: "image/png" },
        ];
    }

    if (name === "read_ir" || name === "read_project_json") {
        const result = await callBridge(name);
        return [{ type: "text", text: asText(result).slice(0, MAX_TEXT_LEN) }];
    }

    if (name === "write_ir") {
        const result = await callBridge("write_ir", { content: reqArg(arguments_, "content") });
        return [{ type: "text", text: asText(result) }];
    }

    if (name === "compile") {
        const result = await callBridge("compile");
        const ok = (result && result.ok) === true;
        const output = (result && result.output) || "";
        const status = ok ? "✓ compile OK" : "✗ compile FAILED";
        return [{ type: "text", text: `${status}\n${String(output).slice(0, 5000)}` }];
    }

    if (name === "reload") {
        const result = await callBridge("reload");
        return [{ type: "text", text: asText(result) }];
    }

    if (name === "navigate") {
        const result = await callBridge("navigate", { screen: reqArg(arguments_, "screen") });
        return [{ type: "text", text: asText(result) }];
    }

    if (name === "write_project_json") {
        const result = await callBridge("write_project_json", {
            content: reqArg(arguments_, "content"),
            reload: arguments_.reload ?? true,
        });
        return [{ type: "text", text: asText(result) }];
    }

    if (name === "patch_project_json") {
        return await patchProjectJsonTool(arguments_);
    }

    // Remaining tools: pass the whitelisted arguments through to the bridge, return the
    // JSON-serialized result. 其余工具参数透传给桥。
    const passthrough = {
        read_output: ["section"],
        check: [],
        build_project: [],
        list_styles: [],
        update_style: ["style", "part", "state", "properties"],
        create_style: ["name", "forWidgetType"],
        delete_style: ["style"],
        set_theme_color: ["color", "value", "theme"],
        add_color: ["name", "value"],
        list_projects: [],
        select_project: ["project"],
        open_project: ["path"],
        debug_start: ["mode"],
        debug_stop: [],
        debug_control: ["op"],
        debug_status: [],
        read_variable: ["name"],
        write_variable: ["name", "value"],
        list_objects: ["screen", "path"],
        get_object: ["path", "depth"],
        update_object: ["path", "properties"],
        create_widget: ["type", "parent", "properties"],
        delete_object: ["path"],
        create_screen: ["name", "width", "height"],
        undo: [],
        redo: [],
        goto_object: ["path"],
        get_selection: [],
        send_input: ["op", "x", "y", "dx", "dy"],
        set_preview_theme: ["theme"],
        create_project: ["path", "width", "height", "lvglVersion"],
        list_assets: [],
        add_font: ["name", "ttf_path", "size", "bpp", "ranges", "symbols"],
        add_image: ["image_path", "name", "bpp"],
    };
    if (name in passthrough) {
        const keys = passthrough[name];
        const args = {};
        for (const k of keys) {
            if (k in arguments_) args[k] = arguments_[k];
        }
        const result = await callBridge(name, args);
        return [{ type: "text", text: JSON.stringify(result ?? null) }];
    }

    return [{ type: "text", text: `Unknown tool: ${name}` }];
}

// ----------------------------------------------------------------------------
// Live resources (eez://): fetched on demand from the bridge, subscribable
// 活资源，可订阅（内容变化推送 resources/updated）
// ----------------------------------------------------------------------------

const LIVE_RESOURCES = {
    "eez://checks": {
        name: "Live checks 实时检查",
        description:
            "Error/warning counts and messages (pushed on change once subscribed) 错误/警告计数与消息（订阅后变更即推送）",
        mimeType: "application/json",
    },
    "eez://debug": {
        name: "Runtime state 运行时状态",
        description:
            "Debugger state, current page and log tail (pushed on change once subscribed) 调试器状态、当前页与日志尾部（订阅后变更即推送）",
        mimeType: "application/json",
    },
    "eez://state": {
        name: "Project state 工程状态",
        description:
            "Active project and selection (pushed on change once subscribed) 活动工程与选中对象（订阅后变更即推送）",
        mimeType: "application/json",
    },
};

const subscribedUris = new Set();
const resourceHashes = new Map();

async function liveResourceContent(uri) {
    if (uri === "eez://checks") {
        const r = await callBridge("read_output", { section: "checks" });
        return JSON.stringify(r ?? null);
    }
    if (uri === "eez://debug") {
        const r = await callBridge("debug_status");
        return JSON.stringify(r ?? null);
    }
    if (uri === "eez://state") {
        const r = await callBridge("ping");
        const sel = await callBridge("get_selection");
        return JSON.stringify({ ping: r, selection: sel });
    }
    throw new Error(`Unknown resource: ${uri}`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Poll live resources every 2s (parallel fetches); on content change push
 * notifications/resources/updated. The first snapshot is stored silently.
 * 中文：每 2 秒并行轮询活资源，md5 变化即推送 updated 通知；首次快照不推送。
 */
async function resourceWatcher() {
    for (;;) {
        try {
            await sleep(2000);
            if (subscribedUris.size === 0) continue;
            const uris = [...subscribedUris];
            // Each bridge call takes ~0.3-3s; parallelize to cut the cycle. 并行拉取缩短周期。
            const results = await Promise.allSettled(uris.map((u) => liveResourceContent(u)));
            for (let i = 0; i < uris.length; i++) {
                const r = results[i];
                if (r.status !== "fulfilled") continue; // e.g. bridge offline 桥不在线等场景
                const h = crypto.createHash("md5").update(r.value, "utf8").digest("hex");
                const prev = resourceHashes.get(uris[i]);
                resourceHashes.set(uris[i], h);
                if (prev !== undefined && prev !== h) {
                    log(`${uris[i]} changed, notifying`);
                    send({
                        jsonrpc: "2.0",
                        method: "notifications/resources/updated",
                        params: { uri: uris[i] },
                    });
                }
            }
        } catch (e) {
            log(`watcher iteration failed: ${e?.message || e}`);
        }
    }
}

// ----------------------------------------------------------------------------
// Static resources 静态资源
// ----------------------------------------------------------------------------

function listResources() {
    const resources = [
        {
            uri: pathToFileURL(path.join(WORKDIR, "sg8.ir.json")).href,
            name: "Current IR JSON 当前 IR JSON",
            description: "UI description source file of the EEZ Studio project 工程界面描述源文件",
            mimeType: "application/json",
        },
        {
            uri: pathToFileURL(path.join(WORKDIR, "IR_SCHEMA.md")).href,
            name: "IR format spec IR 格式规范",
            description: "Structure and EEZ constraints of the IR JSON IR 结构定义与约束",
            mimeType: "text/markdown",
        },
        {
            uri: pathToFileURL(path.join(WORKDIR, "SKILL.md")).href,
            name: "EEZ Studio skill doc EEZ Studio 技能文档",
            description: "Heuristic rules for generating EEZ Studio LVGL UIs LVGL 界面生成经验规则",
            mimeType: "text/markdown",
        },
    ];
    for (const [uri, def] of Object.entries(LIVE_RESOURCES)) {
        resources.push({ uri, name: def.name, description: def.description, mimeType: def.mimeType });
    }
    return resources;
}

async function readResource(params) {
    const uri = String(params.uri || "");
    if (uri.startsWith("eez://")) {
        let text;
        try {
            text = await liveResourceContent(uri);
        } catch (e) {
            text = `read failed: ${e?.message || e}`;
        }
        return { contents: [{ uri, mimeType: "application/json", text: text.slice(0, MAX_TEXT_LEN) }] };
    }
    let filePath;
    try {
        filePath = fileURLToPath(uri);
    } catch {
        filePath = uri.replace(/^file:\/\/\//, "").replace(/^file:\/\//, ""); // lenient fallback 宽松回退
    }
    let text;
    try {
        text = await fs.promises.readFile(filePath, "utf8");
    } catch (e) {
        text = `read failed: ${e?.message || e}`;
    }
    return { contents: [{ uri, mimeType: "text/plain", text: text.slice(0, MAX_TEXT_LEN) }] };
}

// ----------------------------------------------------------------------------
// Prompt definitions 提示词定义
// ----------------------------------------------------------------------------

function promptCommon(lang, schemaText, skillText) {
    if (lang === "zh") {
        return `== EEZ Studio LVGL 工具（经 MCP 桥调用）==

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
${schemaText}

== 经验规则（摘要）==
${skillText}`;
    }
    return `== EEZ Studio LVGL tools (via MCP bridge) ==

Workflow: read_ir -> edit -> compile -> reload -> navigate -> screenshot

Other capabilities:
- Diagnostics: read_output(checks/output), check, build_project, goto_object (jump to the failing object)
- Widget-level editing: list_objects -> get_object -> update_object / create_widget / delete_object / create_screen (surgical edits by path, undoable; prefer this group for small changes instead of full rewrites)
- Styles/themes: list_styles, update_style, create_style, delete_style, set_theme_color, add_color (take effect immediately, no reload)
- Project file: read_project_json / write_project_json (bypasses the IR; auto-reloads after write; for large structural changes only)
- Multi-project: list_projects, select_project, open_project (tools act on the active tab)
- Runtime debugging: debug_start -> send_input click/swipe to test interactions, screenshot the simulator, debug_control(pause/resume/step), read/write_variable, debug_status, debug_stop
- Theme preview: set_preview_theme + screenshot to verify each theme
- New project: create_project (minimal LVGL template); structural patch: patch_project_json (merge/jsonpatch)

== IR spec (excerpt) ==
${schemaText}

== Heuristic rules (excerpt) ==
${skillText}`;
}

const PROMPTS = [
    {
        name: "modify_ui",
        description: "Modify the current LVGL UI (English).",
        arguments: [{ name: "requirement", description: "What to change", required: true }],
    },
    {
        name: "modify_ui_zh",
        description: "修改当前 LVGL 界面（中文版）。",
        arguments: [{ name: "requirement", description: "要改什么", required: true }],
    },
    {
        name: "create_ui",
        description: "Create a new LVGL UI from an HTML mockup (English).",
        arguments: [{ name: "html_path", description: "Path to the mockup HTML", required: true }],
    },
    {
        name: "create_ui_zh",
        description: "从设计稿 HTML 创建新的 LVGL 界面（中文版）。",
        arguments: [{ name: "html_path", description: "设计稿 HTML 文件路径", required: true }],
    },
];

async function getPrompt(params) {
    const name = params.name;
    const arguments_ = params.arguments || {};

    // Tolerate missing docs: prompts still work, just without the excerpt.
    // 中文：文档缺失时优雅降级：prompt 仍可用，只是少了摘要段。
    async function readDoc(fileName, limit, fallback) {
        try {
            const text = await fs.promises.readFile(path.join(WORKDIR, fileName), "utf8");
            return text.slice(0, limit);
        } catch {
            return fallback;
        }
    }

    const schemaText = await readDoc("IR_SCHEMA.md", 5000, "(IR_SCHEMA.md not found in EEZ_WORKDIR)");
    const skillText = await readDoc("SKILL.md", 3000, "(SKILL.md not found in EEZ_WORKDIR)");

    const lang = name.endsWith("_zh") ? "zh" : "en";
    const common = promptCommon(lang, schemaText, skillText);
    const kind = lang === "zh" ? name.slice(0, -3) : name;

    let tail;
    if (kind === "modify_ui") {
        const req = arguments_.requirement || "";
        tail =
            lang === "zh"
                ? `\n\n用户需求：${req}\n\n先用 read_ir 读当前 IR，然后用内置 edit 工具做手术式修改（小改动），改完 compile → reload → navigate → screenshot 自查。`
                : `\n\nUser requirement: ${req}\n\nFirst read the current IR with read_ir, make surgical edits with the built-in edit tool for small changes, then compile -> reload -> navigate -> screenshot to self-check.`;
    } else if (kind === "create_ui") {
        const htmlPath = arguments_.html_path || "";
        tail =
            lang === "zh"
                ? `\n\n设计稿：${htmlPath}\n\n读取设计稿 HTML，分析布局/颜色/文字/交互，从零创建 IR JSON（每个卡片区域用 panel 包裹），然后 compile → reload → navigate → screenshot 自查。`
                : `\n\nMockup: ${htmlPath}\n\nRead the mockup HTML, analyze layout/colors/text/interactions, create the IR JSON from scratch (wrap each card region in a panel), then compile -> reload -> navigate -> screenshot to self-check.`;
    } else {
        return { messages: [] };
    }

    return {
        messages: [
            {
                role: "user",
                content: { type: "text", text: common + tail },
            },
        ],
    };
}

// ----------------------------------------------------------------------------
// Progress notifications for long operations (client opt-in via _meta.progressToken)
// 长操作进度通知（客户端在 _meta.progressToken 传入令牌时启用）
// ----------------------------------------------------------------------------

const LONG_TOOLS = new Set([
    "check",
    "build_project",
    "debug_start",
    "compile",
    "write_project_json",
    "patch_project_json",
    "add_font",
]);

/** The client may normalize _meta into snake_case progress_token (camelCase also arrives).
 * 中文：客户端可能把 key 归一化为 progress_token，两种都查。 */
function extractProgressToken(params) {
    const meta = params?._meta;
    if (meta && typeof meta === "object") {
        return meta.progressToken ?? meta.progress_token ?? null;
    }
    return null;
}

/** Run a tool with a 1s heartbeat progress notification. An immediate first beat ensures
 * fast tools (<1s, e.g. a cached check) still report progress at least once.
 * 中文：带 1 秒心跳跑长工具；先立即发一拍，快速完成的工具也能收到至少一次心跳。 */
async function runWithProgress(token, name, promise) {
    const start = Date.now();
    let lastSent = 1;
    const beat = () => {
        send({
            jsonrpc: "2.0",
            method: "notifications/progress",
            params: { progressToken: token, progress: lastSent, message: `${name}: waited ${lastSent}s` },
        });
    };
    beat();
    const timer = setInterval(() => {
        const elapsed = Math.max(1, Math.floor((Date.now() - start) / 1000));
        if (elapsed > lastSent) {
            lastSent = elapsed;
            beat();
        }
    }, 1000);
    try {
        return await promise;
    } finally {
        clearInterval(timer);
    }
}

async function handleCallTool(params) {
    const name = params.name;
    const arguments_ = params.arguments || {};
    try {
        const promise = callTool(name, arguments_);
        const token = extractProgressToken(params);
        if (LONG_TOOLS.has(name) && token != null) {
            return { content: await runWithProgress(token, name, promise) };
        }
        return { content: await promise };
    } catch (e) {
        let errorHint = "";
        const msg = String(e?.message || e);
        if (msg.includes("Connect") || msg.includes("connect")) {
            errorHint = "\nHint: EEZ Studio may not be running; please start EEZ Studio first.";
        }
        return { content: [{ type: "text", text: `Error: ${msg}${errorHint}` }], isError: true };
    }
}

// ----------------------------------------------------------------------------
// JSON-RPC dispatch 分发
// ----------------------------------------------------------------------------

class RpcError extends Error {
    constructor(code, message) {
        super(message);
        this.code = code;
    }
}

function handleInitialize(params) {
    const requested = params?.protocolVersion;
    const protocolVersion =
        requested && SUPPORTED_PROTOCOL_VERSIONS.has(requested) ? requested : LATEST_PROTOCOL_VERSION;
    return {
        protocolVersion,
        capabilities: {
            tools: { listChanged: false },
            resources: { subscribe: true, listChanged: false },
            prompts: { listChanged: false },
        },
        serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
        instructions:
            "Drive EEZ Studio (LVGL UI editor) through the bridge tools: read_ir/list_objects to inspect, " +
            "update_object/create_widget to edit, check/compile to validate, screenshot to see. " +
            "中文：通过桥操控 EEZ Studio——读 IR/对象树，改对象/样式，check/compile 校验，screenshot 查看。",
    };
}

async function dispatch(method, params) {
    switch (method) {
        case "initialize":
            return handleInitialize(params);
        case "ping":
            return {};
        case "tools/list":
            return { tools: TOOLS };
        case "tools/call":
            return await handleCallTool(params);
        case "resources/list":
            return { resources: listResources() };
        case "resources/templates/list":
            return { resourceTemplates: [] };
        case "resources/read":
            return await readResource(params);
        case "resources/subscribe": {
            const uri = String(params?.uri || "");
            subscribedUris.add(uri);
            // Baseline snapshot right away (best-effort): the first snapshot is silent, so
            // without this a change landing before the first poll tick would be swallowed
            // as the baseline. Errors (bridge offline) fall through to the periodic poll.
            // 中文：订阅即取基线快照（尽力而为）；否则首次轮询前的变化会被当成基线吞掉。
            liveResourceContent(uri)
                .then((content) => {
                    if (subscribedUris.has(uri) && !resourceHashes.has(uri)) {
                        resourceHashes.set(uri, crypto.createHash("md5").update(content, "utf8").digest("hex"));
                    }
                })
                .catch(() => {});
            return {};
        }
        case "resources/unsubscribe": {
            const uri = String(params?.uri || "");
            subscribedUris.delete(uri);
            resourceHashes.delete(uri);
            return {};
        }
        case "prompts/list":
            return { prompts: PROMPTS };
        case "prompts/get":
            return await getPrompt(params);
        case "logging/setLevel":
            return {};
        default:
            throw new RpcError(-32601, `Method not found: ${method}`);
    }
}

/** Handle one decoded JSON-RPC message from stdin. 处理一行输入消息。 */
async function handleMessage(msg) {
    if (!msg || typeof msg !== "object" || Array.isArray(msg) || msg.jsonrpc !== "2.0") {
        if (msg && typeof msg === "object" && msg.id !== undefined) {
            sendError(msg.id, -32600, "Invalid Request");
        } else {
            log("ignored non-JSON-RPC input");
        }
        return;
    }
    if (typeof msg.method !== "string") {
        return; // response to a server request — we never send any; ignore 我们不主动发请求，忽略
    }
    if (msg.id === undefined) {
        // Notification: initialized/cancelled/roots need no reply. 通知无需应答。
        switch (msg.method) {
            case "notifications/initialized":
                log("client initialized");
                break;
            case "notifications/cancelled":
                log(`cancelled: ${msg.params?.requestId}`);
                break;
            default:
                log(`ignored notification: ${msg.method}`);
        }
        return;
    }
    try {
        const result = await dispatch(msg.method, msg.params || {});
        sendResult(msg.id, result);
    } catch (e) {
        if (e instanceof RpcError) {
            sendError(msg.id, e.code, e.message);
        } else {
            log(`error handling ${msg.method}: ${e?.stack || e}`);
            sendError(msg.id, -32603, `internal error: ${e?.message || e}`);
        }
    }
}

// ----------------------------------------------------------------------------
// Main entry 主入口
// ----------------------------------------------------------------------------

async function main() {
    process.stdin.setEncoding("utf8");

    let buffer = "";
    process.stdin.on("data", (chunk) => {
        // Newline-delimited JSON (NOT Content-Length framing). 换行分隔 JSON，不是 Content-Length 帧。
        buffer += chunk;
        let idx;
        while ((idx = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, idx).replace(/\r$/, "");
            buffer = buffer.slice(idx + 1);
            if (line.trim() === "") continue;
            let msg;
            try {
                msg = JSON.parse(line);
            } catch {
                sendError(null, -32700, "Parse error");
                continue;
            }
            handleMessage(msg).catch((e) => log(`unhandled: ${e?.stack || e}`));
        }
    });

    process.stdin.on("close", async () => {
        // Client closed stdin: flush pending writes then exit. 客户端关闭，冲刷后退出。
        await writeChain.catch(() => {});
        process.exit(0);
    });
    process.stdin.on("error", (e) => log(`stdin error: ${e?.message || e}`));

    process.on("SIGINT", () => process.exit(0));
    process.on("SIGTERM", () => process.exit(0));

    resourceWatcher().catch((e) => log(`watcher crashed: ${e?.stack || e}`));

    log(`eez-studio MCP server (node) started; bridge=${BRIDGE_URL} workdir=${WORKDIR}`);
}

// Run only as the main script (like Python's `if __name__ == "__main__"`); importing the
// module for unit tests must not start the server. 中文：仅主脚本时启动，导入测试不拉起服务。
const isMain =
    process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isMain) {
    main().catch((e) => {
        log(`fatal: ${e?.stack || e}`);
        process.exit(1);
    });
}

// Testable exports (importing does not start the server). 可测试导出（导入不会启动服务）。
export { jsonPointerTokens, applyJsonPatch, applyMergePatch };
