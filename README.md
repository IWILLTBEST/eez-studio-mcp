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
| Screenshots | screenshot (canvas), window_screenshot (whole window incl. panels) |
| Live checking | build -> check with errors/warnings surfaced to the model |
| Runtime debugging | debug_start (run/debug), send_input (click/swipe), read/write_variable, debug_control |
| Visual regression | visual_baseline / visual_check — golden screenshot per screen, pixel compare with AA tolerance |

See `eez_mcp_server.py` (Python) or `mcp-server.mjs` (Node) for the full tool list.

Example screens captured end-to-end with these tools live in the [eezml repo](https://github.com/IWILLTBEST/eezml#screenshots).

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
