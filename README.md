# eez-studio-mcp

[![pr-check](https://github.com/IWILLTBEST/eez-studio-mcp/actions/workflows/pr-check.yml/badge.svg)](https://github.com/IWILLTBEST/eez-studio-mcp/actions/workflows/pr-check.yml)

**Turn EEZ Studio into an AI-drivable LVGL UI editor.**

An MCP (Model Context Protocol) server + AI skill + IR compiler that lets Claude, Cursor, ZCode, DSH or any MCP client read and edit LVGL projects *inside* EEZ Studio — widget by widget, style by style — with screenshots, live checking, a wasm simulator, input injection, **visual-regression goldens and a headless CI pipeline** that re-verifies every push, pixel by pixel.

[中文文档 (Chinese)](README.zh-CN.md) · [Patched EEZ Studio](https://github.com/IWILLTBEST/studio) (required runtime)

**Repo map** — one repository, three parts:

| Part | Contents | Who uses it |
|---|---|---|
| **Core** | `ir2eez.py` `eez2ir.py` `uixml.py` `tools/` (`build_sim.py`, `split_uixml.py`, `sim/`) | The IR↔UIXML compiler + simulator shells — the shared foundation both lines below are built on |
| **MCP line** | `mcp-server.mjs` / `eez_mcp_server.py` + [`studio-extension/`](studio-extension/) | Point any MCP client at the server to drive EEZ Studio; the `.eez-extension` installs into Studio |
| **Qt line** | [`vscode/`](vscode/) | Standalone VS Code extension (UIXML editing, preview, Run) — needs the core, not the MCP line |

![architecture](docs/img/architecture.svg)

## Screenshots

All three screens below were generated from the IR compiler (`examples/motor-en`, the English variant) and captured through the MCP `screenshot` tool — no manual touch. A Chinese-language variant lives in `examples/motor`:

| Overview | Params | Alarms |
|:---:|:---:|:---:|
| ![overview](docs/img/motor-en-overview.png) | ![params](docs/img/motor-en-params.png) | ![alarms](docs/img/motor-en-alarms.png) |

And a **glassmorphism + entrance-animation showcase** ([examples/glass](examples/glass) — translucent cards, shadows, gradient background, staggered entrance via the IR `anim` verb; press **Replay** in the simulator):

<p><img src="docs/img/glass-dashboard.png" width="480" alt="glass dashboard"></p>

**i18n, one IR two languages** ([examples/i18n](examples/i18n) — labels compile to `T"key"` expressions resolved by lv_i18n on target; the canvas previews the default language via previewValue, so switching `strings.default` and recompiling re-renders the same keys in the other language — both images below are fresh captures of the *public* font chain, CJK glyphs come from a Noto Sans SC (OFL) subset merged into the demo fonts):

| English (`"default": "en"`) | 中文 (`"default": "zh"`) |
|:---:|:---:|
| ![i18n en](docs/img/i18n-en.png) | ![i18n zh](docs/img/i18n-zh.png) |

And the **rich-data demo** ([examples/richdata](examples/richdata) — roller with a two-way bound selection, gauge with colored sections, calendar, spinbox, keyboard+textarea, tabview with editable tabs; chart/table compile as bare LVGL objects configured by a generated `ui_ext.c`):

| main (roller · chart · table) | controls (scale · calendar · spinbox · keyboard) | settings (tabview) |
|:---:|:---:|:---:|
| ![richdata](docs/img/richdata.png) | ![controls](docs/img/richdata-controls.png) | ![settings](docs/img/richdata-settings.png) |

And a **per-widget close-up** (`screenshot_object` returns just one widget — handy for AI self-verification):

<p><img src="docs/img/widget-closeup-en.png" width="220" alt="widget closeup"></p>

## What can the AI do?

| Domain | Tools | Highlights |
|---|---|---|
| **Widget editing** | `list_objects` `get_object` `update_object` `create_widget` `delete_object` `create_screen` `undo` `redo` | Address widgets by path **or stable objID**; every edit is an undoable command and auto-saves |
| **Styles & themes** | `list_styles` `update_style` `create_style` `delete_style` `set_theme_color` `add_color` `set_preview_theme` | Edit `definition[part][state]` props; switch theme preview and re-screenshot |
| **Visual loop** | `screenshot` `screenshot_object` `goto_object` `get_selection` | Page PNG, single-widget close-up, locate an object, read what the *user* selected |
| **Visual regression** | `visual_baseline` `visual_check` | Lock a golden screenshot per screen; pixel-compare with anti-aliasing tolerance — failures report `changedPixels`/`changedPct`/`bbox` and a red-annotated diff image |
| **Diagnostics** | `read_output` `check` `build_project` | Read Checks/Output panels; run full check or C-source build |
| **Runtime & input** | `debug_start/stop/control/status` `read_variable` `write_variable` `send_input` | Drive the LVGL wasm simulator: pause/step, read/write variables, **inject clicks & swipes** (page navigation verified end-to-end) |
| **Project files** | `read_project_json` `write_project_json` `patch_project_json` | RFC 7396 merge-patch and RFC 6902 JSON-Patch for surgical bulk edits |
| **Multi-project** | `list_projects` `select_project` `open_project` | Tab-level project switching, dead-tab recovery |
| **Assets** | `list_assets` `add_font` `add_image` | TTF→LVGL font via the built-in lv_font_conv pipeline (ranges + CJK symbols), images with auto-copy |
| **IR pipeline** | `read_ir` `write_ir` `compile` `reload` `navigate` `ping` | The original generate-from-IR loop |

Protocol extras: **live resources** (`eez://checks`, `eez://debug`, `eez://state`) with change push (~0.4 s), and **progress notifications** for long operations (`check`, `build_project`, `debug_start`, `add_font`, …).

## The IR compiler & firmware contract

`ir2eez.py` compiles a declarative source file into a `.eez-project` — the format is **UIXML**, our own XML vocabulary (attribute-per-field, `xmlns:lv` style passthrough, native comments; deliberately NOT the LVGL XML Specification, whose license forbids third-party generators). Legacy `.ir.json` still compiles — **23 widget types** now, from the basics (label/button/slider/arc/…) through the rich set (roller, table, chart, scale, calendar, keyboard, spinbox, tabview). Three sidecar artifacts come out next to the project:

```text
motor-demo.eez-project     # open in EEZ Studio — native format
action.h                   # firmware porting contract: your native-action callbacks
bus.ui_ext.h / .ui_ext.c   # chart/table runtime setup (series, ranges, table header)
translations.yaml          # lv_i18n source format, one row per key × language
```

The native-action contract (`action.h`):

```c
// action.h — auto-generated
void on_speed(int32_t value);   // slider/arc: current value
void on_fwd(int32_t value);     // switch: 0/1
void on_poles(int32_t value);   // dropdown: option index
void ack_alarm(void);           // click: no args
```

The UI binds global variables *down* (firmware changes a variable → every bound widget refreshes each tick) and fires native actions *up* (user drags a slider → your C callback runs). Implement the callbacks, include the header, and the port is done. Tool output is yours — not GPL-covered (same as GCC output).

**Firmware build**: projects carry the official 14-file EEZ build template, so a build emits `screens.c` with `create_screens()` — every widget creation call plus `objects.<id>` named handles — `flow_def.c` (assets + native var table), `actions.h`, styles/fonts/images. Charts and tables (bare LVGL objects by design in EEZ) are configured by the generated `ui_ext_init()`; firmware wiring is three lines:

```c
ui_init();          // studio-generated: engine + assets + handles
ui_ext_init();      // ir2eez-generated: chart series/ranges, table structure
while (1) { lv_timer_handler(); ui_tick(); /* feed data: set_var_speed(...), chart_bus_push(0, v) */ }
```

**i18n**: label `"tr": "key"` compiles to a `T"key"` expression (resolved on target via the EEZ Flow translate hook → lv_i18n; [upstream #1045](https://github.com/eez-open/studio/pull/1045)); the canvas previews the default language through previewValue. **Animations**: the IR `anim` verb drives all seven EEZ anim actions with `repeat` and `playback` (ping-pong) — [upstream #1049](https://github.com/eez-open/studio/pull/1049).

```bash
git clone https://github.com/IWILLTBEST/eez-studio-mcp && cd eez-studio-mcp
pip install mcp httpx
python ir2eez.py examples/motor-en/motor-en.uixml -o motor-demo.eez-project
# → motor-demo.eez-project + action.h (12 native actions); 中文版: examples/motor/motor.uixml
```

## UIXML: split form & the reverse channel

A project can live as **one file** or in the **Qt-style split** — planes separated by who edits them, stitched by a manifest with `<include>`:

```text
examples/motor/
├── motor.uixml              # manifest (named after the example — editor tabs stay distinguishable)
├── logic.uixml              # project header + <var> + <action>  (the firmware engineer's interface)
├── widgets/StatusBar.uixml  # reusable user widgets, one file each
└── screens/{overview,params,alarms}.uixml   # the UI plane, one screen per file
```

`tools/split_uixml.py <src.uixml>` converts a single file to the split form and **self-checks** (the manifest must re-parse to the identical IR; it swaps atomically so a failed check never touches your source). `strings.uixml` appears as a fourth plane when the project has `tr` keys — translators touch only that file.

The **reverse channel** closes the loop: hand edits made in EEZ Studio flow back to XML via `UIXML: Import from .eez-project` (`ir2eez.py <proj>.eez-project -o <out>.uixml`). The importer is a faithful mirror of the compiler — decompile → recompile → canonical compare; anything outside the round-trippable subset makes it **refuse with a precise error** rather than silently drop fields. Rich-widget structure (table columns, chart series, roller options — things that never enter the `.eez-project`) rides the `ir_meta.json` side-car. Importing a split-form build writes `<name>-imported.uixml` (a complete single file) so the manifest is never clobbered.

## The VS Code extension

[`vscode/`](vscode/) turns the pipeline into an editor experience — syntax highlighting, XSD validation and **two status-bar buttons**:

| | |
|---|---|
| 👁 **UIXML Preview** | webview with two modes: **Sketch** (instant SVG wireframe, includes inlined, `tr`/`bind` resolved to real values, 250 ms debounce) and **Pixel** (the actual EEZ canvas screenshot via the bridge — golden-grade truth) |
| ▶ **UIXML Run** | builds & opens the **WASM simulator**; live build progress on the button *and* in the UIXML output panel (each step: compile → Studio export → objects → link), instant open when nothing changed |

Commands: `UIXML: Compile` · `UIXML: Check` · `UIXML: Preview` · `UIXML: Run` · `UIXML: Import from .eez-project`. Editor support: TextMate grammar for the whole vocabulary (including `<?xml?>` PI coloring) plus an XSD for attribute completion/validation (works with the Red Hat XML extension).

Install from source:

```bash
cd vscode && npx @vscode/vsce package --no-dependencies
code --install-extension uixml-preview-*.vsix
```

Sketch works offline; Pixel and Run need the bridge (any Studio instance from [Setup](#setup)). Zero config: the extension locates the repo root by walking up from the `.uixml` file.

## The wasm simulator

`tools/build_sim.py <project.uixml>` runs the whole chain — **uixml → ir2eez → Studio C export (bridge) → emcc → `build/sim/index.html`** — and the result is *the real firmware C* (LVGL 9 + the eez-framework amalgamation) executing in the browser: interactive, flows included, `T"key"` labels translated at runtime through a generated key table. Object files are cached and shared across projects (`.sim-cache/`, keyed by `lv_conf.h`), so a fresh project's first build takes ~6 s after the cache is warm. Requirements on top of the bridge: LVGL sources and an emsdk under `third_party/` (not committed — see `tools/sim/` for the shell and config).

## Visual regression & headless CI

The delivery discipline: **IR change → compile → check 0/0 → golden match**. `tools/visreg.py` drives the bridge (open → reload → navigate → screenshot, waiting for paint stability), stores goldens under `golden/` and pixel-compares captures with a per-channel tolerance (anti-aliasing) plus a changed-pixel threshold — failures return the bounding box and a red-annotated diff image.

[`.github/workflows/pr-check.yml`](.github/workflows/pr-check.yml) runs all of it **headless on every push/PR**: build the Studio fork on Ubuntu, run it under Xvfb, wait for the bridge, then `tools/ci-check.py` regenerates + compiles + checks + golden-compares every example (18 steps). A pleasant property fell out of this: the EEZ canvas renders from the project's embedded bitmap fonts, so **Linux CI screenshots are bit-identical to the Windows goldens** (0.0% drift) — goldens are portable cross-platform truth. Failing runs upload the diff images as artifacts.

## Setup

> **Status 2026-09**: the full extension interface landed upstream ([eez-open/studio#1043](https://github.com/eez-open/studio/pull/1043) + [#1044](https://github.com/eez-open/studio/pull/1044) + [#1047](https://github.com/eez-open/studio/pull/1047)) — an installed extension now receives everything it needs: `api.renderer` (`getOpenProjects`, `getActiveProjectStore`, `activateProjectTab`, `openProject`, `requireModule`) plus three capability toolkits (object model / LVGL / assets). The [`studio-extension/`](studio-extension/) runs the **full 47-tool set end-to-end** on that API alone. Animation `repeat`/`playback` ([#1049](https://github.com/eez-open/studio/pull/1049)) is merged. Following the [#1050](https://github.com/eez-open/studio/issues/1050) discussion we contributed native Chart ([#1051](https://github.com/eez-open/studio/pull/1051)) and Table ([#1052](https://github.com/eez-open/studio/pull/1052)) widget properties upstream — in review.

**Preferred once your Studio build includes those PRs** — install the packaged extension ([release `extension-v0.2.0`](https://github.com/IWILLTBEST/eez-studio-mcp/releases/tag/extension-v0.2.0), `.eez-extension` file) via the extensions manager, and skip the fork entirely:

1. **EEZ Studio** — build [eez-open/studio](https://github.com/eez-open/studio) master (or any release after these PRs), install the `.eez-extension` from the release above, open a project. The extension's bridge listens on `127.0.0.1:17620`.

   Until a Studio release ships the API, the patched fork below is the easiest runtime — it has the bridge built in, no extension needed:

   ```bash
   git clone https://github.com/IWILLTBEST/studio
   cd studio && npm install && npm start
   ```

   The bridge listens on `127.0.0.1:17620` either way. Open or create a project.

2. **Register the MCP server** with your client (see `claude_desktop_config.example.json`). The MCP layer ships as **two interchangeable implementations** — same 47 tools, resources, prompts and progress notifications on both:

   - **Node.js (recommended)** — `mcp-server.mjs`, **zero npm dependencies**: JSON-RPC over stdio is implemented by hand (newline-delimited JSON). Needs only Node.js 18+:

     ```json
     {
       "mcpServers": {
         "eez-studio": { "command": "node", "args": ["<repo>/mcp-server.mjs"] }
       }
     }
     ```

   - **Python** — `eez_mcp_server.py`, built on the official `mcp` SDK (Python 3.10+, `pip install mcp httpx`):

     ```json
     {
       "mcpServers": {
         "eez-studio": { "command": "python", "args": ["<repo>/eez_mcp_server.py"] }
       }
     }
     ```

3. **Talk to it** — e.g. *“list the screens”*, *“change the navbar indicator to green and show me a screenshot”*, *“drag the speed slider and tell me which page the simulator is on”*.

4. **(Optional) install the skill** — copy `SKILL.md` into your agent's skills directory. It encodes the accumulated engineering rules: manual-centering formulas, the `text_align` vs `align` pitfall, the font pipeline, the native-action contract, plus the full motor case study.

## The examples

Each example directory holds **sources only** (`*.uixml`, `make_*.py`); every generated artifact — the `.eez-project`, the Studio C export, side-cars and the simulator — lands in `examples/<name>/build/` (git-ignored; rebuild with `tools/build_sim.py` or `tools/ci-check.py`).

| Example | Shows |
|---|---|
| `examples/motor` / `motor-en` | 3-screen motor controller, 13 bound variables, 12 native actions, CN/EN layout variants |
| `examples/glass` | Glassmorphism + staggered entrance animations (`anim` verb, `lv` style passthrough) |
| `examples/i18n` | `T"key"` labels, EN/ZH from one IR, `translations.yaml` |
| `examples/richdata` | Roller/table/chart/scale/calendar/keyboard/spinbox/tabview + `ui_ext.c` |

### The motor example

| Layer | Contents |
|---|---|
| Data down | 13 global variables → metric cards, gauges, LEDs, switches, clock |
| Navigation | 3 flow actions (`nav_overview/params/alarms` → changeScreen) |
| Input up | 12 native actions wired at 24 points (sliders, arcs, switches, dropdowns, ack buttons) |

Layout: manual coordinates everywhere (`x = center - w/2`), value labels in fixed-width boxes with centered text, every card wrapped in a panel. The mockup (`examples/motor/motor_ui.html`), the IR, and the generated project stay pixel-faithful — verified down to ±2 px.

Two language variants of the same UI: `examples/motor` (Chinese) and `examples/motor-en` (English). English text runs ~25% wider than CJK at the same font size, so the English variant widens the navbar (64 → 88 px) and re-plans every label column — a realistic demo of what localization means under a manual-coordinate layout.

## Notes & gotchas

- The bridge is localhost-only by design.
- Corporate proxies/VPNs: both servers keep their loopback calls off the system proxy (Python pins `trust_env=False`; the Node server scrubs `HTTP_PROXY`-style env vars at startup) — a system proxy can otherwise add ~1.7 s per call and stall progress notifications.
- Requires the patched Studio plus either Node.js 18+ (for `mcp-server.mjs`, recommended) or Python 3.10+ with `mcp`/`httpx` (for `eez_mcp_server.py`). The two MCP implementations are behaviorally aligned (47 tools, RFC 6902/7396 patching, live resources, progress heartbeats) and tested on Windows.
- Fonts in `fonts/` are redistributable subsets (see `fonts/*.meta.json` for sources) — regenerable via `font_tool.py`. The `demo_*` fonts used by the examples are free substitutes so this repo stays redistributable; goldens are captured against them. CJK glyphs in the demo fonts are a subset of **Noto Sans SC (SIL OFL 1.1)**; icon glyphs come from **Font Awesome Free** (OFL/CC BY 4.0) — see `font/fontawesome/LICENSE-fontawesome.txt`.

## License

**GPL-3.0** — in the spirit of the EEZ Studio ecosystem this builds on. Generated artifacts (`.eez-project`, `action.h`) are your own work and not covered by the GPL. Fonts keep their upstream licenses (OFL / CC-BY 4.0).

## Acknowledgments

- [eez-open/studio](https://github.com/eez-open/studio) — EEZ Studio, the foundation everything here drives
- [LVGL](https://lvgl.io/) and [lv_font_conv](https://github.com/lvgl/lv_font_conv)
- [Source Han Sans](https://github.com/adobe-fonts/source-han-sans) & [FontAwesome](https://fontawesome.com)
