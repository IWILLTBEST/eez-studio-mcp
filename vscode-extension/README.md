# UIXML for EEZ Studio / LVGL

Language support + live preview + compile commands for **UIXML** — the XML source format that compiles into EEZ Studio LVGL projects (`ir2eez.py`). Own vocabulary (attributes = IR fields, `xmlns:lv` style passthrough); deliberately **not** the LVGL XML Specification (that license forbids third-party generators).

## Features

- **Syntax highlighting** for `.uixml` (elements, attributes, `on-*` events, `lv:*` style attrs, colors)
- **Validation + autocomplete** via `schemas/uixml.xsd` — install the [Red Hat XML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-xml) and associate the schema once:
  ```json
  "xml.fileAssociations": [{ "pattern": "**/*.uixml", "systemId": "./schemas/uixml.xsd" }]
  ```
- **UIXML: Preview** — side-by-side canvas render: compiles on save, drives EEZ Studio over its bridge (open → reload → navigate → screenshot, paint-stability waited), with a screen selector. EEZ Studio must be running with the bridge (any build with it, headless `xvfb` works too).
  - **Sketch** mode: instant SVG approximation (no toolchain needed), shows translated strings and variable default values
  - **Pixel** mode: golden-grade render through the real toolchain
- **UIXML: Compile** — `ir2eez.py <file>.uixml -o <file>.eez-project`
- **UIXML: Import from .eez-project** — reverse channel: pull EEZ Studio hand-edits back into `.uixml`. Self-checks (recompile must reproduce the project) and refuses out-of-subset edits instead of silently dropping them; keeps a `.bak` of the previous source. Compile also writes the `<base>.ir_meta.json` / `<base>.translations.yaml` side-cars the importer needs.
- **UIXML: Check** — runs EEZ Studio's project check (0 errors / 0 warnings expected)

## Syntax 语法说明

**Structure 结构** — single file or Qt-style split, stitched by `<include>` at parse time:
```xml
<?xml version="1.0" encoding="utf-8"?>
<ui>
  <include src="logic.uixml"/>    <!-- project + variables: firmware contract -->
  <include src="strings.uixml"/>  <!-- translations: translators own this -->
  <include src="screens/main.uixml"/> <!-- widgets: designers own this -->
</ui>
```
A split form keeps exactly one `<project>`; screens/widgets/text keys must not duplicate across includes. Single-file form (everything in one `<ui>`) is equally valid.

**Head elements 头部元素**
| Element | Meaning |
|---|---|
| `<project name width height background-color?>` | display resolution + background |
| `<var name type default/>` | flow variable, `bind` targets it |
| `<strings default="en"><text key><l lang="en">…` | i18n table, `tr` targets it |
| `<screen name background-color?>` | one screen per file when split |

**Widgets 部件** — 23 tags: `panel container label button image slider bar switch checkbox led arc scale chart table tabview calendar keyboard spinbox roller textarea spinner canvas line`. Attributes are IR fields with dashes: `x y w h color bg radius font border-width` …

- Text: `text="静态"` static, `tr="key"` translated, `bind="var"` bound (preview shows the default value)
- Children: `<state>` (selected-state styles), `<instance widget="Name">` (user widget reuse), `<series>` (chart), `<section>` (arc/scale ranges), `<tab>` (tabview), plain child elements for `options` (roller/dropdown, comma-separated)
- Events: `on-clicked="action_name"` — value is an `<action>` name (defined in a top `<action>` element)
- Page-level flow: steps wired straight to a widget event pin, no named action needed:
  ```xml
  <trigger id="go" event="clicked">
    <set variable="speed_val" value="1500"/>
    <delay ms="300"/>
    <anim target="go" prop="y" from="10" to="200" time="500"/>
  </trigger>
  ```
- LVGL passthrough: any `lv:xxx-attr` becomes a native style property untouched

Full reference: [IR_SCHEMA.md](https://github.com/IWILLTBEST/eez-studio-mcp/blob/main/IR_SCHEMA.md) in the repo.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `uixml.pythonPath` | `python` | interpreter for `ir2eez.py` (needs stdlib only) |
| `uixml.repoRoot` | *auto* | eez-studio-mcp repo root — auto-discovered by walking up from the `.uixml` file, then workspace folders; set only for unusual layouts |
| `uixml.bridgeUrl` | `http://127.0.0.1:17620` | EEZ Studio bridge |

## Install

```bash
code --install-extension uixml-preview-<version>.vsix
```

Or *Extensions → … → Install from VSIX*.
