/**
 * UIXML for EEZ Studio / LVGL — L1 live sketch + L2 pixel preview + commands.
 *
 * L1 Sketch: instant (250 ms debounce) schematic render of the .uixml drawn
 *            as SVG inside the webview — layout/hierarchy/colors/text, not
 *            LVGL-pixel-accurate. Includes are inlined textually host-side.
 * L2 Pixel:  on save → ir2eez.py compile → EEZ Studio bridge → real canvas
 *            screenshot (paint-stability waited). The golden-grade truth.
 */
const vscode = require("vscode");
const { execFile } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");

let output;
const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);

function out() {
    if (!output) output = vscode.window.createOutputChannel("UIXML");
    return output;
}

function findRepoRoot(uixmlFile) {
    const cfg = vscode.workspace.getConfiguration("uixml").get("repoRoot");
    if (cfg) return cfg;
    // walk up from the .uixml file — our repo layout puts ir2eez.py at the root
    let dir = path.dirname(uixmlFile);
    for (let i = 0; i < 8; i++) {
        if (fs.existsSync(path.join(dir, "ir2eez.py"))) return dir;
        const up = path.dirname(dir);
        if (up === dir) break;
        dir = up;
    }
    // then workspace folders
    for (const wf of vscode.workspace.workspaceFolders || []) {
        if (fs.existsSync(path.join(wf.uri.fsPath, "ir2eez.py"))) return wf.uri.fsPath;
    }
    return path.join(__dirname, "..");
}

function bridgeUrl() {
    return vscode.workspace
        .getConfiguration("uixml")
        .get("bridgeUrl", "http://127.0.0.1:17620")
        .replace(/\/+$/, "");
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

const projectPathFor = (f) => f.replace(/\.uixml$/i, ".eez-project");

function runCompiler(uixmlFile) {
    const py = vscode.workspace.getConfiguration("uixml").get("pythonPath", "python");
    const project = projectPathFor(uixmlFile);
    return new Promise((resolve, reject) => {
        execFile(
            py,
            [path.join(findRepoRoot(uixmlFile), "ir2eez.py"), uixmlFile, "-o", project],
            { timeout: 120000, cwd: path.dirname(uixmlFile) },
            (err, stdout, stderr) => {
                if (err) reject(new Error(`${err.message}\n${(stderr || stdout).slice(-2000)}`));
                else resolve({ stdout: String(stdout), project });
            }
        );
    });
}

/** Reverse channel: pull EEZ Studio hand-edits back into .uixml. The importer
 * self-checks (recompile must reproduce the project canonically) and refuses
 * out-of-subset edits; the previous uixml is kept as .bak. */
function runImport(eezFile) {
    const py = vscode.workspace.getConfiguration("uixml").get("pythonPath", "python");
    const out = eezFile.replace(/\.eez-project$/i, ".uixml");
    return new Promise((resolve, reject) => {
        execFile(
            py,
            [path.join(findRepoRoot(eezFile), "ir2eez.py"), eezFile, "-o", out],
            { timeout: 120000, cwd: path.dirname(eezFile) },
            (err, stdout, stderr) => {
                if (err) reject(new Error(`${err.message}\n${(stderr || stdout).slice(-2500)}`));
                else resolve({ stdout: String(stdout), out });
            }
        );
    });
}

/** Textual include inlining for the sketch: replace <include src=…/> with the
 *  fragment's <ui> inner content (recursion-capped, missing files tolerated). */
function inlineIncludes(text, baseDir, depth = 0) {
    if (depth > 10) return text;
    let changed = false;
    const outText = text.replace(/<include\s+src\s*=\s*"([^"]+)"\s*\/>/g, (m, src) => {
        try {
            const full = path.join(baseDir, src);
            const frag = fs.readFileSync(full, "utf-8");
            const inner = frag.replace(/<\?[^>]*\?>\s*/g, "").match(/<ui[^>]*>([\s\S]*)<\/ui>/);
            changed = true;
            return inner ? inner[1] : "";
        } catch (e) {
            return m;
        }
    });
    if (!changed) return outText;
    return inlineIncludes(outText, baseDir, depth + 1);
}

// ---------------- Preview (L1 sketch + L2 pixel) ----------------

const SKETCH_CSS = `
  body { background:#1e1e1e; color:#ccc; font-family:sans-serif; margin:10px; }
  #bar { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
  button,select { background:#2d2d2d; color:#ddd; border:1px solid #555; padding:2px 10px; cursor:pointer; }
  button.active { border-color:#5EE6C4; color:#5EE6C4; }
  #note { color:#888; font-size:11px; margin-left:auto; }
  #stage { border:1px solid #444; display:inline-block; background:#000; }
  img { max-width:100%; image-rendering:pixelated; display:block; }
  .err { color:#E5484D; font-family:monospace; white-space:pre-wrap; }
`;

const SKETCH_JS = `
  const vscode = acquireVsCodeApi();
  let mode = 'sketch';
  let screens = [];
  let active = null;

  function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');}
  function attrs(el){ const o={}; for(const a of el.attributes) o[a.name]=a.value; return o; }
  function int(o,k,d){ const v=parseInt(o[k]); return isNaN(v)?d:v; }
  function fontSize(o){ const m=(o['font']||'').match(/(\\d+)$/); return m?(+m[1]):14; }

  let STR = {}, VARDEF = {};
  function buildTables(ui) {
    STR = {}; VARDEF = {};
    for (const c of ui.children) {
      if (c.tagName === 'strings') {
        const dflt = attrs(c)['default'] || 'en';
        for (const t of c.children) {
          if (t.tagName !== 'text') continue;
          const ls = [...t.children].filter(n => n.tagName === 'l');
          const hit = ls.find(n => attrs(n)['lang'] === dflt) || ls[0];
          STR[attrs(t)['key']] = hit ? hit.textContent : undefined;
        }
      }
      if (c.tagName === 'variables') {
        for (const v of c.children) if (v.tagName === 'var') {
          const a = attrs(v);
          VARDEF[a['name']] = a['default'] !== undefined ? a['default'] : '0';
        }
      }
    }
  }
  function labelText(o) {
    if (o['tr'] && STR[o['tr']] !== undefined) return STR[o['tr']];
    if (o['tr']) return '[' + o['tr'] + ']';
    if (o['text']) return o['text'];
    if (o['bind'] && VARDEF[o['bind']] !== undefined) return VARDEF[o['bind']];
    if (o['bind']) return '{' + o['bind'] + '}';
    return '';
  }

  function widgetSvg(el, ox, oy, o) {
    const tag = el.tagName;
    const x = ox + int(o,'x',0), y = oy + int(o,'y',0);
    const w = int(o,'w', int(o,'width',80)), h = int(o,'h', int(o,'height',24));
    const fill = o['bg'] || 'none';
    const stroke = o['color'] || '#8FA0BC';
    const rx = int(o,'radius',0);
    let s = '';
    const textAt = (t, color, tx, ty, anchor) =>
      '<text x="'+tx+'" y="'+ty+'" fill="'+(color||stroke)+'" font-size="'+fontSize(o)+
      '" font-family="monospace"'+(anchor?' text-anchor="middle"':'')+'>'+esc(t)+'</text>';
    switch (tag) {
      case 'panel': case 'container':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+fill+
             '" stroke="#3A4B66" stroke-width="1" rx="'+rx+'"/>';
        break;
      case 'label':
        s += textAt(labelText(o), stroke, x+4, y+fontSize(o));
        break;
      case 'button':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+fill+'" stroke="'+stroke+
             '" rx="'+Math.min(rx||8,12)+'"/>' +
             textAt(o['text']||'', o['color']||'#fff', x+w/2, y+h/2+4, true);
        break;
      case 'slider': case 'bar':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="#223" stroke="'+stroke+'"/>' +
             '<rect x="'+x+'" y="'+y+'" width="'+(w*0.4)+'" height="'+h+'" fill="'+stroke+'"/>';
        break;
      case 'switch':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" rx="'+h/2+'" fill="#223" stroke="'+stroke+'"/>' +
             '<circle cx="'+(x+h/2)+'" cy="'+(y+h/2)+'" r="'+h*0.38+'" fill="'+stroke+'"/>';
        break;
      case 'checkbox':
        s += '<rect x="'+x+'" y="'+y+'" width="'+h+'" height="'+h+'" fill="none" stroke="'+stroke+'"/>' +
             textAt(o['text']||'', stroke, x+h+6, y+fontSize(o));
        break;
      case 'led':
        s += '<circle cx="'+(x+w/2)+'" cy="'+(y+h/2)+'" r="'+Math.max(w,h)/2+'" fill="'+(o['color']||'#27AE60')+'"/>';
        break;
      case 'arc': case 'scale': {
        const r = Math.min(w,h)/2, cx = x+w/2, cy = y+h/2;
        s += '<path d="M '+(cx-r)+' '+cy+' A '+r+' '+r+' 0 1 1 '+(cx+r)+' '+cy+
             '" fill="none" stroke="#3A4B66" stroke-width="'+Math.max(4,r*0.08)+'"/>';
        const mn=int(o,'min',0), mx=int(o,'max',100);
        for (const sec of el.children) if (sec.tagName==='section') {
          const sa = attrs(sec);
          const p0=(int(sa,'from',mn)-mn)/(mx-mn)*Math.PI, p1=(int(sa,'to',mx)-mn)/(mx-mn)*Math.PI;
          const large=(p1-p0)>Math.PI?1:0;
          const pt=(ang)=>(cx+r*Math.cos(Math.PI-ang))+' '+(cy-r*Math.sin(Math.PI-ang));
          s += '<path d="M '+pt(p0)+' A '+r+' '+r+' 0 '+large+' 1 '+pt(p1)+
               '" fill="none" stroke="'+(sa['color']||'#E5484D')+'" stroke-width="'+Math.max(4,r*0.1)+'"/>';
        }
        break;
      }
      case 'chart':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+(fill==='none'?'#0B1220':fill)+
             '" stroke="#3A4B66"/>' +
             '<polyline points="'+(x+6)+','+(y+h*0.6)+' '+(x+w*0.35)+','+(y+h*0.35)+' '+(x+w*0.6)+','+(y+h*0.5)+
             ' '+(x+w-6)+','+(y+h*0.3)+'" fill="none" stroke="'+(o['color']||'#5EE6C4')+'"/>';
        break;
      case 'table': {
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+(fill==='none'?'#0B1220':fill)+'" stroke="#3A4B66"/>';
        const cols=int(o,'cols',3), rows=int(o,'rows',4);
        for (let i=1;i<cols;i++) s+='<line x1="'+(x+w*i/cols)+'" y1="'+y+'" x2="'+(x+w*i/cols)+'" y2="'+(y+h)+'" stroke="#334"/>';
        for (let j=1;j<rows;j++) s+='<line x1="'+x+'" y1="'+(y+h*j/rows)+'" x2="'+(x+w)+'" y2="'+(y+h*j/rows)+'" stroke="#334"/>';
        if (o['header']) o['header'].split(',').forEach((t,i)=>{
          s+='<text x="'+(x+w*i/cols+4)+'" y="'+(y+14)+'" fill="#8FA0BC" font-size="11">'+esc(t)+'</text>';});
        break;
      }
      case 'roller': {
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+(fill==='none'?'#1A2438':fill)+'" stroke="#3A4B66"/>';
        (o['options']||'').split(',').slice(0,3).forEach((t,i)=>{
          s+='<text x="'+(x+8)+'" y="'+(y+16+i*18)+'" fill="'+(i===1?'#fff':'#667')+'" font-size="13">'+esc(t)+'</text>';});
        break;
      }
      case 'calendar': {
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+(fill==='none'?'#0B1220':fill)+'" stroke="#3A4B66"/>';
        s += '<text x="'+(x+w/2)+'" y="'+(y+16)+'" fill="#ccc" font-size="12" text-anchor="middle">'+esc(o['today']||'')+'</text>';
        for (let r2=0;r2<6;r2++) for (let c2=0;c2<7;c2++)
          s += '<rect x="'+(x+6+c2*(w-12)/7)+'" y="'+(y+26+r2*(h-32)/6)+'" width="'+((w-12)/7-2)+
               '" height="'+((h-32)/6-2)+'" fill="none" stroke="#2A3550"/>';
        break;
      }
      case 'keyboard':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+(fill==='none'?'#1A2438':fill)+'" stroke="#3A4B66"/>';
        for (let r2=0;r2<4;r2++) for (let c2=0;c2<10;c2++)
          s += '<rect x="'+(x+4+c2*(w-8)/10)+'" y="'+(y+4+r2*(h-8)/4)+'" width="'+((w-8)/10-2)+
               '" height="'+((h-8)/4-2)+'" fill="none" stroke="#3A4B66"/>';
        break;
      case 'tabview': {
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="none" stroke="#3A4B66"/>';
        const bar=int(o,'bar-size', int(o,'barSize',40));
        let tx=x+4;
        for (const t of el.children) if (t.tagName==='tab') {
          const ttl=attrs(t)['title']||'';
          s+='<text x="'+tx+'" y="'+(y+bar/2+4)+'" fill="#8FA0BC" font-size="13">'+esc(ttl)+'</text>';
          tx+=20+ttl.length*8;
        }
        const first=[...el.children].find(t=>t.tagName==='tab');
        if (first) s+=childrenSvg(first, x, y+bar);
        break;
      }
      case 'spinbox':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+(fill==='none'?'#1A2438':fill)+'" stroke="#3A4B66"/>' +
             '<text x="'+(x+8)+'" y="'+(y+h/2+5)+'" fill="#5EE6C4" font-size="'+Math.min(h*0.6,20)+'">[- '+esc(VARDEF[o['bind']]!==undefined?VARDEF[o['bind']]:(o['bind']||'0'))+' +]</text>';
        break;
      case 'textarea':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+(fill==='none'?'#0B1220':fill)+'" stroke="#3A4B66"/>' +
             '<line x1="'+(x+3)+'" y1="'+(y+3)+'" x2="'+(x+3)+'" y2="'+(y+h-3)+'" stroke="#5EE6C4"/>';
        break;
      case 'image':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="none" stroke="#667" stroke-dasharray="4 3"/>' +
             textAt('img:'+(o['src']||''), '#667', x+4, y+fontSize(o));
        break;
      case 'instance':
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="none" stroke="#F2B84B" stroke-dasharray="5 3"/>' +
             textAt('#'+(o['widget']||''), '#F2B84B', x+4, y+fontSize(o));
        break;
      default:
        s += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="none" stroke="#556" stroke-dasharray="3 2"/>' +
             textAt(tag, '#889', x+4, y+fontSize(o));
    }
    if (tag!=='tabview') s += childrenSvg(el, x, y);
    return s;
  }

  function childrenSvg(el, ox, oy) {
    let s='', cursor=0;
    for (const c of el.children) {
      if (['series','section','tab','state','options','header','include'].includes(c.tagName)) continue;
      const ca=attrs(c);
      if (ca['x']!==undefined || ca['y']!==undefined) s+=widgetSvg(c, ox, oy, ca);
      else { s+=widgetSvg(c, ox, oy+cursor, ca); cursor+=int(ca,'h',int(ca,'height',24))+4; }
    }
    return s;
  }

  function renderSketch(xml) {
    const stage=document.getElementById('stage');
    try {
      const doc=new DOMParser().parseFromString(xml,'application/xml');
      const perr=doc.querySelector('parsererror');
      if (perr) throw new Error(perr.textContent.slice(0,300));
      const ui=doc.documentElement;
      if (ui.tagName!=='ui') throw new Error('root is <'+ui.tagName+'>, expected <ui>');
      buildTables(ui);
      let pw=480, ph=320;
      const prj=[...ui.children].find(c=>c.tagName==='project');
      if (prj) { pw=int(attrs(prj),'width',480); ph=int(attrs(prj),'height',320); }
      const scrEls=[...ui.children].filter(c=>c.tagName==='screen');
      screens=scrEls.map(s0=>attrs(s0)['name']||'?');
      const scr=scrEls.find(s0=>attrs(s0)['name']===active)||scrEls[0];
      active=scr?attrs(scr)['name']:null;
      const sel=document.getElementById('screenSel');
      sel.innerHTML=screens.map(n=>'<option '+(n===active?'selected':'')+'>'+esc(n)+'</option>').join('');
      const body=scr?childrenSvg(scr,0,0):'<text x="8" y="20" fill="#888">no screens</text>';
      stage.innerHTML='<svg width="'+pw+'" height="'+ph+'" viewBox="0 0 '+pw+' '+ph+
        '" xmlns="http://www.w3.org/2000/svg"><rect width="'+pw+'" height="'+ph+'" fill="#0B0E14"/>'+body+'</svg>';
      document.getElementById('note').textContent='sketch · '+pw+'×'+ph+' · live';
    } catch (e) {
      stage.innerHTML='<div class="err">'+esc(e.message||e)+'</div>';
    }
  }

  function renderPixel(dataUrl, scrNames) {
    document.getElementById('stage').innerHTML='<img src="'+dataUrl+'"/>';
    document.getElementById('note').textContent='pixel · golden-grade';
    if (scrNames) { screens=scrNames; const sel=document.getElementById('screenSel');
      if (sel && !sel.options.length) sel.innerHTML=scrNames.map(n=>'<option '+(n===active?'selected':'')+'>'+esc(n)+'</option>').join(''); }
  }

  document.getElementById('btnSketch').onclick=()=>{ setMode('sketch'); vscode.postMessage({command:'mode',mode:'sketch'}); };
  document.getElementById('btnPixel').onclick=()=>{ setMode('pixel'); vscode.postMessage({command:'mode',mode:'pixel'}); };
  document.getElementById('screenSel').onchange=(e)=>{ active=e.target.value; vscode.postMessage({command:'screen',screen:active}); };
  function setMode(m) {
    mode=m;
    document.getElementById('btnSketch').className=m==='sketch'?'active':'';
    document.getElementById('btnPixel').className=m==='pixel'?'active':'';
    document.getElementById('stage').innerHTML='<div style="color:#888;padding:20px">'+
      (m==='pixel'?'compiling &amp; rendering…':'loading…')+'</div>';
  }
  window.addEventListener('message',(ev)=>{
    const m=ev.data;
    if (m.command==='sketch' && mode==='sketch') renderSketch(m.xml);
    if (m.command==='pixel') renderPixel(m.dataUrl, m.screens);
    if (m.command==='pixelErr') document.getElementById('stage').innerHTML='<div class="err">'+esc(String(m.error).slice(0,400))+'</div>';
  });
`;

class PreviewProvider {
    constructor() {
        this.panel = null;
        this.currentFile = null;
        this.screen = null;
        this.mode = "sketch";
        this.pixelBusy = false;
        this.sketchTimer = null;
    }

    shell() {
        return `<!DOCTYPE html><html><head><style>${SKETCH_CSS}</style></head><body>
        <div id="bar">
          <button id="btnSketch" class="active">Sketch</button>
          <button id="btnPixel">Pixel</button>
          <select id="screenSel"></select>
          <span id="note">sketch · live</span>
        </div>
        <div id="stage"></div>
        <script>${SKETCH_JS}</script></body></html>`;
    }

    show(uixmlFile) {
        if (!this.panel) {
            this.panel = vscode.window.createWebviewPanel(
                "uixmlPreview", "UIXML Preview", vscode.ViewColumn.Beside, { enableScripts: true }
            );
            this.panel.webview.html = this.shell();
            this.panel.onDidDispose(() => { this.panel = null; this.currentFile = null; });
            this.panel.webview.onDidReceiveMessage((m) => {
                if (m.command === "screen") { this.screen = m.screen; this.refreshPixel(); }
                if (m.command === "mode") { this.mode = m.mode; if (m.mode === "pixel") this.refreshPixel(); }
            });
        }
        this.panel.reveal();
        this.currentFile = uixmlFile;
        this.pushSketch();
    }

    pushSketch() {
        if (!this.panel || !this.currentFile) return;
        try {
            let text = fs.readFileSync(this.currentFile, "utf-8");
            text = inlineIncludes(text, path.dirname(this.currentFile));
            this.panel.webview.postMessage({ command: "sketch", xml: text });
        } catch (e) {
            this.panel.webview.postMessage({ command: "pixelErr", error: String(e.message || e) });
        }
    }

    onDocChanged(doc) {
        if (!this.panel || this.currentFile !== doc.fileName) return;
        clearTimeout(this.sketchTimer);
        this.sketchTimer = setTimeout(() => {
            let text = doc.getText();
            text = inlineIncludes(text, path.dirname(doc.fileName));
            this.panel && this.panel.webview.postMessage({ command: "sketch", xml: text });
        }, 250);
    }

    async refreshPixel() {
        if (!this.panel || !this.currentFile || this.pixelBusy) return;
        this.pixelBusy = true;
        status.text = "uixml: rendering (pixel)…";
        try {
            const project = projectPathFor(this.currentFile);
            await runCompiler(this.currentFile);
            await bridgeCall("open_project", { path: project.replace(/\\/g, "/") });
            await bridgeCall("reload", {});
            await new Promise((r) => setTimeout(r, 3500));
            let screens = [];
            try { screens = ((await bridgeCall("list_objects", {})).screens || []).map((s) => s.name); } catch {}
            const target = this.screen && screens.includes(this.screen) ? this.screen : screens[0];
            if (target) {
                await bridgeCall("navigate", { screen: target, object: `screen_${target}` });
                await new Promise((r) => setTimeout(r, 1500));
            }
            let prev = await bridgeCall("screenshot", {});
            for (let i = 0; i < 5; i++) {
                await new Promise((r) => setTimeout(r, 800));
                const cur = await bridgeCall("screenshot", {});
                if (cur.dataUrl === prev.dataUrl) break;
                prev = cur;
            }
            this.panel.webview.postMessage({ command: "pixel", dataUrl: prev.dataUrl, screens });
            status.text = `uixml: ${path.basename(this.currentFile)} ✓`;
        } catch (e) {
            this.panel.webview.postMessage({ command: "pixelErr", error: String(e.message || e) });
            status.text = "uixml: pixel preview failed";
        } finally {
            this.pixelBusy = false;
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
        return ed && ed.document.fileName.toLowerCase().endsWith(".uixml") ? ed.document.fileName : null;
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

    const importCmd = vscode.commands.registerCommand("uixml.import", async () => {
        let eez = vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.fileName;
        if (!eez || !eez.toLowerCase().endsWith(".eez-project")) {
            const picked = await vscode.window.showOpenDialog({
                title: "UIXML: pick the .eez-project to import (Studio hand-edits flow back to XML)",
                filters: { "EEZ Studio project": ["eez-project"] },
            });
            if (!picked) return;
            eez = picked[0].fsPath;
        }
        status.text = "uixml: importing…";
        try {
            const r = await runImport(eez);
            out().appendLine(r.stdout);
            status.text = "uixml: imported ✓";
            vscode.window.showInformationMessage(`Imported → ${path.basename(r.out)} (self-check passed, .bak kept)`);
            vscode.window.showTextDocument(vscode.Uri.file(r.out));
        } catch (e) {
            out().appendLine(String(e.message || e));
            out().show();
            status.text = "uixml: import refused";
            vscode.window.showErrorMessage("Import refused — see UIXML output (out-of-subset edits or missing side-cars)");
        }
    });

    const onChange = vscode.workspace.onDidChangeTextDocument((e) => preview.onDocChanged(e.document));
    const onSave = vscode.workspace.onDidSaveTextDocument((doc) => {
        if (doc.fileName.toLowerCase().endsWith(".uixml") && preview.currentFile === doc.fileName) {
            preview.refreshPixel();
        }
    });

    context.subscriptions.push(compileCmd, checkCmd, previewCmd, importCmd, onChange, onSave, status);
}

function deactivate() {}

module.exports = { activate, deactivate };
