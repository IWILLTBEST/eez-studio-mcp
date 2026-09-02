/*
 * eez-studio-mcp extension (prototype)
 *
 * Dual-process extension using the process-namespaced IExtensionApi
 * (eez-open/studio PR #1043 + #1044):
 *   - main:     starts the opt-in localhost HTTP bridge (127.0.0.1:17621)
 *   - renderer: receives tool requests over Electron IPC and executes them,
 *               reading editor state through api.renderer.* members
 *               (requireModule for third-party packages, getOpenProjects,
 *               getActiveProjectStore for Studio internals)
 *
 * init() detects the runtime shape: current upstream exposes api.main /
 * api.renderer; older patched runtimes exposed fromProcess + flat members
 * or a requireModule module whitelist. Each is used when present.
 */

"use strict";

const batch1 = require("./lib/tools-batch1.js");
const batch2 = require("./lib/tools-batch2.js");

// Prototype: 17621 while the built-in fork bridge owns 17620; will become
// 17620 once the built-in bridge is retired. 原型期与内置桥并行，正式后接管 17620
const PORT = parseInt(process.env.EEZ_MCP_PORT || 17621, 10);
const REQUEST_TIMEOUT_MS = 120000;

let httpServer;

// ----------------------------------------------------------------------------
// Main process: local HTTP bridge 主进程：本地 HTTP 桥
// ----------------------------------------------------------------------------

function startBridgeMain() {
    const http = require("http");
    const { ipcMain, BrowserWindow } = require("electron");
    const crypto = require("crypto");

    function dispatchToRenderer(tool, args) {
        return new Promise((resolve, reject) => {
            const windows = BrowserWindow.getAllWindows().filter(
                w => !w.isDestroyed()
            );
            if (windows.length === 0) {
                reject(new Error("no open EEZ Studio window"));
                return;
            }

            const requestId = crypto.randomUUID();
            const channel = `eez-mcp-tool-result/${requestId}`;
            const timer = setTimeout(() => {
                ipcMain.removeListener(channel, onResult);
                reject(new Error(`tool ${tool} timeout (${REQUEST_TIMEOUT_MS}ms)`));
            }, REQUEST_TIMEOUT_MS);

            function onResult(_event, payload) {
                clearTimeout(timer);
                ipcMain.removeListener(channel, onResult);
                if (payload && payload.error) {
                    reject(new Error(String(payload.error)));
                } else {
                    resolve(payload && payload.result);
                }
            }

            ipcMain.on(channel, onResult);
            // Broadcast: whichever window has the dispatcher answers
            for (const w of windows) {
                w.webContents.send("eez-mcp-tool-request", {
                    requestId,
                    tool,
                    args
                });
            }
        });
    }

    httpServer = http.createServer((req, res) => {
        const url = new URL(req.url ?? "/", "http://127.0.0.1");

        if (req.method === "GET" && url.pathname === "/health") {
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ ok: true }));
            return;
        }

        if (req.method === "POST" && url.pathname === "/tool") {
            const chunks = [];
            req.on("data", chunk => chunks.push(chunk));
            req.on("end", async () => {
                let body;
                try {
                    body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
                } catch (err) {
                    res.writeHead(400, { "Content-Type": "application/json" });
                    res.end(JSON.stringify({ error: "invalid JSON body" }));
                    return;
                }
                if (!body || typeof body.tool !== "string") {
                    res.writeHead(400, { "Content-Type": "application/json" });
                    res.end(JSON.stringify({ error: "missing tool name" }));
                    return;
                }
                try {
                    const result = await dispatchToRenderer(
                        body.tool,
                        body.args ?? {}
                    );
                    res.writeHead(200, { "Content-Type": "application/json" });
                    res.end(JSON.stringify({ ok: true, result }));
                } catch (err) {
                    res.writeHead(500, { "Content-Type": "application/json" });
                    res.end(
                        JSON.stringify({
                            ok: false,
                            error: String((err && err.message) || err)
                        })
                    );
                }
            });
            return;
        }

        res.writeHead(404, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "not found" }));
    });

    httpServer.on("error", err => {
        console.warn(`[eez-mcp] bridge error: ${err.message}`);
    });

    httpServer.listen(PORT, "127.0.0.1", () => {
        console.log(`[eez-mcp] bridge listening on http://127.0.0.1:${PORT}`);
    });
}

// ----------------------------------------------------------------------------
// Renderer process: IPC dispatcher 渲染进程：IPC 分发器
// ----------------------------------------------------------------------------

let studioApi;

function initRenderer(api) {
    studioApi = api;
    const { ipcRenderer } = require("electron");

    // mobx for the tools (toJS/runInAction) via requireModule — batch2.setMobx
    // forwards to batch1 as well
    try {
        const rapi = rendererApi();
        if (rapi && typeof rapi.requireModule === "function") {
            batch2.setMobx(rapi.requireModule("mobx"));
        }
    } catch (err) {
        console.warn("[eez-mcp] mobx unavailable, tools limited:", err.message);
    }
    batch2.setRendererApiGetter(rendererApi);

    ipcRenderer.on("eez-mcp-tool-request", async (_event, payload) => {
        let result;
        let error;
        try {
            result = await executeTool(payload.tool, payload.args ?? {});
        } catch (err) {
            error = String((err && err.message) || err);
        }
        ipcRenderer.send(`eez-mcp-tool-result/${payload.requestId}`, {
            result,
            error
        });
    });

    console.log("[eez-mcp] renderer dispatcher registered");
}

// Active ProjectStore per request (store instances get replaced on reload).
function toolContext() {
    const api = rendererApi();
    const store =
        api && typeof api.getActiveProjectStore === "function"
            ? api.getActiveProjectStore()
            : undefined;
    return store ? { projectStore: store } : undefined;
}

// ----------------------------------------------------------------------------
// Tool execution (prototype: ping reads real editor state, everything else
// waits for the full tool library)
// ----------------------------------------------------------------------------

// The renderer side of the extension API across runtime generations:
// current upstream nests it under api.renderer (PR #1044), older patched
// runtimes exposed the same members flat next to fromProcess.
function rendererApi() {
    if (!studioApi) {
        return undefined;
    }
    if (studioApi.renderer) {
        return studioApi.renderer;
    }
    if (studioApi.getOpenProjects || studioApi.requireModule) {
        return studioApi;
    }
    return undefined;
}

// Open projects via getOpenProjects() (PR #1044), falling back to the oldest
// shape where requireModule still exported home/tabs-store. null = neither.
function openProjects() {
    const api = rendererApi();
    if (!api) {
        return null;
    }
    if (typeof api.getOpenProjects === "function") {
        return api.getOpenProjects();
    }
    if (typeof api.requireModule === "function") {
        try {
            const tabsStore = api.requireModule("home/tabs-store");
            return tabsStore.tabs.tabs
                .filter(t => t instanceof tabsStore.ProjectEditorTab)
                .map(t => ({
                    name: String(t.filePath || "").replace(/^.*[\\/]/, ""),
                    filePath: t.filePath,
                    active: t === tabsStore.tabs.activeTab
                }));
        } catch (err) {
            console.warn(
                "[eez-mcp] legacy requireModule fallback failed:",
                err && err.message
            );
        }
    }
    return null;
}

async function executeTool(tool, args) {
    const a = args ?? {};
    switch (tool) {
        case "ping": {
            const projects = openProjects();
            const api = rendererApi();
            const projectStore =
                api && typeof api.getActiveProjectStore === "function"
                    ? api.getActiveProjectStore()
                    : undefined;
            return {
                pong: true,
                from: "eez-studio-mcp extension",
                process: typeof window !== "undefined" ? "renderer" : "unknown",
                studioAccess: projects
                    ? projects.length || "none-open"
                    : "unavailable",
                projects: projects || undefined,
                activeProjectLoaded: projectStore
                    ? !!projectStore.project
                    : undefined
            };
        }
        case "echo":
            return args;
        case "list_screens": {
            const ctx = toolContext();
            if (!ctx) throw new Error("no active project editor open");
            const project = ctx.projectStore.project;
            return {
                project: ctx.projectStore.filePath,
                screens: (project.userPages || []).map(p => ({
                    name: p.name,
                    widgets: (p.components || []).length
                })),
                userWidgets: (project.userWidgets || []).map(w => w.name)
            };
        }

        // ---- Batch 1: store-graph editing loop (undoable, auto-saved) ----
        case "list_objects":
            return batch1.listObjects(toolContext(), a.screen, a.path);
        case "get_object":
            return batch1.getObject(toolContext(), a.path, a.depth);
        case "update_object":
            return await batch1.updateObject(
                toolContext(),
                a.path,
                a.properties
            );
        case "delete_object":
            return await batch1.deleteObject(toolContext(), a.path);
        case "undo":
            return await batch1.undoProject(toolContext());
        case "redo":
            return await batch1.redoProject(toolContext());
        case "navigate":
            return batch1.navigateToScreen(toolContext(), a.screen);
        case "goto_object":
            return batch1.gotoObject(toolContext(), a.path);
        case "get_selection":
            return batch1.getSelection(toolContext());
        case "screenshot":
            return await batch1.screenshot(toolContext(), a.out);
        case "screenshot_object":
            return await batch2.screenshotObject(
                toolContext(),
                a.path,
                a.padding
            );
        case "read_output":
            return batch1.readOutputSection(
                toolContext(),
                a.section === "checks" ? "checks" : "output"
            );
        case "check":
            return await batch1.runCheck(toolContext());
        case "build_project":
            return await batch2.runBuild(toolContext());

        // ---- styles & themes ----
        case "list_styles":
            return batch2.listStyles(toolContext());
        case "update_style":
            return await batch2.updateStyle(
                toolContext(),
                a.style,
                a.part,
                a.state,
                a.properties
            );
        case "create_style":
            return await batch2.createStyle(
                toolContext(),
                a.name,
                a.forWidgetType
            );
        case "delete_style":
            return await batch2.deleteStyle(toolContext(), a.name);
        case "set_theme_color":
            return await batch2.setThemeColor(
                toolContext(),
                a.color,
                a.value,
                a.theme
            );
        case "add_color":
            return await batch2.addThemeColor(
                toolContext(),
                a.name,
                a.value
            );
        case "set_preview_theme":
            return batch2.setPreviewTheme(toolContext(), a.theme);

        // ---- creation ----
        case "create_widget":
            return await batch2.createWidget(
                toolContext(),
                a.type,
                a.parent,
                a.properties
            );
        case "create_screen":
            return await batch2.createScreen(
                toolContext(),
                a.name,
                a.width,
                a.height
            );

        // ---- assets ----
        case "list_assets":
            return batch2.listAssets(toolContext());
        case "add_font":
            return await batch2.addFont(
                toolContext(),
                a.name,
                a.ttf,
                a.size,
                a.bpp,
                a.ranges,
                a.symbols
            );
        case "add_image":
            return await batch2.addImage(
                toolContext(),
                a.image,
                a.name,
                a.bpp
            );

        // ---- runtime debug / variables / input ----
        case "debug_status":
            return batch2.debugStatus(toolContext());
        case "debug_start":
            return await batch2.debugStart(toolContext(), a.mode);
        case "debug_stop":
            return await batch2.debugStop(toolContext());
        case "debug_control":
            return await batch2.debugControl(toolContext(), a.op);
        case "read_variable":
            return batch2.readVariable(toolContext(), a.name);
        case "write_variable":
            return batch2.writeVariable(toolContext(), a.name, a.value);
        case "send_input":
            return await batch2.sendInput(
                toolContext(),
                a.op,
                a.x,
                a.y,
                a.dx,
                a.dy
            );

        // ---- project file IO ----
        case "read_project_json":
            return batch2.readProjectJson(toolContext());
        case "write_project_json":
            return await batch2.writeProjectJson(
                toolContext(),
                a.content,
                a.reload !== false
            );
        case "reload":
            return await batch2.reloadProject(toolContext());

        // ---- Multi-project: activateProjectTab/openProject landed in the
        // Batch-2 API round (eez-open/studio#1042, option B). When absent
        // (older runtimes) only the already-active target succeeds.
        case "select_project":
        case "open_project": {
            const api = rendererApi();
            const projects = openProjects() || [];
            let target = null;
            if (typeof a.index === "number") {
                target = projects[a.index] ?? null;
            } else if (a.path || a.project) {
                const want = String(a.path ?? a.project);
                target =
                    projects.find(p => p.filePath === want) ??
                    projects.find(p => p.name === want) ??
                    null;
            } else {
                target = projects.find(p => p.active) ?? null;
            }

            if (a.path && !target && api && typeof api.openProject === "function") {
                // open_project with a path that isn't open yet → open a tab
                api.openProject(String(a.path));
                return { opened: String(a.path), note: "opening — poll list_projects until loaded" };
            }
            if (!target) {
                throw new Error(
                    `project not found among ${projects.length} open tabs` +
                        (api && typeof api.openProject === "function"
                            ? ""
                            : " and openProject is unavailable on this runtime")
                );
            }

            if (target.active) {
                return {
                    selected: target.filePath,
                    activated: false,
                    alreadyActive: true
                };
            }
            if (api && typeof api.activateProjectTab === "function") {
                api.activateProjectTab(target.filePath);
                return { selected: target.filePath, activated: true };
            }
            throw new Error(
                "switching tabs needs api.renderer.activateProjectTab " +
                    "(Batch-2 API, eez-open/studio#1042)"
            );
        }

        default:
            throw new Error(
                `unknown tool in prototype: ${tool} ` +
                    `(batch 2 — assets/styles/multi-project/debug — tracks the next upstream API round)`
            );
    }
}

// ----------------------------------------------------------------------------
// Extension definition
// ----------------------------------------------------------------------------

const extension = {
    preInstalled: false,
    extensionType: "measurement-functions",

    name: "eez-studio-mcp",
    displayName: "EEZ Studio MCP",
    version: "0.1.0",
    author: "IWILLTBEST",
    description:
        "Local MCP bridge for AI agents (prototype). See https://github.com/IWILLTBEST/eez-studio-mcp",

    init(api) {
        try {
            // Current upstream shape: only the field for the current process
            // is populated (api.main / api.renderer). Older runtimes passed
            // fromProcess instead — keep that branch until the fork is
            // rebased onto the merged API.
            if ((api && api.renderer) || !api) {
                // renderer — also the fallback when api is not passed yet
                initRenderer(api);
            } else if (api && api.main) {
                startBridgeMain();
            } else if (api && api.fromProcess === "main") {
                startBridgeMain();
            } else {
                initRenderer(api);
            }
        } catch (err) {
            console.error("[eez-mcp] init failed:", err);
        }
    },

    destroy() {
        if (httpServer) {
            httpServer.close();
            httpServer = undefined;
        }
    }
};

module.exports = { default: extension };
