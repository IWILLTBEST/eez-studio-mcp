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
                from: "eez-studio-mcp extension (prototype)",
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
            // Batch-1 architecture probe: everything reachable from the
            // ProjectStore object graph needs no further upstream API.
            const api = rendererApi();
            const store =
                api && typeof api.getActiveProjectStore === "function"
                    ? api.getActiveProjectStore()
                    : undefined;
            if (!store || !store.project) {
                throw new Error("no active project editor open");
            }
            const project = store.project;
            return {
                project: store.filePath,
                screens: (project.userPages || []).map(p => ({
                    name: p.name,
                    widgets: (p.components || []).length
                })),
                userWidgets: (project.userWidgets || []).map(w => w.name)
            };
        }
        default:
            throw new Error(
                `unknown tool in prototype: ${tool} ` +
                    `(full tool library tracks eez-open/studio PR #1044)`
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
