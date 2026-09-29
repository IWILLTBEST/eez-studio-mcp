# eez-studio-mcp

An MCP (Model Context Protocol) server that lets Claude, Cursor, ZCode, DSH or any MCP client read and edit LVGL projects *inside* EEZ Studio — widget by widget, style by style — with screenshots, live checking, a wasm simulator, input injection and visual regression.

```
MCP client (Claude / Cursor / ZCode / ...)
        |  stdio / SSE
        v
eez_mcp_server.py / mcp-server.mjs      (this repo)
        |  HTTP (bridge on 127.0.0.1:17620)
        v
EEZ Studio  <->  studio-extension/ (Studio-side bridge extension)
```

> **2026-09 repo split**: the XML/UIXML toolchain, the IR compiler (`ir2eez`),
> the VS Code extension, the wasm simulator shells, examples, goldens and the
> font tooling moved to their own repo -> **[IWILLTBEST/eezml](https://github.com/IWILLTBEST/eezml)**.
> This repo now contains only the MCP line: the servers and the Studio bridge.

## What can the AI do?

| Area | Tools (selection) |
|---|---|
| Project & screens | open_project, navigate, reload_project, project overviews |
| Widget editing | read/write widget properties, styles, flags — the AI edits the project the user has open |
| Screenshots | screenshot — canvas PNG, returned to the model as an image block |
| Live checking | build -> check with errors/warnings surfaced to the model |
| Runtime debugging | debug_start (run/debug), send_input (click/swipe), read/write_variable, debug_control |
| Visual regression | visual_baseline / visual_check — golden screenshot per screen, pixel compare with AA tolerance |

### All 47 tools

| Area | Tools |
|---|---|
| IR pipeline | `read_ir`, `write_ir`, `compile`, `reload`, `navigate`, `screenshot` |
| Widget-level editing | `list_objects`, `get_object`, `update_object`, `delete_object`, `create_widget`, `create_screen`, `undo`, `redo`, `goto_object`, `get_selection` — addressing by path or objID |
| Styles & themes | `list_styles`, `update_style`, `create_style`, `delete_style`, `add_color`, `set_theme_color`, `set_preview_theme` |
| Project files | `read_project_json`, `write_project_json`, `patch_project_json` |
| Projects | `list_projects`, `select_project`, `open_project`, `create_project` |
| Assets | `list_assets`, `add_font`, `add_image` |
| Diagnostics | `read_output`, `check`, `build_project` |
| Runtime debugging | `debug_start`, `debug_stop`, `debug_control`, `debug_status`, `read_variable`, `write_variable`, `send_input` (click / swipe injection) |
| Close-ups | `screenshot_object` |
| Visual regression | `visual_baseline`, `visual_check` |
| Infra | `ping` |

Resources: project IR / schema / skill docs plus live resources `eez://checks`, `eez://debug`, `eez://state` (subscribable — pushed on change). Long operations (check / build / debug_start / add_font …) report progress.

> `visual_baseline` / `visual_check` shell out to `tools/visreg.py`, which moved to the [eezml](https://github.com/IWILLTBEST/eezml) repo in the 2026-09 split. The servers auto-detect a sibling `eezml/` checkout; alternatively point `EEZ_VISREG_SCRIPT` at it (python with PIL+numpy required, override via `EEZ_VISREG_PYTHON`).

### AI workflow manual

The step-by-step manual for AI-built projects — IR schema, layout rules, interaction patterns, font pipeline, visual-regression discipline — lives in [eezml/SKILL.md](https://github.com/IWILLTBEST/eezml/blob/main/SKILL.md).


## Screenshots

Everything below was generated from the [eezml](https://github.com/IWILLTBEST/eezml)
toolchain and captured through the MCP `screenshot` tool — the AI takes these
itself, no manual touch.

**Motor controller** (3 screens, English variant):

| overview | params | alarms |
|---|---|---|
| ![overview](docs/img/motor-en-overview.png) | ![params](docs/img/motor-en-params.png) | ![alarms](docs/img/motor-en-alarms.png) |

**Glassmorphism showcase** — translucent cards, shadows, gradient bg, staggered entrance animation:

![glass](docs/img/glass-dashboard.png)

**i18n, one source two languages** — switch `strings.default` and recompile:

| English | 中文 |
|---|---|
| ![en](docs/img/i18n-en.png) | ![zh](docs/img/i18n-zh.png) |

**Rich data demo** — roller, gauge, calendar, spinbox, keyboard, tabview:

| main | controls | settings |
|---|---|---|
| ![main](docs/img/richdata.png) | ![controls](docs/img/richdata-controls.png) | ![settings](docs/img/richdata-settings.png) |

## Setup

1. **Install the Studio bridge**: import `eez-studio-mcp-extension-0.2.0.eez-extension` into EEZ Studio (it starts the HTTP bridge on port 17620).
2. **Run the MCP server**:
   - Python: `pip install mcp httpx` then `python eez_mcp_server.py`
   - Node: `node mcp-server.mjs`
3. Point your MCP client at it (see `claude_desktop_config.example.json`).

## Layout

| Path | What |
|---|---|
| `eez_mcp_server.py` | Python MCP server |
| `mcp-server.mjs` | Node MCP server (same bridge protocol) |
| `studio-extension/` | EEZ Studio bridge extension source |
| `tests/` | e2e MCP client test |

## License

MIT.
