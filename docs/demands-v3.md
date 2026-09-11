# Material Profile Worker — Phase 3:需求细化

## 原始需求(用户 2026-09-11)

7c5b0fe3 这个版本已经基本实现了我想要的功能:通过多维表格的按钮,将该行数据按一定规则整理为 JSON 文件并提交。

现在,我要把需求细化,形成一个比较成熟的 preset 提交系统。这一版主要有以下几点需要修改:

- 多维表格的 schema 严格按照我现在改的这一版来,你需要先读这一版表格的内容,然后修改 ensure_schema
- 提交参数需要分为必填和可选
- 解析上传 json 字段的逻辑要改成只找我们关心的,找不到就跳过,把找到的反写进表格,提交还是由用户自己点

---

> 条目编号 `V3-P<n>`,格式仿 [demands-v2.md](demands-v2.md):现状 → 期望行为 → 验收标准 → 状态。
> 状态取值:已记录 / 已规划 / 实施中 / 待验收 / 已验收 / 不做。
> 硬约束延续:不引入数据库;Git 为唯一真相源;不依赖飞书 Automation/按钮保证正确性。

## 0. 已确认决策(2026-09-11 两轮沟通)

| # | 事项 | 结论 |
|---|---|---|
| 1 | 行身份 | `PI Code × 打印机型号 × 切片软件` 三元组;一行 = 一个身份,永久不新增行 |
| 2 | 仓库分工 | worker 只写 **`preset-db`**(基础数据);构建产物**不进任何 repo,只发布到 Pages** |
| 3 | 路径 | `preset/<PI Code>/<brand>/<model>/<slicer>/<PI Code>@<brand> <model>.json` —— **所有切片器统一 `.json`** |
| 4 | 品牌/机型来源 | 表格 `打印机型号` 按**第一个空格**切分:首词 = 品牌段,其余原样 = 机型段。表格选项已标准化,worker **不带映射表**;旧目录 `Creality/K2Pro` 是当年 typo,以表里 `Creality K2 Pro` 为准(旧目录改名非 worker 范围) |
| 5 | 必填 | `PI Code`、`打印机型号`、`切片软件`、`调参方法版本`、**`继承预设`** + 核心参数:喷嘴温度、热床温度、流量比例、最大体积流速、冷却四项(冷却开启层时/最大风扇速度/最小风扇速度/降速层时) |
| 6 | 可选 | `线材密度`、`玻璃化温度`、`回抽距离`、`压力提前` —— 为空则该键不出现,由 `inherits` 继承父配置 |
| 7 | 热床温度 → 键 | `textured_plate_temp`(只用这一个键) |
| 8 | 值形态 | **标量**(整数不写 `.0`);**不写** `_initial_layer` / `first_layer_*` 类伴随键 —— 数组化与伴随键由构建阶段补 |
| 9 | 输出键 | `name`/`pi_code`/`printer`/`slicer`/`inherits`/`pm_method_version` + 参数键;不写 `id`、`filament_settings_id`(product name 版预设留给构建阶段) |
| 10 | worker 不含切片器知识 | **输出侧**worker 不区分切片器(一律基础数据 JSON);`.ini` 生成与键映射表归**构建脚本**。**输入侧例外**:`.ini` 附件导入仍由 worker 解析(见 V3-P3/P6) |
| 11 | 提交 ID / 重试次数列 | **不加**。branch 由行身份确定;重试计数只在 worker 内存中 |
| 12 | 参考表 | worker **完全不读** PI Codes / Printers / Slicers / Methods 四张表 |
| 13 | 附件导入 | 解析改为"只找关心的键,找不到跳过";支持 `.json` 与 Prusa `.ini`;反写后**不自动提交** |
| 14 | 提交人/提交时间 | 按钮 Automation 写入这两列,worker 读出后写进 PR(V3-P7) |
| 15 | 过程记录 | 附件**不进 Git**,文件名写进 commit message;**提交成功后清空该列**(已确认:每条改动与记录一一对应) |
| 16 | 压力提前 | `BBL` 机型**忽略且不报错**(V2 的"填了即报错"作废);非 BBL 输出 `pressure_advance` + `enable_pressure_advance: 1` |
| 17 | 重复身份 | 检测到同身份的另一行 → 该行永久失败,不提交 PR(V3-P9) |

## 构建阶段边界(明确不在 worker 范围)

```
preset-db(worker 写,基础数据 JSON)
    ↓  构建脚本(读 preset-db)
Pages(构建产物,不进 repo)
```

构建脚本负责:键映射表(含 Prusa `.ini`)、数组化(`"220"` → `["220"]`)、`_initial_layer` 伴随键、product name 版 `name`/`filament_settings_id`、按 `<品名>/<品牌>/<机型>/<切片器>/` 发布布局。

---

## V3-P1 — 表格 schema 对齐(原需求 1)

### 现状

`ensure_schema`([adapters/bitable.py](../src/material_worker/adapters/bitable.py))按 V2 时代的列名工作:会自动创建 `品名`/`品牌`/`机型`/`切片器`/`材料ID`/`切片器版本`/`提交 ID`/`重试次数` 等列 —— 这些列在新表里**已不存在或已改名**,继续运行会把它们重新建出来,污染表格。

### 新表实况(2026-09-11 读取,27 列)

| 列名 | 类型 | worker 用途 |
|---|---|---|
| `PI Code` | 单选(43 个料号) | 身份段 1 / 路径段 1 |
| `打印机型号` | 单选(17 个) | 身份段 2 / 路径段 2、3 |
| `切片软件` | 单选(5 个) | 身份段 3 / 路径段 4 |
| `继承预设` | 文本 | `inherits`(**必填**) |
| `热床温度` | 数字 | `textured_plate_temp`(必填) |
| `喷嘴温度` | 数字 | `nozzle_temperature`(必填) |
| `冷却开启层时` | 数字 | `fan_cooling_layer_time`(必填) |
| `最大风扇速度` | 数字 | `fan_max_speed`(必填) |
| `降速层时` | 数字 | `slow_down_layer_time`(必填) |
| `最小风扇速度` | 数字 | `fan_min_speed`(必填) |
| `最大体积流速` | 数字 | `filament_max_volumetric_speed`(必填) |
| `流量比例` | 数字 | `filament_flow_ratio`(必填) |
| `回抽距离` | 数字 | `filament_retraction_length`(可选) |
| `压力提前` | 数字 | `pressure_advance`(可选,BBL 忽略) |
| `调参方法版本` | 单选(v1 / v1_t1) | `pm_method_version`(必填) |
| `过程记录` | 附件 | 仅取文件名进 commit message(V3-P8) |
| `提交审核` | 按钮(3001) | 触发入口(人工/Automation) |
| `已请求` | 复选框 | 触发信号(worker 清除) |
| `状态` | 单选(7 个) | 状态机 |
| `错误信息` | 文本 | 失败原因 |
| `PR URL` | 文本 | **本轮 PR 的锚点**(V3-P5) |
| `关闭理由` | 文本 | PR 关闭理由同步 |
| `Profile JSON extract` | 附件 | 附件导入入口(V3-P3) |
| `线材密度` | 数字 | `filament_density`(可选) |
| `玻璃化温度` | 数字 | `temperature_vitrification`(可选) |
| `提交人` | 人员 | 读出写进 PR(V3-P7) |
| `提交时间` | 日期 | 读出写进 PR(V3-P7) |

### 期望行为

1. **自动创建**(缺则建,文本/数字/附件三类):`继承预设`、10 项耗材参数列(`热床温度`/`喷嘴温度`/`冷却开启层时`/`最大风扇速度`/`降速层时`/`最小风扇速度`/`最大体积流速`/`流量比例`/`回抽距离`/`压力提前`)、`线材密度`、`玻璃化温度`、`错误信息`、`PR URL`、`关闭理由`、`Profile JSON extract`、`过程记录`;
2. **只校验不创建**(缺列或类型不符 → 启动失败 + 修复指引):`状态`(单选,选项须覆盖 7 个状态)、`已请求`(复选框)、`提交审核`(按钮)、`PI Code`/`打印机型号`/`切片软件`/`调参方法版本`(单选,选项由人工维护)、`提交人`(人员)、`提交时间`(日期);
3. **不再创建**任何已删除的列:`品名`/`品牌`/`机型`/`切片器`/`材料ID`/`切片器版本`/`提交 ID`/`重试次数`;
4. 表中**多出的列**(不在上表内的)一律忽略,不报错、不修改。

### 验收标准

1. 对现表启动 → 不新建任何列、不报错;
2. 删掉任一"只校验"列或改其类型 → 启动失败,错误信息含列名、当前类型、期望类型、修复方式;
3. 删掉任一"自动创建"列 → 启动后自动补齐,类型正确;
4. 全量测试通过(补 schema 校验用例)。

**状态:待实施**

---

## V3-P2 — 必填 / 可选(原需求 2)

### 期望行为

`FIELD_SCHEMA` 的 `required` 位按 §0 第 5/6 条重排,一处定义驱动三件事:

1. **提交校验**:必填缺失/为空 → 逐字段列名报错,落 `错误信息`,不产生 Git 提交;
2. **附件反写**:只反写解析到的键,必填缺失**不再**视为附件失败(V3-P3);
3. **输出文件**:可选键为空 → 该键不出现(由 `inherits` 继承)。

`继承预设` 本轮升为**必填**:为空即报错、不生成 PR(§0 第 5 条)。单选类必填列(`PI Code`/`打印机型号`/`切片软件`/`调参方法版本`)缺失时,错误提示指引"在下拉中选择"。

`压力提前` 是唯一带条件的可选列:`打印机型号` 首词为 `BBL` 时**忽略该值**(不输出 `pressure_advance`/`enable_pressure_advance`,也不报错);其余品牌输出 `pressure_advance` + 派生 `enable_pressure_advance: 1`。

### 验收标准

1. 只填身份 + 核心参数 + 继承预设 → 提交成功,输出文件只含已填键;
2. 缺任一必填(含仅缺 `继承预设`)→ 失败并列出缺失列名;
3. 可选列全空 → 输出文件不含对应键,不报错;
4. `BBL P2S` + 填了 `压力提前` → 提交成功且输出无 PA 键;`Prusa Core One` + 填了 → 输出含 PA 键。

**状态:待实施**

---

## V3-P3 — 附件解析改为"宽容取值"(原需求 3)

### 现状

`MaterialProfile.parse_attachment_json` 要求附件含全部 14 个必填英文键,缺一个即整行失败;反写成功后还会**自动置「已请求」**(V2-P4 的"上传=新建材料"语义)。真实 BambuStudio base 文件(如 `Generic PETG-CF @base.json`)普遍缺 `nozzle_temperature`、`fan_cooling_layer_time`、`temperature_vitrification`,因此必然失败——表里已有一行因此落 `失败`。

### 期望行为

1. **只找关心的键**:按 canonical 字段表逐键查找,找到就取值、找不到就跳过;**不再因缺键报错**,未知键照旧忽略;
2. **只反写、只填空**:已填单元格绝不覆盖;一次原子 update(不产生部分脏数据);
3. **不自动提交**:反写后**不再置「已请求」**——是否提交由用户点按钮决定(点按钮时的"先反写再校验"兜底路径保留,规则一致);
4. **格式兼容**:附件可为 `.json`(键名 = canonical 英文键)或 PrusaSlicer `.ini`(扁平 `key = value`,按 V3-P6 的**导入映射表**反查);`.ini` 中的 `nil` / 空值一律跳过;
5. **失败语义**:
   - 非 `.json`/`.ini`、挂多个文件、下载失败、解析失败 → 写 `错误信息`(前缀「附件解析失败: 」),不反写;
   - **一个关心的键都没认出来 → 静默跳过**,不写错误、不反写;
   - 确定性失败按 `(record_id, file_token)` 只尝试一次,换附件或重启后自然重试;
6. **单选列不从附件反写**(`PI Code`/`打印机型号`/`切片软件`/`调参方法版本` 必须命中选项,由人工手选);`继承预设` ← `inherits` 照常反写。

### 验收标准

1. 上传 `Generic PETG-CF @base.json` → 表里 `线材密度`/`流量比例`/`最大体积流速`/`降速层时`/`最大风扇速度`/`最小风扇速度`/`热床温度` 被反写,`状态` 不变、不产生 PR;
2. 上传仓库现有 Prusa `.ini` → 上表列被正确反写,`nil` 键被跳过;
3. 上传无关键的文件 → 无报错、无改动;
4. 反写后点按钮 → 与手填行走完全相同的提交链路;
5. 全量测试通过。

**状态:待实施**

---

## V3-P4 — 行身份与 Git 输出

### 期望行为

1. **身份**:`identity = f"{PI Code}@{品牌} {机型}"`(如 `L1002@BBL P2S`);唯一性 = 身份 × 切片软件;
2. **路径**:`preset/<PI Code>/<品牌>/<机型>/<切片软件>/<identity>.json`,例:
   ```
   preset/L1002/BBL/P2S/BambuStudio/L1002@BBL P2S.json
   preset/L1002/BBL/P2S/OrcaSlicer/L1002@BBL P2S.json
   ```
   品牌/机型由 `打印机型号` 按第一个空格切分;路径中的空格与大小写原样保留;
3. **输出文件**(基础数据,**所有切片器同一形态**):
   ```json
   {
     "name": "L1002@BBL P2S",
     "pi_code": "L1002",
     "printer": "BBL P2S",
     "slicer": "BambuStudio",
     "inherits": "Panchroma PLA",
     "pm_method_version": "v1",
     "textured_plate_temp": 55,
     "nozzle_temperature": 220,
     "fan_cooling_layer_time": 20,
     "fan_max_speed": 60,
     "fan_min_speed": 0,
     "slow_down_layer_time": 4,
     "filament_flow_ratio": 0.95,
     "filament_max_volumetric_speed": 18,
     "filament_density": 1.17,
     "temperature_vitrification": 60,
     "filament_retraction_length": 0.4,
     "pressure_advance": 0.02,
     "enable_pressure_advance": 1
   }
   ```
   - 数值为**标量**,整数不写 `.0`;**不写** `_initial_layer`/`first_layer_*` 类伴随键(构建阶段补);
   - 不输出 `id` / `filament_settings_id`;
   - `pi_code`/`printer`/`slicer` 独立成键 —— 供构建阶段拼 product name 版 preset;
   - 可选键按是否填写出现;`inherits` 必填故恒在;
   - **文件里没有任何 worker 溯源键**:`submission`(每轮 uuid)/`generated_at` 已按用户确认删除(无用)—— 文件里每一行都是材料数据,谁在什么时候改了什么由 PR 正文与 commit message 交代;
4. **PR 正文讲 diff**(用户确认的形态:第一行简洁的身份,中间是 diff,最后是提交人和时间):
   ```
   L1002@BBL P2S · BambuStudio

   喷嘴温度: 220 °C → 215 °C
   流量比例: 0.95 → 0.92
   回抽距离: (未设置) → 0.4 mm

   提交人: 张三 · 提交时间: 2026-09-11 10:23
   ```
   - 差异 = 默认分支上该档案的现状 vs 本次内容,逐键比对(两者本来就要读,不额外发请求);
   - 标签用**表格列名**、数值带单位,不出现内部键名(`fan_max_speed` 之类审查者不认);
   - 首次提交无旧值可比 → 列全部取值、不带箭头;原先没有/现在没有的键写 `(未设置)`;
   - 身份四要素不重复列出(路径下恒定,首行已交代);派生键 `enable_pressure_advance` 不列;没动的字段不出现。

### 验收标准

1. 一行提交 → 文件落在上述路径,键值与表格一致;
2. 同一身份不同切片软件 → 两个文件、互不覆盖;
3. 可选列为空 → 对应键不出现;输出中无数组、无 `_initial_layer` 键;
4. PR 正文改动一个字段 → 正文只出现该字段的 `旧 → 新` 一行。

**状态:待实施**

---

## V3-P5 — 幂等与重试(无提交 ID 列)

### 期望行为

1. **branch = `material/<身份 slug>`**(空格 → `_`,如 `material/L1002@BBL_P2S_BambuStudio`),同一行永远同一 branch → 重复点击/瞬态重试复用同一 branch 与 PR,绝不产生第二个 PR;
2. **PR 锚点改为 `PR URL` 列**:同一 branch 上会累积多轮历史 PR(已合并/已关闭),按 branch 取"第一个 PR"会误判 → 审查同步改为按行内 `PR URL` 解析出的 PR 编号查询;`PR URL` 为空时才回退到"该 branch 上处于打开状态的 PR";
3. **终态后再次提交** = 同一 branch 上打新 commit + 开新 PR;内容与默认分支完全一致时保留"空 PR 保护"(报明确错误,不静默成功);
4. **重试计数只在内存**(worker 进程内 `record_id -> count`),重启清零;`错误信息` 仍写明重试情况;
5. `已请求`/`状态` 的 claim、重复点击短路、瞬态/永久失败分流逻辑保持不变。

### 验收标准

1. 重复点击(PR 打开中)→ PR 数量不增加,行保持 `审核中`;
2. 已通过/已拒绝后再次提交 → 同一 branch 新 PR,旧 PR 状态不影响新轮判定;
3. worker 重启 → 不影响已提交行的状态推进;
4. 全量测试通过(含多轮 PR 的 fake 用例)。

**状态:待实施**

---

## V3-P6 — PrusaSlicer(已收窄:仅导入方向)

### 结论

**输出侧**:worker 不产出 `.ini`,PrusaSlicer 行与其他切片器走完全相同的路径(基础数据 JSON);`.ini` 生成与键映射表由**构建脚本**维护(§0 第 10 条)。

**输入侧**:`.ini` 附件导入仍由 worker 负责 —— 需要一个**只用于反写**的导入映射表(`prusa key → 表格列`),不做任何输出用途。

### 导入映射表(worker 唯一保留的切片器知识)

| PrusaSlicer key | 表格列 | 取值规则 |
|---|---|---|
| `temperature` | 喷嘴温度 | 优先取 `temperature`;缺失时回退 `first_layer_temperature`(真实文件两者常不等,如 200/210) |
| `bed_temperature` | 热床温度 | 同上,回退 `first_layer_bed_temperature` |
| `extrusion_multiplier` | 流量比例 | |
| `filament_max_volumetric_speed` | 最大体积流速 | |
| `fan_below_layer_time` | 冷却开启层时 | |
| `slowdown_below_layer_time` | 降速层时 | |
| `max_fan_speed` | 最大风扇速度 | |
| `min_fan_speed` | 最小风扇速度 | |
| `filament_density` | 线材密度 | |
| `filament_retract_length` | 回抽距离 | 值为 `nil` → **跳过**(跟随打印机设置) |
| `inherits` | 继承预设 | 原样反写 |
| (`temperature_vitrification` 等) | 玻璃化温度 | Prusa 无对等键 → 不反写 |
| (`M572`/`start_filament_gcode`) | 压力提前 | Prusa 无 filament 级键 → 不反写(真实文件 PA 写在 `start_filament_gcode` 里) |

### 期望行为

1. 附件为 `.ini` → 按 `key = value` 逐行解析,用上表反查到表格列,**只填空格**;
2. `nil`、空值、引号包裹的值、重复出现同一 key → 按 V3-P3 的宽容规则处理(`nil`/空跳过;重复取首次出现值);
3. 解析出的键一个都不认识 → 静默跳过(V3-P3 第 5 条);
4. 输出侧对 Prusa 无任何特殊分支(与其它切片器完全同码路径)。

### 验收标准

1. 上传仓库现有 `Panchroma CoPE @Prusa Core One.ini` → 上表列被反写,`filament_retract_length = nil` 未反写,`压力提前`/`玻璃化温度` 保持空;
2. PrusaSlicer 行提交 → 产出 `.json` 基础数据,内容与同参数的 BambuStudio 行**仅 `slicer` 键不同**;
3. BST 系输出行为不回归。

**状态:待实施**

---

## V3-P7 — 提交人 / 提交时间进 PR

### 期望行为

1. 按钮 Automation 在点击时写入 `提交人`(人员)/`提交时间`(日期),再置 `已请求 = true`(**建议与 `已请求` 同一次更新写入**,避免 worker 抢读空值);
2. worker 在 claim 时读取这两列:人员列取姓名,日期列(毫秒时间戳)格式化为本地时间;
3. 写入 **PR 正文末行**(如 `提交人: 张三 · 提交时间: 2026-09-11 10:23`)与 commit message 文本;**不修改这两列**;
4. 两列为空(未走按钮触发)→ 正文写 `(未记录)`,不影响提交;
5. Git 的 author 字段不做改动(GitHub 不支持自定义 author,保持两平台行为一致)。

### 验收标准

1. 按钮触发 → PR 正文含提交人与时间,与表格一致;
2. 直接勾 `已请求` 触发 → 正文写"(未记录)",提交照常成功。

**状态:待实施**

---

## V3-P8 — 过程记录写进 commit message

### 期望行为

1. `过程记录` 列的附件**不下载、不提交进仓库**,只取**文件名**写入 commit message:

   ```
   [材料] L1002@BBL P2S (BambuStudio)

   过程记录:
   - 2026-09-10_温度塔.jpg
   - 2026-09-10_流量校准.jpg
   ```
2. 无附件时不产生该段落;
3. PR 创建成功后**清空 `过程记录` 列**(已确认,与 V3-P7 同一次回写)—— 照片原件不再保留于表中,改动与记录的对应关系由 commit message 承载。

### 验收标准

1. 带 2 个附件的行提交 → commit message 含两个文件名,仓库里**没有**这两个文件,提交成功后该列为空;
2. 无附件 → message 无该段落,清空操作幂等无副作用。

**状态:待实施**

---

## V3-P9 — 重复身份防护

### 期望行为

处理某行前扫描全表:若存在另一行(不同 record_id)身份相同(同一 `PI Code` × `打印机型号` × `切片软件`),该行**永久失败**,`错误信息` 写明与哪一行冲突,**不产生 PR**;先进入处理的行正常提交。若两行同时待处理,后处理者失败。

### 验收标准

1. 两行同身份 → 只有一行能提交成功,另一行落 `失败` 且写明冲突行;
2. 身份不全(必填缺失)的行不受影响(由校验路径拦截)。

**状态:待实施**

---

## 遗留事项(非 worker 范围)

1. **旧目录改名**:仓库 `preset/*/Creality/K2Pro/`(7 个文件)是当年 typo,应为 `Creality/K2 Pro` —— 由构建脚本或一次性迁移处理。
2. **构建脚本**(独立需求):读 preset-db → 映射/数组化/补 `_initial_layer`/product name 化 → 发布到 Pages。
3. **`压力提前` 与 BBL 的实况差异**:仓库现有 `Polymaker PLA Pro @BBL H2S.json` 实际带 `pressure_advance`(且 `enable_pressure_advance = 0`)。按 §0 第 16 条,worker 对 BBL 忽略 PA → 基础数据里不会有该键。若构建阶段需要保留,请回头调整此条。
4. **bb 侧 `inherits` 取值口径**:BST 与 Prusa 的父预设命名体系不同(如 `Panchroma PLA` vs `Prusament PLA @COREONE`),`继承预设` 一列承载两种体系 —— 需人工按切片软件填对应体系的父名,worker 不做校验。

---

## 影响面(实施时改动的文件)

| 文件 | 改动 |
|---|---|
| [fields.py](../src/material_worker/fields.py) | 列名重写(删 8 列、加 热床温度/玻璃化温度/提交人/提交时间/过程记录) |
| [domain/profile.py](../src/material_worker/domain/profile.py) | `FIELD_SCHEMA` 重写、必填位重排(含 `继承预设`)、派生身份与路径、`to_dict` 重写、附件解析改宽容 |
| domain/slicer_import.py(新) | Prusa `.ini` **导入映射表**(仅反写方向,V3-P6) |
| [adapters/bitable.py](../src/material_worker/adapters/bitable.py) | `ensure_schema` 改为"部分自动创建 + 部分只校验";人员/日期列读取 |
| [adapters/git.py](../src/material_worker/adapters/git.py) | branch 由身份派生;按 `PR URL` 查 PR;PR 正文讲 diff;commit message 携带提交人/过程记录 |
| [services/submission_service.py](../src/material_worker/services/submission_service.py) | 附件反写去掉自动提交、重复身份检测、提交人/时间读取、过程记录清空 |
| [domain/status.py](../src/material_worker/domain/status.py) / submission.py | 去掉 `submission_id` 解析与生成逻辑,改为身份派生 |
| [README.md](../README.md) | 建表指南、路径布局、必填可选、附件规则、仓库分工(Pages)同步更新 |
| tests/ | 回归 + 新增用例(schema/身份/附件宽容/`.ini` 导入/重复身份) |
