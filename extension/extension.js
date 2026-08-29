/*
 * eez-studio-mcp extension (prototype)
 *
 * Dual-process extension using the IExtensionApi.fromProcess parameter
 * (eez-open/studio PR #1043):
 *   - main:     starts the opt-in localhost HTTP bridge (127.0.0.1:17620)
 *   - renderer: receives tool requests over Electron IPC and executes them
 *
 * Prototype scope: renderer tools are internals-free (ping/echo) — the real
 * tool library needs `api.requireModule` (explicit Studio module whitelist)
 * to be added to IExtensionApi upstream first.
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

function initRenderer() {
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
// Tool execution (prototype: internals-free tools only)
// ----------------------------------------------------------------------------

async function executeTool(tool, args) {
    switch (tool) {
        case "ping":
            return {
                pong: true,
                from: "eez-studio-mcp extension (prototype)",
                process: typeof window !== "undefined" ? "renderer" : "unknown"
            };
        case "echo":
            return args;
        default:
            throw new Error(
                `unknown tool in prototype: ${tool} ` +
                    `(full tool library requires api.requireModule upstream)`
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
            if (api && api.fromProcess === "main") {
                startBridgeMain();
            } else {
                // renderer — also the fallback when api is not passed yet
                initRenderer();
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
