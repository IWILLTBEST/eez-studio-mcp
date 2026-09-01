/**
 * UIXML for EEZ Studio / LVGL — compile, live preview, check.
 *
 * Everything rides the EXISTING infrastructure: ir2eez.py for compilation
 * and the EEZ Studio bridge (open/reload/navigate/screenshot/check) for
 * preview — the same endpoints the MCP server exposes.
 */
const vscode = require("vscode");
const { execFile, spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");

let output;
let status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);

function out() {
    if (!output) output = vscode.window.createOutputChannel("UIXML");
    return output;
}

function repoRoot() {
    const cfg = vscode.workspace.getConfiguration("uixml").get("repoRoot");
    if (cfg) return cfg;
    // default: extension lives inside the repo (vscode-extension/)
    return path.join(__dirname, "..");
}

function bridgeUrl() {
    let url = vscode.workspace.getConfiguration("uixml").get("bridgeUrl", "http://127.0.0.1:17620");
    return url.replace(/\/+$/, "");
}

function bridgeCall(tool, args, timeoutMs = 60000) {
    const body = JSON.stringify({ tool, args: args || {} });
    return new Promise((resolve, reject) => {
        const req = http.request(
            bridgeUrl() + "/tool",
            {
                method: "POST",
                headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
                timeout: timeoutMs,
            },
            (res) => {
                let data = "";
                res.on("data", (c) => (data += c));
                res.on("end", () => {
                    try {
                        const parsed = JSON.parse(data);
                        if (parsed.ok) resolve(parsed.result);
                        else reject(new Error(JSON.stringify(parsed).slice(0, 200)));
                    } catch (e) {
                        reject(new Error(`bridge: ${res.statusCode} ${data.slice(0, 200)}`));
                    }
                });
            }
        );
        req.on("timeout", () => { req.destroy(); reject(new Error("bridge timeout")); });
        req.on("error", reject);
        req.write(body);
        req.end();
    });
}

function projectPathFor(uixmlFile) {
    return uixmlFile.replace(/\.uixml$/i, ".eez-project");
}

function runCompiler(uixmlFile) {
    const py = vscode.workspace.getConfiguration("uixml").get("pythonPath", "python");
    const project = projectPathFor(uixmlFile);
    return new Promise((resolve, reject) => {
        execFile(
            py,
            [path.join(repoRoot(), "ir2eez.py"), uixmlFile, "-o", project],
            { timeout: 120000, cwd: path.dirname(uixmlFile) },
            (err, stdout, stderr) => {
                if (err) {
                    reject(new Error(`${err.message}\n${(stderr || stdout).slice(-2000)}`));
                } else {
                    resolve({ stdout: String(stdout), project });
                }
            }
        );
    });
}

// ---------------- Preview webview ----------------

class PreviewProvider {
    constructor() {
        this.panel = null;
        this.currentFile = null;
        this.screen = null; // null = first
        this.pending = false;
    }

    show(uixmlFile) {
        if (!this.panel) {
            this.panel = vscode.window.createWebviewPanel(
                "uixmlPreview", "UIXML Preview",
                vscode.ViewColumn.Beside, { enableScripts: true }
            );
            this.panel.onDidDispose(() => { this.panel = null; this.currentFile = null; });
            this.panel.webview.onDidReceiveMessage((m) => {
                if (m.command === "screen") { this.screen = m.screen; this.refresh(); }
            });
        }
        this.panel.reveal();
        this.currentFile = uixmlFile;
        this.refresh();
    }

    html(imgDataUrl, screens, active, note) {
        const options = (screens || [])
            .map((s) => `<option value="${s}" ${s === active ? "selected" : ""}>${s}</option>`)
            .join("");
        return `<!DOCTYPE html><html><head><style>
            body { background: #1e1e1e; color: #ccc; font-family: sans-serif; margin: 12px; }
            img { max-width: 100%; image-rendering: pixelated; border: 1px solid #444; margin-top: 8px; }
            select, span { background:#2d2d2d; color:#ddd; border:1px solid #555; padding:2px 8px; }
            .note { color:#888; margin-left:8px; }
        </style></head><body>
            <select>${options}</select>
            <span id="note" class="note">${note || ""}</span>
            <img id="shot" src="${imgDataUrl || ""}"/>
            <script>
                const vscode = acquireVsCodeApi();
                document.addEventListener('change', (e) => {
                    vscode.postMessage({ command: 'screen', screen: e.target.value });
                });
            </script>
        </body></html>`;
    }

    async refresh() {
        if (!this.panel || !this.currentFile || this.pending) return;
        this.pending = true;
        status.text = "uixml: rendering…";
        try {
            const project = projectPathFor(this.currentFile);
            await runCompiler(this.currentFile);
            await bridgeCall("open_project", { path: project.replace(/\\/g, "/") });
            await bridgeCall("reload", {});
            await new Promise((r) => setTimeout(r, 3500));
            let screens = [];
            try {
                const list = await bridgeCall("list_objects", {});
                screens = (list.screens || []).map((s) => s.name);
            } catch {}
            const target = this.screen && screens.includes(this.screen) ? this.screen : screens[0];
            await bridgeCall("navigate", { screen: target, object: `screen_${target}` });
            await new Promise((r) => setTimeout(r, 1500));
            // paint-stability: two identical shots
            let prev = await bridgeCall("screenshot", {});
            for (let i = 0; i < 5; i++) {
                await new Promise((r) => setTimeout(r, 800));
                const cur = await bridgeCall("screenshot", {});
                if (cur.dataUrl === prev.dataUrl) { prev = cur; break; }
                prev = cur;
            }
            this.panel.webview.html = this.html(prev.dataUrl, screens, target);
            status.text = `uixml: ${path.basename(this.currentFile)} ✓`;
        } catch (e) {
            out().appendLine(`preview: ${e.message || e}`);
            if (this.panel) this.panel.webview.html = this.html(null, [], null, String(e.message || e).slice(0, 300));
            status.text = "uixml: preview failed";
        } finally {
            this.pending = false;
        }
    }
}

function activate(context) {
    const preview = new PreviewProvider();
    status.command = "uixml.preview";
    status.text = "uixml";
    status.show();

    const activeUixml = () => {
        const ed = vscode.window.activeTextEditor;
        if (ed && ed.document.fileName.toLowerCase().endsWith(".uixml")) return ed.document.fileName;
        return null;
    };

    const compileCmd = vscode.commands.registerCommand("uixml.compile", async () => {
        const f = activeUixml();
        if (!f) return vscode.window.showWarningMessage("Open a .uixml file first");
        try {
            status.text = "uixml: compiling…";
            const r = await runCompiler(f);
            out().appendLine(r.stdout);
            status.text = `uixml: compiled ${path.basename(f)} ✓`;
            vscode.window.showInformationMessage(`Compiled → ${path.basename(r.project)}`);
        } catch (e) {
            out().appendLine(String(e.message || e));
            out().show();
            status.text = "uixml: compile failed";
            vscode.window.showErrorMessage("Compile failed — see UIXML output");
        }
    });

    const checkCmd = vscode.commands.registerCommand("uixml.check", async () => {
        try {
            const c = await bridgeCall("check", {});
            const msg = `${c.numErrors} errors, ${c.numWarnings} warnings`;
            out().appendLine(`check: ${msg}`);
            (c.numErrors ? vscode.window.showErrorMessage : vscode.window.showInformationMessage)(`EEZ check: ${msg}`);
        } catch (e) {
            vscode.window.showErrorMessage(`Bridge unreachable — is EEZ Studio running? (${e.message || e})`);
        }
    });

    const previewCmd = vscode.commands.registerCommand("uixml.preview", async () => {
        const f = activeUixml();
        if (!f) return vscode.window.showWarningMessage("Open a .uixml file first");
        preview.show(f);
    });

    // save-to-refresh when the preview is open for the saved file
    const onSave = vscode.workspace.onDidSaveTextDocument((doc) => {
        if (doc.fileName.toLowerCase().endsWith(".uixml") && preview.currentFile === doc.fileName) {
            preview.refresh();
        }
    });

    context.subscriptions.push(compileCmd, checkCmd, previewCmd, onSave, status);
}

function deactivate() {}

module.exports = { activate, deactivate };
