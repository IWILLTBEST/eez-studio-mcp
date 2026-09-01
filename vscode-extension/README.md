# UIXML for EEZ Studio / LVGL

Language support + live preview + compile commands for **UIXML** — the XML source format that compiles into EEZ Studio LVGL projects (`ir2eez.py`). Own vocabulary (attributes = IR fields, `xmlns:lv` style passthrough); deliberately **not** the LVGL XML Specification (that license forbids third-party generators).

## Features

- **Syntax highlighting** for `.uixml` (elements, attributes, `on-*` events, `lv:*` style attrs, colors)
- **Validation + autocomplete** via `schemas/uixml.xsd` — install the [Red Hat XML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-xml) and associate the schema once:
  ```json
  "xml.fileAssociations": [{ "pattern": "**/*.uixml", "systemId": "./schemas/uixml.xsd" }]
  ```
- **UIXML: Preview** — side-by-side canvas render: compiles on save, drives EEZ Studio over its bridge (open → reload → navigate → screenshot, paint-stability waited), with a screen selector. EEZ Studio must be running with the bridge (any build with it, headless `xvfb` works too).
- **UIXML: Compile** — `ir2eez.py <file>.uixml -o <file>.eez-project`
- **UIXML: Check** — runs EEZ Studio's project check (0 errors / 0 warnings expected)

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `uixml.pythonPath` | `python` | interpreter for `ir2eez.py` (needs stdlib only) |
| `uixml.repoRoot` | *(extension's parent)* | eez-studio-mcp repo root (`ir2eez.py` / `uixml.py`) |
| `uixml.bridgeUrl` | `http://127.0.0.1:17620` | EEZ Studio bridge |

## Install

```bash
code --install-extension uixml-0.1.0.vsix
```

Or Package Developer Host: *Extensions → … → Install from VSIX*.
