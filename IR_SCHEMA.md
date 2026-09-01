# html2eez IR 格式（v1）

IR 是 LVGL 原生的界面描述 JSON，`ir2eez.py` 把它编译成 EEZ Studio `.eez-project`。
AI / 人只写语义：**不写 objID、不写连线、不写节点坐标**——编译器负责生成并校验。

```
python ir2eez.py <input.ir.json> -o <output.eez-project>
```

## 文件分离（Qt 式三平面，可选）

单文件依旧合法；大工程可按"改文件的人是谁"拆分，清单 `<include>` 缝合（相对路径、环检测、screen/widget/text 重名报错、`<project>` 只许一份）：

```text
examples/i18n/
├── project.uixml      ← 清单：<include> logic / strings / screens（编译入口）
├── logic.uixml        ← 逻辑平面：<project> + <var>（固件工程师的接口，生成 action.h）
├── strings.uixml      ← 文案平面：翻译者只碰这里
└── screens/main.uixml ← UI 平面：界面 + UI 内部编排（切屏/动画），设计者/AI 迭代
```

拆分发生在 uixml 解析层（合并成单一 IR），编译器零改动；i18n 示例即拆分形态，金标准 0.0% 自证无损。

## 顶层结构

```jsonc
{
  "project":   { "name", "width": 1024, "height": 600, "font": "myfont_32" },
  "variables": [ { "name", "type", "default", "native": true } ],
  "strings":   { "default": "en", "texts": { "key": { "en": "...", "zh": "..." } } },  // i18n，可选
  "widgets":   { "NavBar": { ...widget 定义，同普通 widget 节点... } },
  "screens":   [ { "name", "children": [...] } ],
  "actions":   [ { "name", "steps": [...] } ]   // 无 steps = native 动作（固件在 action.h 实现回调）
}
```

## i18n 字符串表（tr 标签）

label 节点用 `"tr": "key"` 替代 `text`：编译成 `T"key"` 表达式（上游 eez-open/studio#1045），运行时经 EEZ Flow 的 `Flow.translate` 钩子解析——固件把钩子接到 `lv_i18n_get_text`，模拟器只显示 key 本身。

- **设计时画布**渲染 previewValue = `strings.default` 语言的译文（我们的 previewValue 机制，不是上游 eval）；**切换预览语言 = 改 `strings.default` 重编译**，工程里的 key/字节码不变
- 编译器同时落 `*.translations.yaml`（lv_i18n 源格式，`lv_i18n compile` 生成 C），包含全部 key × 全部语言；比从导出 C 里 `lv_i18n extract` 提取更直接
- `tr` 引用了 `strings.texts` 里不存在的 key → 报错（preview 回退显示 key）；未引用的 key → 警告
- 字体字形检查按**所有语言**的译文校验（demo 字体没中文 → 用 cn_24 这类含 CJK 的字体）
- 示例见 examples/i18n（make_i18n.py）：en/zh 双语，改一行 default 切换画布语言

```jsonc
{ "type": "label", "tr": "title", "x": 24, "y": 20, "w": 360, "h": 34, "font": "cn_24" }
```

## widget 节点（树）

| 字段 | 说明 |
|---|---|
| `type` | `container` `button` `label` `image` `dropdown` `bar` `slider` `textarea` `checkbox` `switch` `arc` `spinner` `led` `roller` `table` `chart` `scale` `calendar` `keyboard` `spinbox` `tabview` |
| `widget` | user widget 实例：`{"widget": "NavBar", "x": 0, "y": 0}`（此时不用 type，不能带 children） |
| `id` | → EEZ identifier，供 C 代码 / LVGL action 引用 |
| `x` `y` `w` `h` | 显式坐标尺寸；缺省时按布局规则推导。**坐标一律相对父容器**（LVGL 语义，无全局坐标）：user widget 的 children 坐标相对 widget 自身（如 NavBar 宽 800 时，右端 LED 的 x 按 800 算，不按屏幕宽算）；实例换位置/换屏幕，内容整体跟随 |
| `text` | label/button/checkbox/textarea 的文字 |
| `bind` | 绑定全局变量名（按类型映射属性，见下表）；未声明的变量自动声明 |
| `align` | label 专属：`left`/`center`/`right`/`auto` 文字对齐（编译为 text_align；数值/单位等已居中摆放的 label 用 `center`）。注意与容器 flex 的 align 同名不同义 |
| `preview` | 绑定变量时 EEZ 画布上的预览值 |
| `font` | 字体名（fonts/catalog.json 中的名字，如 `myfont_32`、`cn_24`） |
| `color` / `bg` | `#RRGGBB`，自动规整（去 alpha） |
| `layout` | `row` / `col`（容器变 flex）+ `gap` `justify`(start/end/center/between/around/evenly) `align`(start/end/center)。**注意：user widget 定义里不要用 flex**（子元素位置会被卡住，实测），用显式 x/y 绝对坐标；flex 适合普通页面里的流式内容 |
| `events` | `{"clicked"/"value_changed": "action名"}`，键不区分大小写；滑条/弧/开关/下拉用 `value_changed`，按钮用 `clicked` |
| `children` | 仅 `container` 支持 |

### bind 按类型绑定的属性（LED 注意）

| widget | 绑定属性 | 变量类型 |
|---|---|---|
| label / textarea | text | string |
| bar / slider / arc | value | integer |
| **led** | **brightness (0-255)** | integer |
| switch / checkbox | checkedState | boolean |
| **roller** | **selected（可写）** | integer |

**LED 的 `color` 只能是字面量**（EEZ 限制）；用 brightness 表达状态：
固件里 `set_var_led_wifi(255)` 点亮 / `set_var_led_bt(80)` 变暗，
所有页面实例化的 NavBar 里的 LED 会随 tick 自动刷新（一处改，处处变）。

**设计时预览（design-time preview）**：EEZ 画布渲染的是 `previewValue`，不是表达式本身（表达式只在运行时求值）。编译器用变量默认值对 bind 表达式做安全求值写入 previewValue：裸变量 → 默认值；算术/字符串拼接可求则求；求不动（函数调用、未声明变量）原样回退。节点上的 `preview` 字段可显式覆盖。**截图比对前先确认变量 default 正确**，否则画布上显示的是变量名而不是数值。

### 富数据部件（roller / table / chart）

**Roller 完整编译**——选项、选中项双向绑定全进工程：

```jsonc
{ "type": "roller", "id": "mode", "bind": "mode_idx",
  "options": ["Auto", "Manual", "Service"],   // 或 "
" 分隔字符串
  "mode": "normal",                            // normal | infinite（无限循环滚动）
  "events": { "value_changed": "on_mode" } }   // 滚动选择触发
```

宽度按最长选项自动兜底；`bind` 绑 selected（integer，双向：变量变→`rollerSetSelected`，滚动→写变量）。

**Table / Chart 编译为裸 LVGL 对象**（EEZ 侧本来就没有结构属性——`lv_table_create`/`lv_chart_create` 即全部），IR 的结构参数做**校验 + 尺寸参考**，并生成 **`ui_ext.h` / `ui_ext.c`**（可编译，依赖 build 产出的 `screens.h` 的 `objects.<id>` 句柄）：

```jsonc
{ "type": "chart", "id": "bus", "kind": "line",     // line | scatter
  "min": 0, "max": 400, "points": 120,
  "series": [ { "name": "Ibus", "color": "#5EE6C4", "width": 2 } ] }

{ "type": "table", "id": "events",
  "cols": 3, "rows": 5, "header": ["Time", "Code", "Message"] }
```

固件接线（三行）：`ui_init()` 后调 `ui_ext_init()`——内部按常量配好 chart 类型/量程/点数/序列、table 行列并填表头；喂数据调 `chart_bus_push(series_idx, value)`（`lv_chart_set_next_value` 滚动模式）。画布上 chart/table 是空矩形（运行时才有内容）——视觉比对时这是预期。示例见 examples/richdata。

**其余四个全部完整编译**（examples/richdata 第二屏 controls）：

```jsonc
{ "type": "scale", "id": "rpm", "mode": "round_inner",   // horizontal_top/bottom, vertical_left/right, round_inner/outer
  "min": 0, "max": 3000, "angle": 270, "rotate": 135,    // angleRange + 起始角
  "ticks": 11, "major": 5, "labels": true,
  "sections": [ {"from": 2600, "to": 3000, "color": "#E5484D", "width": 8} ] }  // 分段着色

{ "type": "calendar", "id": "cal", "today": "2026-09-01", "header": "arrow", "chinese": false }

{ "type": "keyboard", "id": "kb", "textarea": "input", "mode": "number" }
                                 // ↑ textarea 的 IR 短 id；mode: text_lower/text_upper/special/number/user_1..4

{ "type": "spinbox", "id": "count", "bind": "pulse_count", "min": 0, "max": 9999,
  "digits": 4, "separator": 0, "step": 1, "rollover": false }
```

- **scale** 是 LVGL 9 的刻度盘（8.x 的 meter 在 9.x 工程被 EEZ 禁用，别用）——模式/量程/角度/刻度/标签/分段全进工程
- **calendar** 的 `today` 设当前日期，`header` 为 none/arrow/dropdown；运行时换日期用动作 `calendarSetTodayDate` 等。**高度必须 ≥240**（编译器自动兜底并警告）：跨 6 周的月份（31 天+周六开头）会撑爆过矮的日历，内部日期矩阵子对象自行滚动——表现为"有的月份滚、有的不滚"，父对象去滚动标志也管不住。**画布渲染注意**：日历头部行在重载间存在非确定渲染（疑似字体加载竞态，字形度量不同），含日历的屏金标准用 `--pct 0.5`
- **keyboard** 必须先给 textarea 一个 id 再引用；`mode: number` 出数字键盘
- **spinbox** 的 `bind` 绑 value（EEZ 标记 assignable），`digits` 位数、`rollover` 循环越界

**Tabview（EEZ 原生完整建模，tabs 即子组件）：**

```jsonc
{ "type": "tabview", "id": "cfg", "bind": "active_idx",   // selectedTab 双向（tabviewSetActiveTab）
  "position": "top", "barSize": 44,                        // top/bottom/left/right + 标签栏厚度
  "events": { "value_changed": "on_tab" },
  "tabs": [
    { "title": "Display", "children": [ ...普通 widget 树... ] },
    { "title": "Network", "children": [ ... ] } ] }
```

标签内容坐标相对 tab 内容区（自动扣掉标签栏占的一轴）。**MessageBox 不在此列**：它和 chart/table 同属 EEZ 零编辑件（空串创建+closed，见上游 issue #1050），等官方路线图。

## variables

- `type`: `integer` `float` `double` `boolean` `string`
- `default` 接受别名 `value` / `init`（字段写错不会静默变成 0）
- `native: true`（默认）→ 生成 `get_var_xxx()` / `set_var_xxx()` extern 声明，**变量本体在固件 C 里实现**
- string 的 default 直接写 `"Home"`（编译成 EEZ 表达式 `"\"Home\""`）
- 被 `bind` 引用但未声明的变量自动按上表推断声明

## actions（EEZ Flow 生成）

`steps` 是线性序列，编译成 `Start → 节点1 → 节点2 → …` 的 flow（自动布局）：

```jsonc
{ "name": "nav_home", "steps": [
    { "op": "lvgl", "action": "changeScreen", "screen": "home",
      "fade": "FADE_IN", "speed": 200, "delay": 0, "useStack": false },
    { "op": "lvgl", "action": "anim", "target": "alarm_row1", "prop": "x",
      "from": 10, "to": 500, "time": 500, "ease": "ease_out", "repeat": 2 },
    { "op": "set", "variable": "page_title", "value": "\"Home\"" },   // value 是 EEZ 表达式，字符串要带引号
    { "op": "delay", "ms": 200 },
    { "op": "call", "action": "另一个action名" }
] }
```

- 无 `steps` 的 action → `native` 空壳（固件 C 实现，如 `sys_reboot`）
- 事件引用了未定义的 action → 警告 + 自动生成 native 空壳（不报错）
- lvgl op 已实现：`changeScreen` / `anim` / `objSetY` / `objAddState` / `objClearState` / `objAddFlag` / `objClearFlag` / `labelSetText`

### 页面级 flow（trigger，屏幕与 user widget 均可）

不定义具名 action，把步骤直接挂到页内某部件的事件引脚上（编译成 `handlerType=flow` + 连线）：

```xml
<screen name="main">
  <button id="go" x="10" y="10" text="Go" />
  <trigger id="go" event="clicked">
    <set variable="speed_val" value="1500" />
    <delay ms="300" />
    <anim target="go" prop="y" from="10" to="200" time="500" />
  </trigger>
</screen>
```

IR 形态：`screen.flow = [{"when": {"id","event"}, "steps": [...]}]`（`event` 默认 `clicked`，`id` 可写简短 id）。

### 反向导入（.eez-project → uixml）

`python ir2eez.py project.eez-project -o project.uixml`（VS Code 命令 `UIXML: Import from .eez-project`）——把 EEZ Studio 里的手改回流成 uixml。写回前做自检：反编译结果重新编译必须与工程 canonical 相等，否则拒绝写入（防超纲内容静默丢失）；旧 uixml 备份为 `.bak`。反编译依赖两个伴生文件（编译时自动生成在同目录）：
- `*.ir_meta.json` — strings 默认语言、工程默认字体、富数据部件结构（table 列行表头 / chart 序列 / roller 选项；这些不进工程文件本体）
- `*.translations.yaml` — lv_i18n 译文表

### anim（属性动画）

| 字段 | 取值 | 说明 |
|---|---|---|
| `target` | 组件 id | 要动画的部件 |
| `prop` | `x` `y` `w` `h` `opacity` `img_zoom` `img_angle` | 动画属性（w/h 为 width/height 别名） |
| `from` / `to` | 整数 | 起止值（opacity 为 0-255） |
| `time` | ms，默认 400 | 单次时长 |
| `ease` | `linear` `ease_in` `ease_out` `ease_in_out` `overshoot` `bounce`，默认 `ease_in_out` | 缓动曲线 |
| `repeat` | 默认 0 | **0=播一次；N=重复 N 次（共 N+1 次）；-1=无限循环**（呼吸/常驻动效用 -1） |
| `playback` | 默认 false | **乒乓**：正向播完自动反向（同时长），映射 `lv_anim_set_playback_duration`；呼吸灯用 `repeat:-1 + playback:true` 得到平滑往返而非跳变 |
| `instant` | 默认 true | 立即应用起始值 |
| `relative` | 默认 false | from/to 相对当前值 |

repeat 是重播（每次从 from 重新开始）；playback 才是往返。模拟器 wasm 未重建前忽略 repeat/playback（播一次），**固件导出完整生效**（上游 PR eez-open/studio#1049：`lv_anim_set_repeat_count` + `lv_anim_set_playback_duration`）。

### lv（LVGL 样式透传）

节点上的 `"lv": {...}` 把任意 LVGL 样式属性直接写进 localStyles MAIN.DEFAULT——阴影、边框、渐变、文字透明度、内边距全目录：

```jsonc
{ "type": "panel", ..., "bg": "#1B2436", "bgOpa": 150, "radius": 16,
  "lv": { "border_width": 1, "border_color": "#9FB2D8", "border_opa": 40,
          "shadow_width": 26, "shadow_spread": 3, "shadow_color": "#000000", "shadow_opa": 130,
          "bg_grad_color": "#05080F", "bg_grad_dir": "VER" } }
```

玻璃拟态配方（见 examples/glass）：半透明底（bgOpa 130-170）+ 1px 低透明边框 + 大柔和阴影 + 圆角 14-18。属性名以 EEZ style-catalog / LVGL 文档为准。

## 结构规范（必须遵守）

1. **相同功能的组件组成 panel**：功能内聚的组件包进一个 panel（如：一个通道的 canvas + 通道号/电极/数值
   三个 label = 一个通道 panel），再由大 panel 包同级别的组（如 4 个通道 panel 组成波形区 panel）。
   层级即语义，方便 EEZ 里浏览和固件里定位
2. **动态文本 label 必须给 `id`**：凡运行时会被程序更新的 label（数值、状态、计数），都要有语义化
   identifier（如 `ch1_uv`、`file_page_info`、`ota_pct_label`），否则固件无法按名字找到它；
   已绑定变量的 label（bind）由变量驱动，可不加 id
3. 语义化命名：`canvas_ch1`、`ch_group_1`、`file_item_0`、`status_battery` 这类"功能_序号"风格

## 编译器内置的 EEZ 约束（AI 不用管）

- objID 全部自动生成并查重；flow 连线 source/target/input/output 自动生成并校验
- user widget 页必须显式 `isUsedAsUserWidget: true`，且**没有根 widget**：子组件直接平铺在页面
  components 里，坐标以 widget 本身为基准（Page.lvglCreate else 分支官方支持）。
  **不要加 ScreenWidget 根**（预览路径 Screen.tsx 无条件 createScreen，嵌在实例下渲染错位）
  **也不要加 Panel 等中间容器层**（同样引入原点偏移）。def 级 bg 由编译器自动转成
  第一个全尺寸背景容器兄弟（后画的组件在其上层）。缺 isUsedAsUserWidget 标志时
  EEZ 报 "not an user widget page" 和"尺寸与显示器不符"（两处检查都看这个标志）
- arc 六个角度字段 + preview 镜像字段必填，缺一 EEZ 报 "must be an integer"
- flex 必须 `layout:FLEX` + `flex_flow` 成对出现；flex 容器按子元素递归撑大
- 颜色规整为 6 位 `#RRGGBB`；dropdown 展开列表用 montserrat，中文选项警告
- dropdown 两个坑：① 高度用显式 `h`（如 26/28），不写 h 会 content 模式随字体行高撑到 30+；
  ② 右侧箭头是硬编码 LV_SYMBOL_DOWN（U+F078），**字体必须包含该字形**否则显示方块——
  编译字体时图标清单固定带上 0xF077-0xF078
- LED 自动写入 shadow_width:0（EEZ 主题默认 12，形成光晕）
- 容器（container）自动显式清零 padding 四边 + border_width：LVGL 默认主题给普通 lv_obj
  加 card 样式（pad_all≈16-24px + 边框 2px），而子组件坐标系从"内容区"（左上角+padding+边框）
  开始——每嵌套一层未清零的容器，子树整体偏移 ~18-26px。清零后嵌套安全
- LED color 仅字面量；checkbox 必带 text/textType/useStaticText + content 自适应

## 已知边界

- **标识符作用域（易踩）**：user widget 页里的组件只在**本页 flow** 可见——组件内部交互
  （如导航按钮高亮联动）必须走页面级 flow（widget 定义里的 `"flow": [{"when": {"id","event"}, "steps"}]`，
  编译成 handlerType=flow + 事件引脚连线）；顶层 action 只能引用**普通页面**上的组件 id。
  违反时 EEZ 检查报 `"Object": "xxx" not found`
- flow 只有线性 steps，无分支/循环（if/loop 待加：IsTrue/Loop 组件 + 多引脚连线）
- image 引用的位图需在 EEZ 里手动导入（bitmaps 段未实现）
- user widget 实例暂不支持传参（EEZ 的 userPropertyValues 需要 flowSupport，工程已开启，IR 层面待加）
- ~~裸机固件完整导出待补模板~~ **已闭环（2026-09-01）**：种子工程现带官方完整 14 文件构建模板（`lvgl-build-files.json`，抽自 eez-open/eez-project-templates "LVGL with EEZ Flow-9.0"）——build 后 `screens.c` 生成 `create_screens()`（含每个部件的创建调用与 `objects.<id>` 命名句柄）、`flow_def.c` 产 assets+native 变量表、i18n 的 `T"key"` 清单也落 screens.c。注意构建产物（screens/actions/vars/structs/flow_def/images/fonts/styles/ui.*）落在工程同目录，别提交进仓库
