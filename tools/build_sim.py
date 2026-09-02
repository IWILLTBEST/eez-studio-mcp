#!/usr/bin/env python3
"""build_sim — compile a UIXML project into a browser simulator (WASM).

Chain: .uixml → ir2eez → .eez-project → Studio build (bridge, exports the
firmware C set) → shims (font symbol rename, native action/var stubs) →
emcc object-cached compile → sim/index.html (real firmware code in browser).

The real LVGL + the exported firmware sources run in the browser — clicks
drive real flows, animations run, same C a device builds.

用法:
    python tools/build_sim.py examples/motor/motor.uixml
    python tools/build_sim.py examples/motor/motor.eez-project   # skip uixml step
    --no-export   reuse the existing exported C (fast probe iterations)
Environment:
    EMSDK_ROOT  (default E:/eez_studio_project/emsdk-main)
    LVGL_ROOT   (default E:/eez_studio_project/third_party/lvgl)
    EEZ_BRIDGE_URL (default http://127.0.0.1:17620)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

EMSDK_ROOT = os.environ.get("EMSDK_ROOT", r"E:\eez_studio_project\emsdk-main")
LVGL_ROOT = os.environ.get("LVGL_ROOT", r"E:\eez_studio_project\third_party\lvgl")
BRIDGE = os.environ.get("EEZ_BRIDGE_URL", "http://127.0.0.1:17620").rstrip("/") + "/tool"


def bridge(tool: str, args: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        BRIDGE, data=json.dumps({"tool": tool, "args": args}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    if not out.get("ok"):
        raise RuntimeError(f"bridge {tool}: {out}")
    return out.get("result", {})


def emcc_cmd() -> list:
    return [os.path.join(EMSDK_ROOT, "upstream", "emscripten", "emcc.exe")]


def emcc_env() -> dict:
    env = dict(os.environ)
    env["EM_CONFIG"] = os.path.join(EMSDK_ROOT, ".emscripten")
    return env


def build_objects(srcs: list, obj_dir: str, flags: list, what: str) -> list:
    """Timestamp-cached per-file compile; returns .o paths."""
    os.makedirs(obj_dir, exist_ok=True)
    objs, rebuilt = [], 0
    for src in srcs:
        tag = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.relpath(src, ROOT if src.startswith(ROOT) else os.path.dirname(src)))
        if len(tag) > 100:
            tag = tag[-100:]
        obj = os.path.join(obj_dir, tag + ".o")
        if not os.path.exists(obj) or os.path.getmtime(obj) < os.path.getmtime(src):
            r = subprocess.run(emcc_cmd() + flags + ["-c", src, "-o", obj],
                               capture_output=True, text=True, env=emcc_env())
            if r.returncode != 0:
                errs = [l for l in (r.stderr or "").splitlines() if "error" in l.lower()]
                raise RuntimeError(f"compile {src}:\n" + "\n".join(errs[:8]))
            rebuilt += 1
        objs.append(obj)
    print(f"  {what}: {len(objs)} objects ({rebuilt} rebuilt)")
    return objs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help=".uixml or .eez-project")
    ap.add_argument("--out", default=None, help="sim output dir (default <project>/sim)")
    ap.add_argument("--no-export", action="store_true",
                    help="skip ir2eez + Studio build (probe iterations)")
    args = ap.parse_args()

    src = os.path.abspath(args.input)
    if src.endswith((".uixml", ".xml")):
        proj = os.path.splitext(src)[0] + ".eez-project"
        if not args.no_export:
            print("• ir2eez compile")
            r = subprocess.run([sys.executable, os.path.join(ROOT, "ir2eez.py"),
                                src, "-o", proj], cwd=ROOT, capture_output=True, text=True)
            if r.returncode != 0:
                print(r.stdout, r.stderr)
                return 1
    else:
        proj = src
    pdir = os.path.dirname(proj)
    sim_dir = args.out or os.path.join(pdir, "sim")
    os.makedirs(sim_dir, exist_ok=True)
    obj_dir = os.path.join(sim_dir, "obj")

    gen = os.path.join(pdir, "screens.c")
    if not args.no_export:
        print("• Studio build (C export via bridge)")
        bridge("open_project", {"path": proj.replace("\\", "/")})
        bridge("reload", {})   # open_project only switches tabs on an open project
        res = bridge("build_project", {})
        if res.get("numErrors"):
            print(f"✗ Studio build errors: {res['numErrors']}")
            return 1
    elif not os.path.exists(gen):
        print("✗ no exported C — drop --no-export for the first build")
        return 1

    # Studio can emit the flow_def.h extern with the compressed size while the
    # data section is uncompressed (its own LVGL path) — re-align it with the
    # actual array in flow_def.c. Studio 的 extern 尺寸可能与数据段口径不一致，这里对齐。
    fdc = open(os.path.join(pdir, "flow_def.c"), encoding="utf-8").read()
    real_size = re.search(r"const uint8_t assets\[(\d+)\]", fdc).group(1)
    fdh_path = os.path.join(pdir, "flow_def.h")
    fdh = open(fdh_path, encoding="utf-8").read()
    fdh2 = re.sub(r"extern const uint8_t assets\[\d+\];",
                  f"extern const uint8_t assets[{real_size}];", fdh)
    if fdh2 != fdh:
        open(fdh_path, "w", encoding="utf-8", newline="\n").write(fdh2)
        print(f"  flow_def.h extern re-aligned to {real_size}")

    meta = json.load(open(proj, encoding="utf-8"))
    gen = meta["settings"]["general"]
    W, H = gen.get("displayWidth", 480), gen.get("displayHeight", 320)

    print(f"• shims ({W}x{H})")
    # 1) fonts: copy fonts/<name>.c with the symbol renamed to ui_font_<name>
    font_objs_src = []
    for m in re.finditer(r"extern const lv_font_t ui_font_(\w+);",
                         open(os.path.join(pdir, "fonts.h"), encoding="utf-8").read()):
        fname = m.group(1)
        fsrc = os.path.join(ROOT, "fonts", f"{fname}.c")
        if not os.path.exists(fsrc):
            print(f"✗ fonts/{fname}.c missing from the repo font catalog")
            return 1
        body = open(fsrc, encoding="utf-8", errors="ignore").read()
        sym = re.search(r"lv_font_t\s+(\w+)\s*=\s*\{", body).group(1)
        dst = os.path.join(sim_dir, f"font_{fname}.c")
        open(dst, "w", encoding="utf-8", newline="\n").write(body.replace(sym, f"ui_font_{fname}"))
        font_objs_src.append(dst)
    # 2) native action stubs
    acts = re.findall(r"extern void (\w+)\(lv_event_t \* e\);",
                      open(os.path.join(pdir, "actions.h"), encoding="utf-8").read())
    open(os.path.join(sim_dir, "actions_stub.c"), "w", encoding="utf-8", newline="\n").write(
        '#include "lvgl/lvgl.h"\n' +
        "".join(f"void {a}(lv_event_t *e) {{ (void)e; }}\n" for a in acts))
    # 3) native variable accessors (typed static storage)
    vh = open(os.path.join(pdir, "vars.h"), encoding="utf-8").read()
    lines = [l.strip() for l in vh.splitlines() if l.strip().startswith("extern")]
    out = ['#include "lvgl/lvgl.h"', "#include <stdint.h>", "#include <stdbool.h>",
           "#include <string.h>", ""]
    i = 0
    while i < len(lines) - 1:
        m1 = re.match(r"extern\s+(.+?)get_var_(\w+)\(\);", lines[i])
        m2 = re.match(r"extern\s+void\s+set_var_(\w+)\(", lines[i + 1])
        if m1 and m2 and m1.group(2) == m2.group(1):
            ret, name = m1.group(1).strip(), m1.group(2)
            if "char" in ret:
                out += [f'static char s_var_{name}[128];',
                        f'const char *get_var_{name}() {{ return s_var_{name}; }}',
                        f'void set_var_{name}(const char *v) {{ strncpy(s_var_{name}, v, 127); s_var_{name}[127] = 0; }}', '']
            else:
                out += [f'static {ret} s_var_{name};',
                        f'{ret} get_var_{name}() {{ return s_var_{name}; }}',
                        f'void set_var_{name}({ret} v) {{ s_var_{name} = v; }}', '']
            i += 2
            continue
        i += 1
    open(os.path.join(sim_dir, "vars_stub.c"), "w", encoding="utf-8", newline="\n").write("\n".join(out))
    # 4) pre-js: canvas size + blit wiring. Must live here, NOT in shell's
    #    onRuntimeInitialized — this emcc glue does not merge the page's Module
    #    object, so that hook never fires; pre-js binds to the real Module.
    open(os.path.join(sim_dir, "sim_pre.js"), "w", encoding="utf-8", newline="\n").write(
        "Module.simW = %d; Module.simH = %d;\n"
        "Module.canvas = document.getElementById('canvas');\n"
        "Module.canvas.width = Module.simW; Module.canvas.height = Module.simH;\n"
        "var __g = Module.canvas.getContext('2d');\n"
        "Module.simImage = __g.createImageData(Module.simW, Module.simH);\n"
        "Module.simBlit = function(bytes, w, h) {\n"
        "  if (w !== Module.simW || h !== Module.simH) return;\n"
        "  Module.simImage.data.set(bytes);\n"
        "  __g.putImageData(Module.simImage, 0, 0);\n"
        "};\n"
        "window.shellFit = function() {\n"
        "  var wrap = document.getElementById('wrap'), c = Module.canvas;\n"
        "  var s = Math.min((wrap.clientWidth - 16) / c.width,\n"
        "                   (wrap.clientHeight - 16) / c.height, 1.5);\n"
        "  c.style.width = Math.round(c.width * s) + 'px';\n"
        "  c.style.height = Math.round(c.height * s) + 'px';\n"
        "};\n"
        "document.getElementById('proj').textContent = document.title;\n"
        "shellFit(); window.addEventListener('resize', shellFit);\n" % (W, H))

    print("• compile (object-cached)")
    common = ["-DLV_CONF_INCLUDE_SIMPLE",
              "-DSIM_W=" + str(W), "-DSIM_H=" + str(H),
              "-I" + os.path.join(ROOT, "tools", "sim"),
              "-I" + LVGL_ROOT,
              "-I" + os.path.dirname(LVGL_ROOT),
              "-I" + pdir,
              "-include", os.path.join(pdir, "eez-flow.h"),
              "-O1", "-Wall"]
    t0 = time.time()
    lvgl_srcs = sorted(glob.glob(os.path.join(LVGL_ROOT, "src", "**", "*.c"), recursive=True))
    proj_srcs = [os.path.join(pdir, f) for f in
                 ("screens.c", "flow_def.c", "images.c", "styles.c", "ui.c")]
    objs = []
    objs += build_objects(proj_srcs + font_objs_src +
                          [os.path.join(sim_dir, "actions_stub.c"),
                           os.path.join(sim_dir, "vars_stub.c")],
                          obj_dir, common, "project")
    objs += build_objects([os.path.join(pdir, "eez-flow.cpp")], obj_dir,
                          common + ["-std=c++17"], "eez-framework")
    objs += build_objects(lvgl_srcs, obj_dir, common, "lvgl")
    objs += build_objects([os.path.join(ROOT, "tools", "sim", "main_sim.c")],
                          obj_dir, common, "sim-shell")
    print(f"  compiled in {time.time()-t0:.0f}s")

    print("• link")
    html = os.path.join(sim_dir, "index.html")
    link_args = objs + [
        "-lc++", "-lc++abi",
        "-sALLOW_MEMORY_GROWTH=1", "-sWASM=1",
        "--pre-js", os.path.join(sim_dir, "sim_pre.js"),
        "--shell-file", os.path.join(ROOT, "tools", "sim", "shell.html"),
        "-o", html]
    rsp = os.path.join(sim_dir, "link_args.txt")
    open(rsp, "w", encoding="utf-8", newline="\n").write(
        "\n".join(a.replace("\\", "/") for a in link_args))
    r = subprocess.run(emcc_cmd() + ["@" + rsp],
                       capture_output=True, text=True, env=emcc_env())
    if r.returncode != 0:
        errs = [l for l in (r.stderr or "").splitlines() if "error" in l.lower()]
        print("✗ link:\n" + "\n".join(errs[:10]))
        return 1
    print(f"✓ {html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
