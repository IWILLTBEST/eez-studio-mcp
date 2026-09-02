/*
 * Batch-2 tool implementations for the eez-studio-mcp extension.
 *
 * Depends on the Batch-2 extension API (eez-open/studio PR #1047, option B
 * from #1042): the capability toolkits getEditorObjectToolkit /
 * getLvglToolkit / getAssetToolkit plus activateProjectTab / openProject.
 * Every tool feature-detects its toolkit and throws a clear error when the
 * runtime predates the API.
 *
 * Ported from the fork's packages/ai-agent/tools.ts; store-graph parts run
 * on getActiveProjectStore() exactly like Batch 1.
 */

"use strict";

const fs = require("fs");
const path = require("path");

const batch1 = require("./tools-batch1.js");

let mobx;
function setMobx(m) {
    mobx = m;
    batch1.setMobx(m);
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// Toolkit accessors — each returns undefined when the runtime predates
// PR #1047; tools turn that into a clear error via needToolkit().
let rendererApiGetter;
function setRendererApiGetter(fn) {
    rendererApiGetter = fn;
}

function needToolkit(name) {
    const api = rendererApiGetter && rendererApiGetter();
    const t =
        api &&
        typeof api[name] === "function" ? api[name]() : undefined;
    if (!t) {
        throw new Error(
            `this tool needs api.renderer.${name}() (eez-open/studio PR #1047)`
        );
    }
    return t;
}

function store(ctx) {
    if (!ctx || !ctx.projectStore || !ctx.projectStore.project) {
        throw new Error("no active project editor open");
    }
    return ctx.projectStore;
}

async function persist(s) {
    await s.save();
}

function allPages(project) {
    return [...(project.userPages ?? []), ...(project.userWidgets ?? [])];
}

// ---------------------------------------------------------------------------
// Styles & theme colors (toolkit: LVGLStyle / Color)
// ---------------------------------------------------------------------------

function findLvglStyles(project) {
    return project.lvglStyles?.styles ?? [];
}

function findLvglStyle(project, name) {
    const style = findLvglStyles(project).find(s => s.name === name);
    if (!style) {
        throw new Error(
            `no LVGL style named ${name}, have: ${
                findLvglStyles(project)
                    .map(s => s.name)
                    .join(", ") || "(none)"
            }`
        );
    }
    return style;
}

function listStyles(ctx) {
    const project = store(ctx).project;
    return {
        lvglStyles: findLvglStyles(project).map(s => ({
            name: s.name,
            forWidgetType: s.forWidgetType || undefined,
            definition: JSON.parse(
                JSON.stringify(
                    mobx.toJS(s.definition?.definition ?? {})
                )
            ),
            childStyles: (s.childStyles ?? []).map(c => c.name)
        })),
        styles: (project.styles ?? []).map(s => s.name),
        colors: (project.colors ?? []).map(c => c.name),
        themes: (project.themes ?? []).map(t => ({
            name: t.name,
            colors: mobx.toJS(t.colors)
        }))
    };
}

async function updateStyle(ctx, styleName, part, state, properties) {
    const s = store(ctx);
    const partKey = String(part || "MAIN").toUpperCase();
    const stateKey = String(state || "DEFAULT").toUpperCase();
    const style = findLvglStyle(s.project, styleName);
    const defObj = style.definition;
    const def = JSON.parse(
        JSON.stringify(mobx.toJS(defObj?.definition ?? {}))
    );
    if (!def[partKey]) def[partKey] = {};
    if (!def[partKey][stateKey]) def[partKey][stateKey] = {};
    for (const [k, v] of Object.entries(properties ?? {})) {
        if (v === null || v === undefined) {
            delete def[partKey][stateKey][k];
        } else {
            def[partKey][stateKey][k] = v;
        }
    }
    s.updateObject(defObj, { definition: def });
    await persist(s);
    return `style ${styleName} ${partKey}/${stateKey} updated: ${JSON.stringify(
        def[partKey][stateKey]
    )}`;
}

async function createStyle(ctx, name, forWidgetType) {
    const s = store(ctx);
    const objects = needToolkit("getEditorObjectToolkit");
    const lvgl = needToolkit("getLvglToolkit");
    if (findLvglStyles(s.project).some(x => x.name === name)) {
        throw new Error(`style ${name} already exists`);
    }
    const style = objects.createObject(
        s,
        {
            name,
            forWidgetType: forWidgetType || "LVGLPanelWidget",
            definition: {}
        },
        lvgl.LVGLStyle
    );
    s.addObject(s.project.lvglStyles.styles, style);
    await persist(s);
    return `created style ${name} (forWidgetType=${
        forWidgetType || "LVGLPanelWidget"
    }) — set properties with update_style`;
}

async function deleteStyle(ctx, name) {
    const s = store(ctx);
    const style = findLvglStyle(s.project, name);
    s.deleteObject(style);
    await persist(s);
    return `deleted style ${name} (widgets referencing it fail in check)`;
}

async function setThemeColor(ctx, colorName, value, themeName) {
    const s = store(ctx);
    const project = s.project;
    const colorIndex = (project.colors ?? []).findIndex(
        c => c.name === colorName
    );
    if (colorIndex < 0) {
        throw new Error(
            `no color ${colorName}, have: ${(project.colors ?? [])
                .map(c => c.name)
                .join(", ") || "(none)"}`
        );
    }
    const themes = themeName
        ? (project.themes ?? []).filter(t => t.name === themeName)
        : project.themes ?? [];
    if (themes.length === 0) {
        throw new Error(
            `no theme ${themeName}, have: ${(project.themes ?? [])
                .map(t => t.name)
                .join(", ")}`
        );
    }
    s.undoManager.setCombineCommands(true);
    try {
        for (const theme of themes) {
            const colors = theme.colors.slice();
            colors[colorIndex] = value;
            s.updateObject(theme, { colors });
        }
    } finally {
        s.undoManager.setCombineCommands(false);
    }
    await persist(s);
    return `color ${colorName} = ${value} (themes: ${themes
        .map(t => t.name)
        .join(", ")})`;
}

async function addThemeColor(ctx, colorName, value) {
    const s = store(ctx);
    const objects = needToolkit("getEditorObjectToolkit");
    const lvgl = needToolkit("getLvglToolkit");
    const project = s.project;
    if ((project.colors ?? []).some(c => c.name === colorName)) {
        throw new Error(`color ${colorName} already exists`);
    }
    const color = objects.createObject(s, { name: colorName }, lvgl.Color);
    s.addObject(project.colors, color);
    const themes = project.themes ?? [];
    s.undoManager.setCombineCommands(true);
    try {
        for (const theme of themes) {
            const colors = theme.colors.slice();
            while (colors.length < project.colors.length) {
                colors.push("#000000");
            }
            colors[project.colors.length - 1] = value;
            s.updateObject(theme, { colors });
        }
    } finally {
        s.undoManager.setCombineCommands(false);
    }
    await persist(s);
    return `added color ${colorName} (${themes.length} themes initialized to ${value})`;
}

function setPreviewTheme(ctx, themeName) {
    const s = store(ctx);
    const theme = (s.project.themes ?? []).find(t => t.name === themeName);
    if (!theme) {
        throw new Error(
            `no theme ${themeName}, have: ${(s.project.themes ?? [])
                .map(t => t.name)
                .join(", ") || "(none)"}`
        );
    }
    const pageRuntime = s.runtime?.lgvlPageRuntime;
    if (pageRuntime && typeof pageRuntime.setColorTheme === "function") {
        pageRuntime.setColorTheme(themeName);
        return `runtime theme switched to ${themeName}`;
    }
    mobx.runInAction(() => {
        s.navigationStore.selectedThemeObject.set(theme);
    });
    return `editor preview theme switched to ${themeName} (screenshot to see it)`;
}

// ---------------------------------------------------------------------------
// Widget / screen creation (toolkit: class registry + createObject + Page)
// ---------------------------------------------------------------------------

function availableWidgetTypes(s) {
    try {
        const objects = needToolkit("getEditorObjectToolkit");
        const lvgl = needToolkit("getLvglToolkit");
        return objects
            .getClassesDerivedFrom(s, lvgl.LVGLWidget)
            .map(c => c.name)
            .filter(n => n !== "LVGLScreenWidget");
    } catch {
        return [];
    }
}

async function createWidget(ctx, type, parent, properties) {
    const s = store(ctx);
    const objects = needToolkit("getEditorObjectToolkit");
    const project = s.project;
    const cls = objects.getClassByName(s, type);
    if (!cls) {
        throw new Error(
            `unknown widget type ${type}. available: ${availableWidgetTypes(
                s
            ).join(", ")}`
        );
    }
    let parentArray;
    let parentDesc;
    const page = allPages(project).find(p => p.name === parent);
    if (page) {
        parentArray = page.components;
        parentDesc = `page ${page.name}`;
    } else {
        const pObj = batch1.resolveObject(s, parent);
        if (Array.isArray(pObj.components) && pObj.name !== undefined) {
            parentArray = pObj.components;
            parentDesc = `page ${pObj.name}`;
        } else if (Array.isArray(pObj.children)) {
            parentArray = pObj.children;
            parentDesc = `${pObj.type} ${batch1.objectPathOf(pObj)}`;
        } else {
            throw new Error(
                `parent is not a container (page name/path or widget with children): ${parent}`
            );
        }
    }
    const js = Object.assign(
        {},
        objects.getDefaultValue(s, cls.classInfo) ?? {},
        { type },
        properties ?? {}
    );
    if (js.left == undefined) js.left = 0;
    if (js.top == undefined) js.top = 0;
    if (js.width == undefined) js.width = 100;
    if (js.height == undefined) js.height = 40;
    const widget = objects.createObject(s, js, cls);
    s.addObject(parentArray, widget);
    await persist(s);
    return {
        created: batch1.objectPathOf(widget),
        type,
        parent: parentDesc
    };
}

async function createScreen(ctx, name, width, height) {
    const s = store(ctx);
    const objects = needToolkit("getEditorObjectToolkit");
    const lvgl = needToolkit("getLvglToolkit");
    const project = s.project;
    if (allPages(project).some(p => p.name === name)) {
        throw new Error(`screen ${name} already exists`);
    }
    const w = width ?? project.settings.general.displayWidth ?? 480;
    const h = height ?? project.settings.general.displayHeight ?? 272;
    const pageProperties = {
        name,
        left: 0,
        top: 0,
        width: w,
        height: h,
        components: [
            {
                type: "LVGLScreenWidget",
                left: 0,
                top: 0,
                width: w,
                height: h,
                leftUnit: "px",
                topUnit: "px",
                widthUnit: "px",
                heightUnit: "px",
                children: []
            }
        ],
        isUsedAsUserWidget: false
    };
    const page = objects.createObject(s, pageProperties, lvgl.Page);
    s.addObject(project.userPages, page);
    await persist(s);
    return { created: batch1.objectPathOf(page), name, width: w, height: h };
}

// ---------------------------------------------------------------------------
// Assets (toolkit: Font/extractFont/encodings, Bitmap/createBitmap)
// ---------------------------------------------------------------------------

function listAssets(ctx) {
    const project = store(ctx).project;
    let builtInFonts;
    try {
        builtInFonts = needToolkit("getLvglToolkit").BUILT_IN_FONTS;
    } catch {
        builtInFonts = [];
    }
    return {
        fonts: (project.fonts ?? []).map(f => ({
            name: f.name,
            bpp: f.bpp,
            size: f.source?.size,
            height: f.height,
            sourceFile: f.source?.filePath,
            lvglRanges: f.lvglRanges || undefined,
            lvglSymbols: f.lvglSymbols || undefined,
            additionalSources: (f.lvglAdditionalSources ?? []).map(
                s => s.filePath
            )
        })),
        builtInFonts,
        bitmaps: (project.bitmaps ?? []).map(b => ({
            name: b.name,
            bpp: b.bpp,
            image: String(b.image ?? "").startsWith("data:")
                ? "(embedded)"
                : b.image
        }))
    };
}

async function addFont(ctx, name, ttfPath, size, bpp, ranges, symbols) {
    const s = store(ctx);
    const objects = needToolkit("getEditorObjectToolkit");
    const assets = needToolkit("getAssetToolkit");
    const project = s.project;
    if ((project.fonts ?? []).some(f => f.name === name)) {
        throw new Error(`font ${name} already exists`);
    }
    const absoluteFilePath = path.resolve(ttfPath);
    if (!fs.existsSync(absoluteFilePath)) {
        throw new Error(`TTF not found: ${ttfPath}`);
    }
    // fonts is an optional array; when missing, create it with a proper
    // parent chain first (updateObject on a raw [] keeps no owner link).
    if (!project.fonts) {
        const arr = objects.createObject(s, [], assets.Font);
        objects.setParent(arr, project);
        s.updateObject(project, { fonts: arr });
    }
    const lvglRanges = ranges || "32-127";
    const lvglSymbols = symbols || "";
    const enc = assets.getLvglEncodingsAndSymbols(lvglRanges, lvglSymbols);
    const fontProperties = await assets.extractFont({
        name,
        absoluteFilePath,
        relativeFilePath: s.getFilePathRelativeToProjectPath(absoluteFilePath),
        renderingEngine: "LVGL",
        bpp: bpp || 4,
        size,
        threshold: (bpp || 4) == 1 ? 128 : 0,
        createGlyphs: true,
        encodings: enc.encodings,
        symbols: enc.symbols,
        createBlankGlyphs: false,
        doNotAddGlyphIfNotFound: false,
        getAllGlyphs: true,
        lvglVersion: project.settings.general.lvglVersion,
        lvglInclude: project.settings.build.lvglInclude
    });
    const font = objects.createObject(
        s,
        { ...fontProperties, lvglRanges, lvglSymbols },
        assets.Font
    );
    s.addObject(project.fonts, font);
    await persist(s);
    return {
        name: font.name,
        glyphCount: font.glyphs?.length ?? 0,
        height: font.height,
        usage: `text_font: "${name}" in styles or localStyles`
    };
}

const MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".gif": "image/gif"
};

async function addImage(ctx, imagePath, name, bpp) {
    const s = store(ctx);
    const objects = needToolkit("getEditorObjectToolkit");
    const assets = needToolkit("getAssetToolkit");
    const project = s.project;
    const abs = path.resolve(imagePath);
    if (!fs.existsSync(abs)) {
        throw new Error(`image not found: ${imagePath}`);
    }
    // Non-embed mode stores a path relative to the project: copy it in.
    let targetPath = abs;
    if (!project.settings.general.embedBitmaps) {
        const projectDir = path.dirname(s.filePath);
        targetPath = path.join(projectDir, "images", path.basename(abs));
        fs.mkdirSync(path.dirname(targetPath), { recursive: true });
        if (path.resolve(targetPath) !== path.resolve(abs)) {
            fs.copyFileSync(abs, targetPath);
        }
    }
    const mime = MIME_BY_EXT[path.extname(abs).toLowerCase()] ?? "image/png";
    if (!project.bitmaps) {
        const arr = objects.createObject(s, [], assets.Bitmap);
        objects.setParent(arr, project);
        s.updateObject(project, { bitmaps: arr });
    }
    const bitmap = await assets.createBitmap(
        s,
        targetPath,
        mime,
        name || undefined,
        bpp || undefined
    );
    if (!bitmap) {
        throw new Error("createBitmap failed");
    }
    s.addObject(project.bitmaps, bitmap);
    await persist(s);
    return {
        name: bitmap.name,
        image: String(bitmap.image).startsWith("data:")
            ? "(embedded)"
            : bitmap.image,
        bpp: bitmap.bpp,
        usage: `set image: "${bitmap.name}" on LVGLImageWidget`
    };
}

// ---------------------------------------------------------------------------
// Build / runtime debug / input injection / variables (store graph)
// ---------------------------------------------------------------------------

async function runBuild(ctx) {
    const s = store(ctx);
    await s.build();
    return batch1.readOutputSection(ctx, "output");
}

function runtimeSummary(s) {
    const r = s.runtime;
    if (!r) {
        return { runtime: "inactive" };
    }
    const logs = r.logs?.logs ?? [];
    return {
        runtime: "active",
        isDebuggerActive: !!r.isDebuggerActive,
        isRunning: !!r.isRunning,
        isPaused: !!r.isPaused,
        isSingleStep: !!r.isSingleStep,
        selectedPage: r.selectedPage?.name,
        logsTail: logs.slice(-30).map(l => ({
            time:
                l.date instanceof Date
                    ? l.date.toLocaleTimeString()
                    : String(l.date),
            type: l.type,
            text: l.label
        }))
    };
}

function debugStatus(ctx) {
    return runtimeSummary(store(ctx));
}

async function debugStart(ctx, mode) {
    const s = store(ctx);
    if (s.runtime) {
        return runtimeSummary(s);
    }
    if (mode === "run") {
        s.onSetRuntimeMode();
    } else {
        s.onSetDebuggerMode();
    }
    for (let i = 0; i < 220; i++) {
        await sleep(400);
        if (s.runtime) {
            await sleep(1500); // first frame
            return runtimeSummary(s);
        }
    }
    return { runtime: "start-timeout (~90s)", hint: "poll debug_status" };
}

async function debugStop(ctx) {
    const s = store(ctx);
    if (!s.runtime) {
        return { runtime: "inactive" };
    }
    await s.onSetEditorMode();
    return { runtime: "stopped" };
}

async function debugControl(ctx, op) {
    const s = store(ctx);
    const r = s.runtime;
    if (!r) {
        throw new Error("runtime not started (debug_start first)");
    }
    switch (op) {
        case "pause":
            await r.pause();
            break;
        case "resume":
            await r.resume();
            break;
        case "step_over":
        case "step_into":
        case "step_out":
            await r.runSingleStep(op);
            break;
        case "restart":
            await s.onRestartRuntimeWithDebuggerActive();
            break;
        default:
            throw new Error(
                `unknown op ${op}; use pause/resume/step_over/step_into/step_out/restart`
            );
    }
    await sleep(300);
    return runtimeSummary(s);
}

function readVariable(ctx, name) {
    const s = store(ctx);
    const dc = s.dataContext;
    if (!dc.has(name)) {
        const vars = (s.project.variables?.globalVariables ?? []).map(
            v => v.name
        );
        return { found: false, hint: `no variable ${name}; have: ${vars.join(", ") || "(none)"}` };
    }
    return { found: true, name, value: mobx.toJS(dc.get(name)) };
}

function writeVariable(ctx, name, value) {
    const s = store(ctx);
    const dc = s.dataContext;
    if (!dc.has(name)) {
        const vars = (s.project.variables?.globalVariables ?? []).map(
            v => v.name
        );
        throw new Error(`no variable ${name}; have: ${vars.join(", ") || "(none)"}`);
    }
    dc.set(name, value);
    return `written ${name} = ${JSON.stringify(mobx.toJS(dc.get(name)))}`;
}

async function sendInput(ctx, op, x, y, dx, dy) {
    const s = store(ctx);
    const runtime = s.runtime;
    if (!runtime) {
        throw new Error("runtime not started (debug_start first)");
    }
    if (runtime.isPaused) {
        throw new Error("runtime paused — events not forwarded (resume first)");
    }
    const page = runtime.selectedPage;
    let ox = 0;
    let oy = 0;
    if (!runtime.isDebuggerActive && page) {
        ox =
            (page.left ?? 0) +
            ((runtime.displayWidth ?? 0) - (page.width ?? 0)) / 2;
        oy =
            (page.top ?? 0) +
            ((runtime.displayHeight ?? 0) - (page.height ?? 0)) / 2;
    }
    const push = (px, py, pressed) =>
        runtime.pointerEvents.push({
            x: Math.round(px + ox),
            y: Math.round(py + oy),
            pressed
        });
    let desc;
    if (op === "click") {
        push(x, y, 1);
        await sleep(150);
        push(x, y, 0);
        desc = `click @(${x},${y})`;
    } else if (op === "press" || op === "release") {
        push(x, y, op === "press" ? 1 : 0);
        desc = `${op} @(${x},${y})`;
    } else if (op === "swipe") {
        const ddx = dx ?? 0;
        const ddy = dy ?? 0;
        const dist = Math.abs(ddx) + Math.abs(ddy);
        if (dist < 5) {
            throw new Error("swipe needs dx/dy");
        }
        const steps = Math.max(2, Math.min(30, Math.round(dist / 20)));
        push(x, y, 1);
        await sleep(60);
        for (let i = 1; i <= steps; i++) {
            push(x + (ddx * i) / steps, y + (ddy * i) / steps, 1);
            await sleep(30);
        }
        await sleep(60);
        push(x + ddx, y + ddy, 0);
        desc = `swipe @(${x},${y}) d(${ddx},${ddy})`;
    } else {
        throw new Error(`unknown op ${op}; use click/press/release/swipe`);
    }
    await sleep(100);
    return `injected ${desc} (page ${page?.name ?? "?"})`;
}

// ---------------------------------------------------------------------------
// Project JSON direct IO (fs + reload through the store)
// ---------------------------------------------------------------------------

function readProjectJson(ctx) {
    const s = store(ctx);
    if (!s.filePath) {
        throw new Error("no project file path on the active editor");
    }
    return fs.readFileSync(s.filePath, "utf-8");
}

async function reloadProject(ctx) {
    const s = store(ctx);
    mobx.runInAction(() => {
        s.savedRevision = s.lastRevision; // skip dirty-confirm dialog
    });
    s.reloadProject();
    for (let i = 0; i < 100; i++) {
        await sleep(200);
        const p = s.project;
        if (p && p._fullyLoaded) {
            await sleep(1200); // let the preview render a frame
            return "project reloaded";
        }
    }
    return "reload wait timeout (10s) — proceeding may read stale state";
}

async function writeProjectJson(ctx, content, doReload) {
    const s = store(ctx);
    if (!s.filePath) {
        throw new Error("no project file path on the active editor");
    }
    JSON.parse(content); // invalid JSON throws to the caller
    fs.writeFileSync(s.filePath, content, "utf-8");
    let msg = `written ${s.filePath} (${content.length} bytes)`;
    if (doReload) {
        msg += "; " + (await reloadProject(ctx));
    } else {
        msg += " (not reloaded — editor still shows the old content)";
    }
    return msg;
}

// ---------------------------------------------------------------------------
// Widget close-up screenshot (DOM crop over the page canvas)
// ---------------------------------------------------------------------------

function absoluteWidgetRect(s, obj) {
    const segs = batch1.objectPathOf(obj).split("/").filter(Boolean);
    let left = obj.left ?? 0;
    let top = obj.top ?? 0;
    for (let n = segs.length - 2; n >= 4; n -= 2) {
        const anc = batch1.fromStringPath(
            s.project,
            "/" + segs.slice(0, n).join("/")
        );
        if (anc && String(anc.type ?? "").startsWith("LVGL")) {
            if ((anc.leftUnit && anc.leftUnit !== "px") ||
                (anc.topUnit && anc.topUnit !== "px")) {
                throw new Error(
                    `ancestor uses non-px units (${anc.leftUnit}/${anc.topUnit}); static geometry unavailable`
                );
            }
            left += anc.left ?? 0;
            top += anc.top ?? 0;
        }
    }
    return { left, top, width: obj.width ?? 0, height: obj.height ?? 0 };
}

async function screenshotObject(ctx, objPath, padding) {
    const s = store(ctx);
    const obj = batch1.resolveObject(s, objPath);
    if (!String(obj.type ?? "").startsWith("LVGL")) {
        throw new Error(`only LVGL widgets, this is ${obj.type ?? "not a widget"}`);
    }
    const pageUrl = batch1.screenshotOnce();
    if (!pageUrl) {
        throw new Error("no visible page canvas — navigate to the page first");
    }
    const rect = absoluteWidgetRect(s, obj);
    const img = await new Promise((resolve, reject) => {
        const im = new Image();
        im.onload = () => resolve(im);
        im.onerror = () => reject(new Error("page screenshot decode failed"));
        im.src = pageUrl;
    });
    const pad = Number.isFinite(padding) ? padding : 8;
    const sx = Math.max(0, Math.round(rect.left - pad));
    const sy = Math.max(0, Math.round(rect.top - pad));
    const sw = Math.min(img.width - sx, Math.round(rect.width + pad * 2));
    const sh = Math.min(img.height - sy, Math.round(rect.height + pad * 2));
    if (sw <= 0 || sh <= 0) {
        throw new Error(
            `widget rect out of page: (${rect.left},${rect.top},${rect.width}x${rect.height}) vs ${img.width}x${img.height}`
        );
    }
    const crop = document.createElement("canvas");
    crop.width = sw;
    crop.height = sh;
    crop.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
    const dataUrl = crop.toDataURL("image/png");
    const dir = path.join(path.dirname(s.filePath || "."), "_shots");
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(
        dir,
        `obj_${new Date().toISOString().replace(/[:.]/g, "-")}.png`
    );
    fs.writeFileSync(file, Buffer.from(dataUrl.split(",")[1], "base64"));
    return { dataUrl, file, rect: { ...rect, padding: pad } };
}

module.exports = {
    setMobx,
    setRendererApiGetter,
    sleep,
    listStyles,
    updateStyle,
    createStyle,
    deleteStyle,
    setThemeColor,
    addThemeColor,
    setPreviewTheme,
    availableWidgetTypes,
    createWidget,
    createScreen,
    listAssets,
    addFont,
    addImage,
    runBuild,
    debugStatus,
    debugStart,
    debugStop,
    debugControl,
    readVariable,
    writeVariable,
    sendInput,
    readProjectJson,
    writeProjectJson,
    reloadProject,
    screenshotObject
};
