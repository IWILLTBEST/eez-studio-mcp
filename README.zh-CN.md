# eez-studio-mcp

**把 EEZ Studio 变成 AI 可操控的 LVGL 界面编辑器。**

MCP（Model Context Protocol）服务器 + AI 技能 + IR 编译器：让 Claude、Cursor、ZCode、DSH 或任何 MCP 客户端直接读写 EEZ Studio 里的 LVGL 工程——逐部件、逐样式，带截图自查、实时检查、wasm 模拟器，连点击注入都有。

[English](README.md) · [带桥的 EEZ Studio](https://github.com/IWILLTBEST/studio)（必需运行时）

![架构](docs/img/architecture.svg)

## 截图

下面三屏全部由 IR 编译器生成（`examples/motor`），再通过 MCP 的 `screenshot` 工具抓取——零手工修饰：

| 总览 | 参数 | 告警 |
|:---:|:---:|:---:|
| ![总览](docs/img/motor-overview.png) | ![参数](docs/img/motor-params.png) | ![告警](docs/img/motor-alarms.png) |

**单部件特写**（`screenshot_object` 只返回一个部件——AI 自查利器）：

<p><img src="docs/img/widget-closeup.png" width="220" alt="部件特写"></p>

## AI 能做什么？

| 能力域 | 工具 | 亮点 |
|---|---|---|
| **部件级编辑** | `list_objects` `get_object` `update_object` `create_widget` `delete_object` `create_screen` `undo` `redo` | 按路径**或稳定 objID** 寻址；每次编辑都是可撤销的命令，自动保存 |
| **样式与主题** | `list_styles` `update_style` `create_style` `delete_style` `set_theme_color` `add_color` `set_preview_theme` | 直接改 `definition[part][state]` 属性；切主题预览再截图验证配色 |
| **视觉闭环** | `screenshot` `screenshot_object` `goto_object` `get_selection` | 整页截图、单部件特写、定位对象、读**用户当前选中**（人在回路） |
| **诊断** | `read_output` `check` `build_project` | 读 Checks/Output 面板；跑完整检查或真实构建（生成 C 源码） |
| **运行时与输入** | `debug_start/stop/control/status` `read_variable` `write_variable` `send_input` | 操控 LVGL wasm 模拟器：暂停/单步、读写变量、**注入点击与滑动**（实测点击导航按钮完成切页） |
| **工程文件** | `read_project_json` `write_project_json` `patch_project_json` | RFC 7396 深合并 + RFC 6902 JSON-Patch，安全的批量结构修改 |
| **多工程** | `list_projects` `select_project` `open_project` | tab 级工程切换，含死 tab 复活 |
| **资产** | `list_assets` `add_font` `add_image` | TTF→LVGL 字体（内建 lv_font_conv 管线，支持区间+中文字符）；图片导入自动落位 |
| **IR 流水线** | `read_ir` `write_ir` `compile` `reload` `navigate` `ping` | 最初的"IR 生成界面"闭环 |

协议层增强：**活资源**（`eez://checks`、`eez://debug`、`eez://state`）内容变化约 0.4 秒推送；长操作（`check`、`build_project`、`debug_start`、`add_font`…）支持**进度通知**。

## IR 编译器与固件接口

`ir2eez.py` 把声明式 JSON IR 编译成 `.eez-project`；IR 里声明了 native 动作时，同时生成固件移植接口 **`action.h`**：

```c
// action.h — 自动生成
void on_speed(int32_t value);   // 滑条/弧：当前值
void on_fwd(int32_t value);     // 开关：0/1
void on_poles(int32_t value);   // 下拉：选项索引
void ack_alarm(void);           // 点击类：无参
```

交互是双向的：**变量下行**——固件改变量，所有绑定部件每 tick 自动刷新；**动作上行**——用户拖滑条，你的 C 回调被调用。实现这些回调、include 头文件，移植就完成了。工具的**产物归你**，不受 GPL 约束（同 GCC 编译输出）。

```bash
git clone https://github.com/IWILLTBEST/eez-studio-mcp && cd eez-studio-mcp
pip install mcp httpx
python ir2eez.py examples/motor/motor.ir.json -o motor-demo.eez-project
# → motor-demo.eez-project + action.h（12 个 native 动作）
```

## 安装

1. **带桥的 EEZ Studio**——克隆并运行改造版（GPL-3.0，基于 [eez-open/studio](https://github.com/eez-open/studio) v0.30.0）：

   ```bash
   git clone https://github.com/IWILLTBEST/studio
   cd studio && npm install && npm start
   ```

   桥监听 `127.0.0.1:17620`，在 Studio 里打开（或新建）一个工程。

2. **把 MCP 服务器注册进你的客户端**（示例见 `claude_desktop_config.example.json`）。MCP 层是**双实现、可互换**——45 个工具、资源、提示词、进度通知两边完全一致：

   - **Node.js（推荐）**——`mcp-server.mjs`，**零 npm 依赖**：stdio 上的 JSON-RPC 纯手写（换行分隔 JSON），只要 Node.js 18+：

     ```json
     {
       "mcpServers": {
         "eez-studio": { "command": "node", "args": ["<仓库路径>/mcp-server.mjs"] }
       }
     }
     ```

   - **Python**——`eez_mcp_server.py`，基于官方 `mcp` SDK（Python 3.10+，`pip install mcp httpx`）：

     ```json
     {
       "mcpServers": {
         "eez-studio": { "command": "python", "args": ["<仓库路径>/eez_mcp_server.py"] }
       }
     }
     ```

3. **开始对话**——比如"列出所有屏幕"、"把导航栏指示灯改成绿色并截图给我看"、"拖一下速度滑条，告诉我模拟器现在在哪个页面"。

4. **（可选）安装技能**——把 `SKILL.md` 拷进你 agent 的技能目录。里面沉淀了全部工程经验：手动坐标居中公式、`text_align` 与 `align` 的大坑、字体流水线、native 动作约定，以及 motor 完整案例范式。

## motor 示例

| 层 | 内容 |
|---|---|
| 数据下行 | 13 个全局变量 → 指标卡、仪表、LED、开关、时钟 |
| 页面导航 | 3 个 flow action（`nav_overview/params/alarms` → changeScreen） |
| 输入上行 | 12 个 native 动作、24 处接线（滑条/弧/开关/下拉/告警确认按钮） |

布局全手动坐标（`x = 参考中心 - w/2`），数值 label 固定宽盒子 + 文字居中，每个卡片 panel 包裹。效果图（`motor_ui.html`）、IR、生成工程三者像素级一致——实测误差 ±2px。

## 注意事项

- 桥只监听本机回环，刻意如此。
- 公司代理/VPN：两个实现都不让回环请求走系统代理（Python 固定 `trust_env=False`；Node 版启动时清掉 `HTTP_PROXY` 类环境变量）——否则系统代理会让每次调用慢 ~1.7 秒，还会饿死进度通知。
- 需要带桥的 Studio，外加 Node.js 18+（跑 `mcp-server.mjs`，推荐）或 Python 3.10+ 配 `mcp`/`httpx`（跑 `eez_mcp_server.py`）。两个 MCP 实现行为对齐（45 个工具、RFC 6902/7396 补丁、活资源、进度心跳），Windows 实测。
- `fonts/` 里的字体是思源黑体（OFL）子集 + FontAwesome（OFL/CC-BY 4.0），可用 `font_tool.py` 重新生成。

## 许可

**GPL-3.0**——与它所依托的 EEZ Studio 生态保持一致。生成的产物（`.eez-project`、`action.h`）是你自己的作品，不受 GPL 约束；字体保留各自上游许可（OFL / CC-BY 4.0）。

## 致谢

- [eez-open/studio](https://github.com/eez-open/studio) —— EEZ Studio，一切的基座
- [LVGL](https://lvgl.io/) 与 [lv_font_conv](https://github.com/lvgl/lv_font_conv)
- [思源黑体](https://github.com/adobe-fonts/source-han-sans) 与 [FontAwesome](https://fontawesome.com)
