---
name: ir2eez
description: Generate EEZ Studio LVGL .eez-project files from a declarative IR JSON. Use when the user asks to create embedded GUI screens/pages/HMI for EEZ Studio or LVGL, design device UI (dashboards, navigation bars, settings pages), bind variables to widgets, generate EEZ Flow actions (button click → change screen / set variable / selection highlight / partial view switching), or convert an HTML mockup into an EEZ project. Also covers the MCP server for remote AI control of EEZ Studio.
---

# ir2eez — AI 生成 EEZ Studio LVGL 工程

把界面需求写成 IR JSON，编译成 .eez-project，用户在 EEZ Studio 里打开即可可视化编辑。

## 接入方式

| 方式 | 适用 | 说明 |
|------|------|------|
| **MCP Server**（主路线） | Claude Desktop / Cursor / ZCode 等任何 MCP 客户端 | `eez_mcp_server.py`，45 工具 + 6 资源（3 活资源可订阅）+ 进度通知 |
| **DeepSeek Harness** | dsh Web UI / Studio Toolbar AI 按钮 | 经同一 MCP/HTTP 桥 |

内置 LLM agent 面板已移除（双轨维护成本高，Studio 只提供桥）。

三种方式共用同一个 HTTP 桥（`127.0.0.1:17620`），工具实现在 `packages/ai-agent/tools.ts`。

## 工具链

- 工具目录：本仓库根目录（ir2eez.py 所在目录）
- 编译器：`ir2eez.py`（内置 IR 校验 + 产物自检 + 字形覆盖校验，**退出码非 0 = 失败，文件未写盘**）
- 预览：`ir_preview.py`（编译产物坐标 → SVG）
- 格式文档：`IR_SCHEMA.md`（**写 IR 前必读**）
- MCP Server：`eez_mcp_server.py`（`pip install mcp httpx`）
- 字体工具：`font_tool.py`（compile/scan-html/list/show）
- Python：`python`
- EEZ Studio 源码：https://github.com/IWILLTBEST/studio （EEZ Studio v0.30.0 fork + ai-agent 桥，GPL-3.0）
- 例子：`motor.ir.json`（电机控制器 3 屏，含每屏独立导航）/ `sg8.ir.json` + `make_sg8.py`

## 工作流程

1. **读格式文档**：Read `IR_SCHEMA.md` + 一个例子
2. **写 IR**（或生成脚本）→ 3. **编译**（校验退出码）→ 4. **处理报错** → 5. **预览自查** → 6. **交付**

```
D:/.../python.exe ir2eez.py <输入.ir.json> -o <输出.eez-project>; echo exit=$?
```

## 必守规则

| 规则 | 原因 |
|------|------|
| 坐标一律**相对父容器**；user widget 的 children 坐标相对 widget 自身 | LVGL 语义 |
| **卡片/指标区/设置组/仪表区 → 每个必须用 panel 包裹**（底色 #1C2333 + radius 8）| 没有面板包裹视觉散乱 |
| **动态文本 label 必须有语义化 `id`**；编译器自动加类型前缀（label_/button_/panel_…），flow target 写简短 id 自动映射 | 固件按 identifier 找对象 |
| **普通页面标识符全工程唯一**；纯装饰组件不要 id | EEZ 全局查重 |
| **标识符必须全小写** | EEZ 构建按 UnderscoreLowerCase 存储，大写 indexOf 失配 |
| **标识符作用域**：user widget 页组件只在本页 flow 可见；顶层 action 只能引用普通页的组件 id | 违反报 not found |
| **需要跨屏同步状态的组件（导航栏高亮等）不要做成 user widget** — 每屏用独立 panel，指示条固定在正确位置 | user widget 实例状态每屏独立，改一处不同步到其他实例 |
| user widget 定义用显式 x/y 绝对坐标，不用 flex；不加 ScreenWidget/Panel 根 | 编译器已处理 |
| 嵌套用 `container`/`panel`（编译器自动清零 padding/border） | LVGL 主题默认给 lv_obj 加 16-24px 内边距 |
| LED 只绑 `brightness`(0-255)；已自动 shadow_width:0 | EEZ 限制 |
| dropdown 用显式 `h`（26/28）；选项用英文；字体图标清单固定带 0xF077-0xF078 | 展开列表用 montserrat 中文变方块；箭头缺字形 |
| 圆角：按钮 6；卡片 8；小条目 4 | 对照设计稿 |
| 分割线用 `{"type":"line"}` | ppa32 同款 |
| 字体与效果图同款（msyh.ttc 拆 ttf）；bpp=8；**字符集覆盖最终 IR 全部文本**（不是只扫 mockup HTML） | 手写文案不在 HTML 扫描集 → 方块 |
| label 高度/宽度编译器自动兜底（行高×行数 / 最长行估算） | 防裁字/换行 |
| **手动坐标对齐——居中公式**：面板内元素居中用 `x = (父宽 - w) / 2`；叠在圆弧/图形上的元素用 `x = 参考中心 - w/2`（如 306 宽面板的 90 宽标题 x=108；140 圆弧中心 153 → 80 宽数值 x=113） | flex 不可靠也不直观，全部显式坐标 |
| **文字居中必须 `align: "center"`（IR 字段）→ 编译为 `text_align`**；绝不能直接写 localStyles 的 `align` —— 那是**对象对齐**（LV_ALIGN_CENTER 会把对象挪到父中心、left/top 变成中心偏移，加载后布局整体错位） | text_align 只对齐文字行，对象位置不动 |
| 变量绑定的数值 label：盒子固定宽 + `align: "center"`，运行时数字宽度变化仍保持居中（默认左对齐会偏左） | 数字实际渲染宽 ≠ 估算宽 |
| 修改走 IR 重新编译，覆盖 EEZ 手工编辑 | 单向生成 |

## 吸收的外部实战经验（TrailCurrent eezstudio skill）

来源：[trailcurrentoss/TrailCurrentClaudeSkills · eezstudio/SKILL.md](https://github.com/trailcurrentoss/TrailCurrentClaudeSkills/blob/main/eezstudio/SKILL.md)（MIT；Trap 编号即其原文小节）。该 skill 走"一次性脚本离线改 .eez-project + 人眼在 EEZ canvas 验证"路线，以下坑位已收编进本工具链——多数由编译器/MCP 闭环机械解决，其余为手改 JSON/patch_project_json 时的必查项。

1. **开关/复选框必须带 `CHECKABLE`**（Trap 21）：缺它时有按压波纹但状态永不翻转、VALUE_CHANGED 不触发。EEZ 老版默认 flags（`oldDefaultFlags`：CLICKABLE|PRESS_LOCK...）和手写生成器最容易漏；当前 EEZ 新建默认已含。判别特征：C 里 `lv_switch_create` 动态建的能翻、EEZ/IR 生成的不能翻 = 中招。`ir2eez.py` 的 switch/checkbox 构建器已显式写入（checkbox 曾漏，2026-08 修复）。
2. **运行时自排版控件要样式钉扎**（Trap 13）：`lv_keyboard / lv_list / lv_buttonmatrix / lv_dropdown / lv_roller / lv_tabview` 内部布局会无视 JSON 的 left/top/width/height（键盘默认锚底居中 + 半屏高），**canvas 同样复现**（canvas 模拟 LVGL 行为）。正确修法不是 C 侧 `lv_obj_set_pos`（canvas 不跑 C），而是 localStyles MAIN.DEFAULT 写 `align: TOP_LEFT` + `min_width/max_width/min_height/max_height = 授权宽高`——canvas 与设备同时生效。IR 的 dropdown 已强制显式 h；IR 未来增加 keyboard/list/tabview 时必须走钉扎模式。
3. **装饰性子组件会吞点击**（Trap 15/16）：LVGL 命中测试停在最顶层可点后代，事件不冒泡——按钮内的 label、可点行内的图标/文字会把点击截走（症状："按钮要点角落才中"）。规则：纯展示子件 `clickableFlag: false`；整行点击目标放行容器**最后一个子节点**（z 序最高）且 `bg_opa: 0`（透明背景；别配色，换主题就穿帮）。
4. **等宽数值盒：ceil 宽度 + longMode CLIP**（Trap 20）：adv_w/16 有亚像素（如 10.8125px/字），盒宽差 0.25px 就会 WRAP 到第二行被裁（显示 "13." 丢 "4"，长得像字体 bug）。规则：`width = ceil(len × 每字宽) + 1`，数值 label 用 `longMode: "CLIP"`。ir2eez 宽度兜底已含 +16 padding，多数场景已防住；固定宽数值盒可再显式 CLIP 双保险。
5. **颜色集中定义，禁散落裸 hex**（其"non-negotiable rule"）：换肤/改主题时散落的 hex 全是穿帮点。其纪律：新色必须先提案命名 token（含亮/暗主题各自色值 + 是否 theme-invariant）再引用。IR 等价物：颜色集中在 themes 段，MCP `set_theme_color` 一处改全局生效；手改 JSON 时同样别在 localStyles 里新增裸 hex。
6. **canvas 必须诚实代表设备**（Trap 14）：运行时由固件填充内容的 label，IR 里 text 留空或 "-"，别写 "Network 1..8" 假占位——用户在 canvas 看到的应与设备未填充时一致，canvas 就是设备预览。

## 交互效果模式

- **选中高亮**：`states: {"CHECKED": {"bg","color"}}` + `objAddState/objClearState` 动作
- **局部视图切换**：两个 panel，一个 `hidden: true`，`objAddFlag/objClearFlag`（HIDDEN）互斥
- **滚动区域**：视口 panel + `scrollable: true`（自动 SCROLLABLE + CLICKABLE），子内容超出即滚
- **切屏动画**：changeScreen 的 `fade`（MOVE_LEFT/FADE_IN 等）+ `speed`
- **对象位置移动**：`objSetY` 动作（如移动指示条）——但在 user widget 内不跨屏同步
- **组件位置**：`objSetX/objSetY` 动作（target + x/y 参数）
- **属性动画**：`{"op":"lvgl","action":"anim","target":id,"prop":"x|y|w|h|opacity|img_zoom|img_angle","from":..,"to":..,"time":ms,"ease":"ease_out","repeat":N}`——repeat 0=一次/N=重复N次/-1=无限循环（呼吸灯用 opacity+repeat:-1）；编译到 animX 等 7 个动作 + `lv_anim_set_repeat_count`。repeat 仅重播非往返；模拟器 wasm 重建前忽略 repeat（播一次），固件导出完整生效。入场动画=anim ease_out，退场=animX 滑出，强调=animOpacity 脉冲
- **native 动作（固件接口）**：IR 里声明 `{"name": "on_xxx"}` 不带 steps → 编译为 `implementationType: "native"`；滑条/弧/开关/下拉绑 `value_changed` 事件、按钮绑 `clicked`。编译同时产出 `action.h`（与输出工程同目录）：值变化类 `void on_xxx(int32_t value)`（滑条/弧=当前值，开关=0/1，下拉=选项索引），点击类 `void xxx(void)`。固件 include 后实现即完成移植

## MCP Server 接入

```json
// Claude Desktop → %APPDATA%/Claude/claude_desktop_config.json
{
  "mcpServers": {
    "eez-studio": {
      "command": "python",
      "args": ["<repo>/eez_mcp_server.py"]
    }
  }
}
```

45 个工具，能力域：IR 流水线（read/write_ir、compile、reload、navigate、screenshot）/ 部件级编辑（list/get/update/delete_object、create_widget/screen、undo/redo、goto_object、get_selection，路径或 objID 寻址）/ 样式主题（update_style、set_theme_color、set_preview_theme…）/ 工程文件（read/write/patch_project_json）/ 多工程（list/select/open_project）/ 诊断（read_output、check、build_project）/ 调试（debug_start/stop/control/status、read/write_variable、send_input 点击滑动注入）/ 资产（add_font、add_image）/ screenshot_object 部件特写 / create_project 新建工程。资源：IR/规范/技能文档 + 活资源 eez://checks、eez://debug、eez://state（可订阅，变化即推送）。长操作（check/build/debug_start/add_font…）支持进度通知。

## 字体流水线

```
# 扫描字符（HTML + IR JSON + 生成脚本合并后再编译！）
cat mockup.html xxx.ir.json make.py > _symsrc.html
font_tool.py compile --src fonts/msyh.ttf --name <名>_<字号> --size <字号> --bpp 8 \
  --range 32-127 --symbols-from-html _symsrc.html \
  --icon-font "font/fontawesome-free-6.7.2-web/webfonts/fa-solid-900.ttf:<码点>" \
  --icon-font "font/fontawesome-free-6.7.2-web/webfonts/fa-brands-400.ttf:0xF293-0xF294"
```

## 与用户的协作约定

- EEZ Studio 里开着项目时**不要重新编译**（EEZ 缓存旧版，一保存就覆盖）
- 手工改动想保留 → 描述改动 → 同步进 IR → 再编译（IR 是唯一源头）
- IR 修改策略：小改动用内置 edit（手术式），大改动用 eez_write_ir（全量重写）
- 交付后说明验证点（哪些效果要上设备看）

## 变量运行时语义

- `native: true` 变量：固件实现 `get_var_xxx()/set_var_xxx()`
- 绑定是每 tick 轮询：主循环调 `ui_tick()`，改变量后所有绑定处自动更新
- 无 steps 的 action = native 空壳由固件实现

## 成熟案例：motor 电机控制器（照此模式写新工程）

效果图 `motor_ui.html` → IR `motor.ir.json` → 编译 `out_motor.eez-project` + `action.h`。1024×600 三屏（overview/params/alarms）+ StatusBar user widget。

**三层交互架构**（移植标准范式）：
- **数据下行**：13 个全局变量 bind 到部件（指标卡 label / 仪表弧 value / LED brightness / 开关 checkedState / 时钟 text）——固件改变量，UI 每 tick 自动刷新
- **页面导航**：3 个 flow action（nav_overview/params/alarms → changeScreen），导航栏按钮 clicked 绑定；每屏独立导航栏 + 指示条固定位置（跨屏高亮不走变量）
- **输入上行**：12 个 native action（on_speed/on_torque/on_motor_temp/on_bus_volt/on_out_curr/on_fwd/on_eco/on_poles/on_ctrl_mode/on_can_baud/on_protocol + ack_alarm），24 处 value_changed/clicked 事件接线；固件 include `action.h` 实现回调即移植完成

**命名约定**：变量=名词（bus_volt）；值变化动作=`on_<变量名>`（fwd_on→on_fwd 去掉 _on）；点击类=动词短语（ack_alarm）；动态 label 必须有语义 id（label_val_speed）。

**布局范式**：页面 = 顶部 StatusBar(user widget) + 左侧窄导航栏(64px) + 右侧内容区（指标卡行 / 仪表行 / 控制行，每块 panel 包裹 #151B28+radius8）；手动坐标居中公式 `x = 参考中心 - w/2`；数值 label 盒子固定宽 + `align:"center"`。
