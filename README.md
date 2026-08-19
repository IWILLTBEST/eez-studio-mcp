# eez-studio-mcp

**Turn EEZ Studio into an AI-drivable LVGL UI editor.** An MCP (Model Context Protocol) server + AI skill that lets Claude / Cursor / ZCode / DSH or any MCP client read and edit LVGL projects inside EEZ Studio — widget by widget, style by style, with screenshots, live checking, a simulator and even input injection.

把 EEZ Studio 变成 AI 可操控的 LVGL 界面编辑器：MCP 服务器 + AI 技能，任何 MCP 客户端都能逐部件读写 EEZ Studio 里的 LVGL 工程——截图自查、实时检查、模拟器、连点击注入都有。

```
MCP 客户端 (Claude/Cursor/ZCode/DSH)
      │ MCP (stdio)
eez_mcp_server.py  ──45 个工具──
      │ HTTP 127.0.0.1:17620
EEZ Studio (patched fork, GPL-3.0)  ← 内置 ai-agent 桥
      │
LVGL 工程 (.eez-project / IR JSON)
```

## Highlights

- **Widget-level editing** — `list_objects` / `get_object` / `update_object` / `create_widget` / `delete_object`, addressed by path **or stable objID**, all undoable
- **Styles & themes** — create/update LVGL styles, theme colors, theme preview switching
- **Visual loop** — page screenshots, **per-widget close-up screenshots** (`screenshot_object`), pixel-accurate verification
- **Diagnostics** — read Checks/Output panels, run full `check` / `build_project` (generates C sources)
- **Runtime debugging** — start/stop the LVGL wasm simulator, pause/step, read/write variables, logs
- **Input injection** — `send_input` (click / swipe) drives the running simulator; page navigation verified end-to-end
- **Assets** — `add_font` (built-in lv_font_conv pipeline), `add_image`
- **Multi-project** — list / switch / open project tabs
- **Live resources** — subscribe to `eez://checks`, `eez://debug`, `eez://state`; get pushed on change. Progress notifications for long operations
- **IR compiler** — `ir2eez.py` compiles a declarative JSON IR into a `.eez-project`, and generates an **`action.h`** native-action contract for firmware porting

## Requirements

- Python 3.10+ with `pip install mcp httpx`
- **EEZ Studio with the ai-agent bridge** — use the patched fork: https://github.com/IWILLTBEST/studio (GPL-3.0, fork of [eez-open/studio](https://github.com/eez-open/studio) v0.30.0). Start it (`npm start`); the bridge listens on `127.0.0.1:17620`

## Quickstart

1. Start the patched EEZ Studio, open (or create) a project.
2. Add the MCP server to your client. Claude Desktop example (see `claude_desktop_config.example.json`):

```json
{
  "mcpServers": {
    "eez-studio": {
      "command": "python",
      "args": ["<repo>/eez_mcp_server.py"]
    }
  }
}
```

3. Talk to it: *"list the screens"*, *"make the navbar indicator green"*, *"add a label with the new font and screenshot it"*.

4. Install the skill (optional, for ZCode-style agents): copy `SKILL.md` into your skills directory. It encodes the accumulated rules: layout formulas, `text_align` vs `align` pitfalls, font pipeline, native-action contract, and a complete worked example.

## Example: motor controller UI

```bash
# compile the IR into a .eez-project + action.h (run from repo root)
python ir2eez.py examples/motor/motor.ir.json -o motor-demo.eez-project
```

- `examples/motor/motor_ui.html` — design mockup
- `examples/motor/motor.ir.json` — declarative IR: 3 screens, 13 bound variables, 3 navigation flow actions, 12 native actions
- `action.h` — generated firmware contract: `void on_speed(int32_t value);` … implement these and the port is done

The example fonts are regenerated from [Source Han Sans](https://github.com/adobe-fonts/source-han-sans) (OFL) subsets + FontAwesome icons — fully redistributable. Regenerate/tune via `font_tool.py`.

## Tool map (45)

| Domain | Tools |
|---|---|
| IR pipeline | read_ir, write_ir, compile, reload, navigate, screenshot, ping |
| Objects | list_objects, get_object, update_object, create_widget, delete_object, create_screen, undo, redo, goto_object, get_selection |
| Styles/themes | list_styles, update_style, create_style, delete_style, set_theme_color, add_color, set_preview_theme |
| Project file | read_project_json, write_project_json, patch_project_json (RFC 7396 / 6902) |
| Multi-project | list_projects, select_project, open_project |
| Diagnostics | read_output, check, build_project |
| Debug & input | debug_start/stop/control/status, read_variable, write_variable, send_input |
| Assets & misc | list_assets, add_font, add_image, screenshot_object, create_project |

## License

- This repo: **MIT** (MCP server, IR compiler, skill, example). It talks to EEZ Studio over a local protocol and contains no EEZ Studio code.
- The patched EEZ Studio fork it requires is GPL-3.0 (as upstream).
- Fonts: Source Han Sans subsets + FontAwesome (OFL / CC BY 4.0) — see `font/fontawesome/`.

## Status

Works on Windows with EEZ Studio v0.30.0 fork. The MCP layer is pure Python and transport-agnostic; the bridge lives in the Studio fork. Contributions welcome.
