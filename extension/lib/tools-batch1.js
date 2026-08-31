/*
 * Batch-1 tool implementations for the eez-studio-mcp extension.
 *
 * Every tool here runs purely on what the merged upstream extension API
 * (eez-open/studio PR #1044) already exposes — api.renderer.getActiveProjectStore()
 * for the ProjectStore object graph, api.renderer.requireModule("mobx") for
 * reactivity — plus renderer-process capabilities that need no API at all
 * (DOM access for screenshots, node fs for file writes).
 *
 * Ported from the fork's packages/ai-agent/tools.ts. Store-module helpers are
 * re-implemented locally on the public object shape:
 *   - getObjectPath()  -> _eez_parent / _eez_key walk (same fields EEZ uses)
 *   - path resolution  -> manual segment walk + objID search (visitObjects-free)
 *   - Section enum     -> CHECKS=0, OUTPUT=1 (store/output-sections.tsx)
 */

"use strict";

const fs = require("fs");
const path = require("path");

let mobx; // { toJS, runInAction } via api.renderer.requireModule("mobx")

function setMobx(m) {
    mobx = m;
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Object addressing — path (/userPages/0/components/0/children/3) or objID
// ---------------------------------------------------------------------------

function isArrayObj(v) {
    return Array.isArray(v);
}

function objectPathOf(obj) {
    const result = [];
    let child = obj;
    let parent = child && child._eez_parent;
    while (parent) {
        if (isArrayObj(parent)) {
            result.unshift(parent.indexOf(child));
        } else if (child._eez_key !== undefined) {
            result.unshift(child._eez_key);
        } else {
            result.unshift("?");
        }
        child = parent;
        parent = child && child._eez_parent;
    }
    return "/" + result.join("/");
}

/** Manual path walk replicating getObjectFromStringPath for plain
 *  property/index segments; the caller validates the result has an objID. */
function fromStringPath(root, p) {
    let cur = root;
    for (const seg of p.split("/").filter(Boolean)) {
        if (cur === undefined || cur === null) {
            return undefined;
        }
        if (/^\d+$/.test(seg) && Array.isArray(cur)) {
            cur = cur[Number(seg)];
        } else {
            cur = cur[seg];
        }
    }
    return cur;
}

/** Full-tree objID search (stand-in for visitObjects from core/search). */
function findByObjID(root, objID) {
    const stack = [root];
    let guard = 0;
    while (stack.length && guard++ < 2000000) {
        const o = stack.pop();
        if (!o || typeof o !== "object") {
            continue;
        }
        if (o.objID === objID) {
            return o;
        }
        if (o instanceof Date || o instanceof Map || o instanceof Set) {
            continue;
        }
        for (const k of Object.keys(o)) {
            if (k.startsWith("_")) {
                continue;
            }
            const v = o[k];
            if (v && typeof v === "object") {
                if (Array.isArray(v)) {
                    for (const c of v) {
                        if (c && typeof c === "object" && c.objID !== undefined) {
                            stack.push(c);
                        }
                    }
                } else if (v.objID !== undefined) {
                    stack.push(v);
                }
            }
        }
    }
    return undefined;
}

const GUID_RE = /^[0-9a-f][0-9a-f-]{19,}$/i;

function resolveObject(store, ref) {
    let p = String(ref ?? "").trim();
    const i = p.indexOf("]:");
    if (p.startsWith("[") && i !== -1) {
        p = p.slice(i + 2);
    }
    let objID;
    if (p.startsWith("objID:")) {
        objID = p.slice(6).trim();
    } else if (!p.includes("/") && GUID_RE.test(p)) {
        objID = p;
    }
    if (objID) {
        const obj = findByObjID(store.project, objID);
        if (obj) {
            return obj;
        }
        throw new Error(`no object with objID=${objID}`);
    }
    if (!p.startsWith("/")) {
        p = "/" + p;
    }
    const obj = fromStringPath(store.project, p);
    // Everything EezObject has objID: resolving to something without one
    // (array / out-of-range debris) means the path drifted after edits.
    if (!obj || obj.objID === undefined) {
        throw new Error(
            `path not found: ${ref} (array indexes drift after edits — list_objects again for fresh paths, or address by objID)`
        );
    }
    return obj;
}

// ---------------------------------------------------------------------------
// Serialization — same contract as the official objectToJson (toJS keeps only
// observable persistent fields; computed/internals are skipped automatically)
// ---------------------------------------------------------------------------

function isEezObjectLike(v) {
    return (
        v !== null &&
        typeof v === "object" &&
        !(v instanceof Date) &&
        !(v instanceof Map) &&
        !(v instanceof Set) &&
        (v.objID !== undefined || v.type !== undefined)
    );
}

function pruneTree(plain, live, depth) {
    if (plain === null || typeof plain !== "object") {
        return plain;
    }
    if (Array.isArray(plain)) {
        const liveArr = Array.isArray(live) ? live : [];
        const isEezArr =
            liveArr.length > 0 && isEezObjectLike(liveArr[0]);
        if (!isEezArr) {
            return plain;
        }
        if (depth > 0) {
            return plain.map((p, i) => pruneTree(p, liveArr[i], depth - 1));
        }
        return liveArr.map(c => objectPathOf(c));
    }
    const out = {};
    for (const k of Object.keys(plain)) {
        if (k.startsWith("_")) {
            continue;
        }
        const v = plain[k];
        const lv = live ? live[k] : undefined;
        if (v !== null && typeof v === "object") {
            if (Array.isArray(v)) {
                out[k] = pruneTree(v, lv, depth);
            } else if (isEezObjectLike(lv)) {
                out[k] = depth > 0 ? pruneTree(v, lv, depth - 1) : objectPathOf(lv);
            } else {
                // plain nested dicts (style definition maps...): follow depth
                out[k] = depth > 0 ? pruneTree(v, lv, depth) : v;
            }
        } else {
            out[k] = v;
        }
    }
    return out;
}

function serializeTree(obj, depth) {
    if (!mobx) {
        return { __error__: "mobx unavailable via requireModule" };
    }
    let plain;
    try {
        plain = mobx.toJS(obj);
    } catch (err) {
        return { __error__: "serialization failed" };
    }
    return pruneTree(plain, obj, depth);
}

// ---------------------------------------------------------------------------
// Widget tree listing (compact nodes)
// ---------------------------------------------------------------------------

function widgetNode(w) {
    const node = {
        path: objectPathOf(w),
        objID: w.objID,
        type: w.type
    };
    if (w.identifier) {
        node.identifier = w.identifier;
    }
    node.left = w.left;
    node.top = w.top;
    node.width = w.width;
    node.height = w.height;
    for (const unit of ["leftUnit", "topUnit", "widthUnit", "heightUnit"]) {
        if (w[unit] && w[unit] !== "px") {
            node[unit] = w[unit];
        }
    }
    for (const k of [
        "useStyle",
        "hiddenFlag",
        "clickableFlag",
        "text",
        "textType",
        "value",
        "min",
        "max",
        "options",
        "placeholder",
        "src",
        "color"
    ]) {
        if (w[k] !== undefined && w[k] !== null && w[k] !== false) {
            node[k] = w[k];
        }
    }
    const ch = w.children ?? [];
    if (ch.length > 0) {
        node.children = ch.map(widgetNode);
    }
    return node;
}

function allPages(project) {
    return [...(project.userPages ?? []), ...(project.userWidgets ?? [])];
}

// ---------------------------------------------------------------------------
// Tools. ctx = { projectStore, fromExtension: true }
// ---------------------------------------------------------------------------

function needStore(ctx) {
    if (!ctx || !ctx.projectStore || !ctx.projectStore.project) {
        throw new Error("no active project editor open");
    }
    return ctx.projectStore;
}

function listObjects(ctx, screen, ref) {
    const store = needStore(ctx);
    const project = store.project;
    if (screen) {
        const page = allPages(project).find(p => p.name === screen);
        if (!page) {
            throw new Error(
                `no screen ${screen}, have: ${allPages(project)
                    .map(p => p.name)
                    .join(", ")}`
            );
        }
        const root = page.lvglScreenWidget;
        return {
            screen: page.name,
            path: objectPathOf(page),
            width: page.width,
            height: page.height,
            children: (root?.children ?? page.components ?? []).map(widgetNode)
        };
    }
    if (ref) {
        const obj = resolveObject(store, ref);
        if (obj.children) {
            return {
                path: objectPathOf(obj),
                type: obj.type,
                children: (obj.children ?? []).map(widgetNode)
            };
        }
        if (obj.components) {
            return {
                path: objectPathOf(obj),
                name: obj.name,
                children: (
                    obj.lvglScreenWidget?.children ?? obj.components
                ).map(widgetNode)
            };
        }
        return { path: objectPathOf(obj), type: obj.type, leaf: true };
    }
    return {
        screens: allPages(project).map(p => ({
            name: p.name,
            path: objectPathOf(p),
            isUserWidget: !!p.isUsedAsUserWidget,
            width: p.width,
            height: p.height,
            widgetCount: (
                p.lvglScreenWidget?.children ?? p.components ?? []
            ).length
        }))
    };
}

function getObject(ctx, ref, depth) {
    const store = needStore(ctx);
    return serializeTree(resolveObject(store, ref), depth ?? 2);
}

/** Surgical property edit via store.updateObject (undoable, auto-saved).
 *  Supports one level of dot paths ("data.text"). */
async function updateObject(ctx, ref, properties) {
    const store = needStore(ctx);
    const obj = resolveObject(store, ref);
    store.undoManager.setCombineCommands(true);
    try {
        const direct = {};
        const nested = new Map();
        for (const [k, v] of Object.entries(properties ?? {})) {
            const dot = k.indexOf(".");
            if (dot > 0) {
                const child = obj[k.slice(0, dot)];
                if (
                    child &&
                    typeof child === "object" &&
                    !Array.isArray(child)
                ) {
                    let sub = nested.get(child);
                    if (!sub) {
                        sub = {};
                        nested.set(child, sub);
                    }
                    sub[k.slice(dot + 1)] = v;
                    continue;
                }
            }
            direct[k] = v;
        }
        if (Object.keys(direct).length > 0) {
            store.updateObject(obj, direct);
        }
        for (const [child, sub] of nested) {
            store.updateObject(child, sub);
        }
    } finally {
        store.undoManager.setCombineCommands(false);
    }
    await store.save();
    return `updated ${ref}: ${JSON.stringify(properties)}`;
}

async function deleteObject(ctx, ref) {
    const store = needStore(ctx);
    const obj = resolveObject(store, ref);
    const p = objectPathOf(obj);
    store.deleteObject(obj);
    await store.save();
    return `deleted ${p}`;
}

async function undoProject(ctx) {
    const store = needStore(ctx);
    if (!store.undoManager.canUndo) {
        return { undone: false, reason: "nothing to undo" };
    }
    store.undoManager.undo();
    await store.save();
    return {
        undone: true,
        canUndo: store.undoManager.canUndo,
        canRedo: store.undoManager.canRedo
    };
}

async function redoProject(ctx) {
    const store = needStore(ctx);
    if (!store.undoManager.canRedo) {
        return { redone: false, reason: "nothing to redo" };
    }
    store.undoManager.redo();
    await store.save();
    return {
        redone: true,
        canUndo: store.undoManager.canUndo,
        canRedo: store.undoManager.canRedo
    };
}

function navigateToScreen(ctx, screen) {
    const store = needStore(ctx);
    const page = store.project.userPages.find(p => p.name === screen);
    if (!page) {
        return `no screen named ${screen}, have: ${store.project.userPages
            .map(p => p.name)
            .join(", ")}`;
    }
    store.navigationStore.showObjects([page], true, true, true);
    return `navigated to screen ${screen}`;
}

function gotoObject(ctx, ref) {
    const store = needStore(ctx);
    const obj = resolveObject(store, ref);
    // selectInEditor=true, showInNavigation/selectObject=false: selecting in
    // the navigation panel would shadow the editor selection (get_selection).
    store.navigationStore.showObjects([obj], true, false, false);
    return {
        selected: objectPathOf(obj),
        objID: obj.objID,
        type: obj.type ?? undefined,
        name: obj.name
    };
}

function getSelection(ctx) {
    const store = needStore(ctx);
    const fmt = o => ({
        path: objectPathOf(o),
        objID: o.objID,
        type: o.type,
        name: o.name,
        identifier: o.identifier
    });
    const tabState = store.editorsStore?.activeEditor?.state;
    const editorObjs = Array.isArray(tabState?.selectedObjects)
        ? tabState.selectedObjects
        : [];
    const panel = store.navigationStore?.selectedPanel;
    let panelObjs = [];
    if (Array.isArray(panel?.selectedObjects)) {
        panelObjs = panel.selectedObjects;
    } else if (panel?.selectedObject) {
        panelObjs = [panel.selectedObject];
    }
    return {
        editorSelection: editorObjs.map(fmt),
        panelSelection: panelObjs.map(fmt)
    };
}

// ---------------------------------------------------------------------------
// Screenshots — pure DOM (the LVGL page editor renders into a wasm-backed
// canvas at exact page resolution)
// ---------------------------------------------------------------------------

function screenshotOnce() {
    const canvases = document.querySelectorAll(
        ".EezStudio_FlowEditorCanvasContainer .eez-canvas canvas"
    );
    let best;
    for (const c of canvases) {
        if (c.width === 0 || c.height === 0) {
            continue;
        }
        if (c.getClientRects().length === 0) {
            continue; // stale canvas of a hidden editor tab
        }
        if (!best || c.width * c.height > best.width * best.height) {
            best = c;
        }
    }
    if (best && best.width > 0 && best.height > 0) {
        return best.toDataURL("image/png");
    }
    return undefined;
}

async function screenshot(ctx, out) {
    needStore(ctx);
    let dataUrl;
    for (let i = 0; i < 8 && !dataUrl; i++) {
        dataUrl = screenshotOnce();
        if (!dataUrl) {
            await sleep(500);
        }
    }
    if (!dataUrl) {
        throw new Error(
            "no visible page editor canvas — navigate to a screen first"
        );
    }
    let file;
    if (out) {
        file = out;
    } else {
        const dir = path.join(
            path.dirname(ctx.projectStore.filePath || "."),
            "_shots"
        );
        fs.mkdirSync(dir, { recursive: true });
        file = path.join(
            dir,
            `${new Date().toISOString().replace(/[:.]/g, "-")}_ext.png`
        );
    }
    fs.writeFileSync(file, Buffer.from(dataUrl.split(",")[1], "base64"));
    // Contract shared with the fork-internal bridge: {dataUrl, file} — the
    // MCP servers split the base64 payload off the dataUrl for image blocks.
    return { dataUrl, file, bytes: fs.statSync(file).size };
}

// ---------------------------------------------------------------------------
// Output / Checks — Section.CHECKS = 0, Section.OUTPUT = 1
// ---------------------------------------------------------------------------

const MESSAGE_TYPE_NAMES = {
    0: "info",
    1: "error",
    2: "warning",
    3: "search",
    4: "group"
};

function flattenMessages(messages, out) {
    for (const m of messages) {
        out.push({
            type: MESSAGE_TYPE_NAMES[m.type] ?? String(m.type),
            text: String(m.text ?? ""),
            object: m.object ? objectPathOf(m.object) : undefined
        });
        if (m.type === 4 && Array.isArray(m.messages)) {
            flattenMessages(m.messages, out);
        }
    }
}

function readOutputSection(ctx, which) {
    const store = needStore(ctx);
    const section = store.outputSectionsStore.getSection(
        which === "output" ? 1 : 0
    );
    const messages = [];
    flattenMessages(section.messages.messages, messages);
    return {
        section: which,
        loading: section.loading,
        numErrors: section.numErrors,
        numWarnings: section.numWarnings,
        messages
    };
}

async function runCheck(ctx) {
    const store = needStore(ctx);
    const out = store.outputSectionsStore.getSection(1);
    store.check(); // async internally — poll the loading flag
    const deadline = Date.now() + 90000;
    let sawLoading = out.loading;
    while (Date.now() < deadline) {
        await sleep(150);
        if (out.loading) {
            sawLoading = true;
        }
        if (sawLoading && !out.loading) {
            break;
        }
        if (!sawLoading && out.messages.messages.length > 0) {
            await sleep(400);
            break;
        }
    }
    return readOutputSection(ctx, "output");
}

module.exports = {
    setMobx,
    sleep,
    objectPathOf,
    listObjects,
    getObject,
    updateObject,
    deleteObject,
    undoProject,
    redoProject,
    navigateToScreen,
    gotoObject,
    getSelection,
    screenshot,
    readOutputSection,
    runCheck
};
