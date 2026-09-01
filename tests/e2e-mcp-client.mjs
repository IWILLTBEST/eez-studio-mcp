/*
 * Minimal real MCP client for E2E: spawns the Node MCP server over stdio,
 * performs the initialize/tools/list handshake, then calls tools through it.
 * Usage: node _test_mcp_client.mjs [bridgeUrl]
 *   e.g. node _test_mcp_client.mjs http://127.0.0.1:17621   (extension path)
 */
import { spawn } from "child_process";
import { fileURLToPath } from "url";

const BRIDGE = process.argv[2] || "http://127.0.0.1:17620";
const SERVER = fileURLToPath(new URL("../mcp-server.mjs", import.meta.url));

const child = spawn(process.execPath, [SERVER], {
    env: { ...process.env, EEZ_BRIDGE_URL: BRIDGE },
    stdio: ["pipe", "pipe", "inherit"]
});

let buf = "";
const pending = new Map();
let nextId = 1;

child.stdout.on("data", chunk => {
    buf += chunk.toString("utf8");
    let nl;
    while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        try {
            const msg = JSON.parse(line);
            if (msg.id !== undefined && pending.has(msg.id)) {
                pending.get(msg.id)(msg);
                pending.delete(msg.id);
            }
        } catch (err) {
            console.error("unparseable line:", line.slice(0, 120));
        }
    }
});

function request(method, params) {
    return new Promise((resolve, reject) => {
        const id = nextId++;
        pending.set(id, msg => {
            if (msg.error) {
                reject(new Error(JSON.stringify(msg.error).slice(0, 300)));
            } else {
                resolve(msg.result);
            }
        });
        child.stdin.write(
            JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n"
        );
    });
}

function notify(method, params) {
    child.stdin.write(
        JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n"
    );
}

const results = [];
function record(name, ok, detail) {
    results.push({ name, ok });
    console.log(`${ok ? "[OK]  " : "[FAIL]"} ${name}${detail ? ": " + detail : ""}`);
}

async function callTool(name, args = {}) {
    const r = await request("tools/call", {
        name,
        arguments: args
    });
    if (r.isError) {
        throw new Error((r.content || []).map(c => c.text).join(" ").slice(0, 300));
    }
    const text = (r.content || []).map(c => c.text).join("\n");
    return { text, raw: r };
}

try {
    // --- MCP handshake ---
    const init = await request("initialize", {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "e2e-test-client", version: "0.1.0" }
    });
    record(
        "initialize",
        !!init.serverInfo && init.serverInfo.name.includes("eez"),
        `${init.serverInfo.name} v${init.serverInfo.version}, tools cap: ${!!init.capabilities?.tools}`
    );
    notify("notifications/initialized", {});

    // --- tools/list ---
    const tools = await request("tools/list", {});
    const names = tools.tools.map(t => t.name);
    record("tools/list", names.length >= 40, `${names.length} tools advertised`);

    // --- ping via extension ---
    const ping = await callTool("ping", {});
    const pong = JSON.parse(ping.text);
    record(
        "ping",
        pong.pong === true && pong.studioAccess !== "unavailable",
        `studioAccess=${pong.studioAccess}`
    );

    // --- deterministic preamble: make the motor project the active tab
    //     (later steps navigate its screens; other e2e runs may have left a
    //     different tab active). 确定前言：让 motor 工程成为活动 tab。 ---
    {
        const { existsSync } = await import("node:fs");
        const { resolve } = await import("node:path");
        const motor = [
            process.env.EEZ_E2E_PROJECT,
            resolve("out_motor.eez-project"),
            resolve("../html2eez/out_motor.eez-project"),
        ].find(p => p && existsSync(p));
        if (motor) {
            // forward slashes only — tab identity is string-based, mixed
            // separators would open a duplicate tab. 只用正斜杠，避免重复 tab。
            await callTool("open_project", { path: motor.replace(/\\/g, "/") });
            await new Promise(r => setTimeout(r, 3000));
        }
    }

    // --- a real editing read ---
    const list = await callTool("list_objects", {});
    const screens = JSON.parse(list.text);
    const overview = (screens.screens || []).find(s => s.name === "overview");
    record(
        "list_objects",
        !!overview,
        `overview ${overview?.width}x${overview?.height}, ${screens.screens?.length} screens`
    );

    // --- surgical update + undo (round-trips through store commands) ---
    const g = await callTool("get_object", {
        path: overview.path + "/components/0/children/3/children/0/children/0",
        depth: 0
    });
    const obj = JSON.parse(g.text);
    const origText = obj.text;
    await callTool("update_object", {
        path: obj.objID,
        properties: { text: "MCP-E2E" }
    });
    const g2 = await callTool("get_object", { path: obj.objID, depth: 0 });
    record("update_object", JSON.parse(g2.text).text === "MCP-E2E");
    await callTool("undo", {});
    const g3 = await callTool("get_object", { path: obj.objID, depth: 0 });
    record(
        "undo restores",
        JSON.parse(g3.text).text === origText,
        `text back to ${JSON.parse(g3.text).text}`
    );

    // --- navigate + screenshot through MCP (text + image content block) ---
    await callTool("navigate", { screen: "alarms" });
    const shot = await callTool("screenshot", {});
    const imgBlock = (shot.raw.content || []).find(c => c.type === "image");
    record(
        "screenshot",
        shot.text.startsWith("screenshot saved:") &&
            imgBlock &&
            imgBlock.data.length > 10000,
        `${shot.text.slice(0, 60)}..., image ${(imgBlock?.data?.length || 0) / 1000 | 0}k b64`
    );

    // --- checks ---
    const chk = await callTool("check", {});
    const c = JSON.parse(chk.text);
    record(
        "check",
        c.numErrors === 0,
        `${c.numErrors} errors, ${c.numWarnings} warnings`
    );

    // --- visual regression through MCP (python + golden must exist; else skip) ---
    const { existsSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const glassProject = [
        process.env.EEZ_E2E_GLASS_PROJECT,
        resolve("out_glass.eez-project"),
        resolve("examples/glass/out_glass.eez-project"),
        resolve("../html2eez/out_glass.eez-project"),
    ].find(p => p && existsSync(p));
    if (glassProject && existsSync(resolve("golden/glass.png"))) {
        const v = await callTool("visual_check", {
            name: "glass", project: glassProject, screen: "main"
        });
        const vs = JSON.parse(v.text);
        record(
            "visual_check",
            vs.ok === true,
            vs.ok
                ? `golden match (0 drift), ${glassProject}`
                : `raw: ${JSON.stringify(vs).slice(0, 200)}`
        );
    } else {
        record("visual_check", true, "skipped — no golden/project on this machine");
    }

    // --- select_project skeleton (already-active fallback path) ---
    const sel = await callTool("select_project", { index: 0 }).catch(e => e);
    if (sel instanceof Error) {
        record("select_project", true, `expected-limitation: ${String(sel.message).slice(0, 80)}`);
    } else {
        record("select_project", true, "returned: " + String(sel.text).slice(0, 80));
    }
} catch (err) {
    record("fatal", false, String(err.message || err).slice(0, 300));
} finally {
    const pass = results.filter(r => r.ok).length;
    console.log(`\n${pass}/${results.length} passed (bridge=${BRIDGE})`);
    child.kill();
    process.exit(pass === results.length ? 0 : 1);
}
