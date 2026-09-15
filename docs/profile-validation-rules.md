# Profile 文件校验与构建规则(v4 需求来源)

> **本文是 worker v4 的需求来源。** v4 交付两个脚本:**构建**(基础数据 → 可下发 profile)与**校验**(对构建产物做静态检查),两者都进 CI。另附一份 `pi_product_map.json`(**本轮只做 example 占位**,见 §5)。
>
> **本轮范围(2026-09-12 定)**:只验收这两个脚本**能跑通** —— 用测试样本,不导入真实数据。
>
> 事实依据统一引 [bambustudio-preset-loading-findings.md](bambustudio-preset-loading-findings.md) 的 `F-xx`,本文**不重复证据**。规则编号:`R-xx` = 校验规则,`B-xx` = 构建规则。
>
> 上下文:worker 现状(见 [demands-v3.md](demands-v3.md))只产**基础数据 JSON**(标量、仅含关心的键)写入 `preset-db`。v4 新增的是从基础数据到**用户可导入文件**的那一段,以及它的质量门。

---

## 1. 已确认的前提

只列对规则有约束力的。全部来自 F-xx + 你的实测反馈。

| # | 事实 | 依据 |
|---|---|---|
| P1 | 未注册的键**静默丢弃**,不报错、不弹窗、不中断解析 | F-04、F-05 |
| P2 | 合法键白名单是 filament 专属的 **~152 个**,不是全局注册表的 760 个 | F-11、F-16 |
| P3 | `inherits` 指向不存在的父预设 → 导入被拒 / 重启后消失 | F-07 |
| P4 | **不带 `inherits` 的根预设可以正常导入**(你已实测) | F-07 反馈 |
| P5 | `filament_settings_id` 缺失 → 类型识别失败 → 导入被拒 | F-10 |
| P6 | `version` 需 **2–4 段**数字;1 段或 ≥5 段解析失败 → 导入被拒 | F-02 + 你的实测 |
| P7 | 版本门禁只有上界 `maj ≤ app.maj`,**没有下界** | F-03 |
| P8 | `include` 对用户预设是 no-op:写了**不影响其他值**,但自身不生效 | F-06 + 你的实测 |
| P9 | `name` 与系统/默认预设重名 → 拒绝导入 | F-09 |
| P10 | `setting_id` 在本地下发路径**不被读取**(你已实测缺失也能导入) | F-13 |
| P11 | **只支持当前主版本**(BBS 2.x);不做跨版本矩阵 | 你的 Q1/Q5 |
| P12 | `inherits: ""`(**空字符串**)与"不带该键"**等价**,都按根预设处理;只有**非空但查不到**的 `inherits` 才会让文件被跳过 | [Preset.cpp:1463-1470](../../BambuStudio/src/libslic3r/Preset.cpp#L1463-L1470)、[PresetBundle.cpp:1175-1180](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1175-L1180) |
| P13 | `filament_id` 在**导入**路径与**启动**路径上优先级**相反** | [PresetBundle.cpp:1189](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1189)、[Preset.cpp:1457](../../BambuStudio/src/libslic3r/Preset.cpp#L1457) |

> P12 是实测里会踩的坑:你 repo 里 **94 个文件**写的是 `inherits: ""`。它在 BBS 里**完全合法**(等价于根预设),校验器不能把它报成"父预设缺失"。
>
> P13 的细节见 §2.4 —— 它决定你手写的 `filament_id` 能不能留住。

**范围**:只覆盖 **BambuStudio 2.x**(pin = `v02.08.02.60`,见 §5)。`F-xx` 事实来自**仓库 master 的源码**(`SLIC3R_VERSION` = `02.08.02.61`),并与你已安装的 `02.08.02.60` 交叉核对过。

> 两者只差一个 patch,但 `resources/profiles/BBL` 下有 **263 个文件存在真实数值差异**(§2.2)。所以:**源码事实以 master 为准**(行为差异极小),**产物数值以 pin 为准**。不要把这两个版本混为一谈。
>
> **不要假定这些事实在其他切片器上成立**,见 §5.5。

---

## 2. ✅ 已决策:下发形态 = 展平根预设(方案 B)

**决定(2026-09-12)**:产物**不带 `inherits`**,显式写全所有生效键。

### 2.1 决策记录:为什么不选继承式

| 继承式(方案 A)的问题 | 后果 |
|---|---|
| 父预设名与 BBS 版本强耦合(P3) | 用户装旧版 → **静默拒绝导入**,我们这边观测不到 |
| 上游改父预设 → 我们的行为静默跟着变 | 产物不可复现 |
| 校验无法穷尽 | 只说得出"我们写了什么",说不出"生效后是什么" |

**已接受的代价**:文件约 152 键 / 6–8 KB;不跟随上游改进。

**一个附带好处**:`filament_id` 不再被父预设覆盖。有 `inherits` 时它**导入时生效、重启后被父预设的值顶掉**(F-12、P13),展平后这条覆盖不生效,我们写的 `filament_id` 才留得住(取值见 §2.4)。

> 展平后没写到的键会落到**全局默认值**(见 §6 Q-C),所以 B-02「写全所有生效键」不是优化项,是正确性要求。

### 2.2 基准值从哪来

**决定**:不把父预设同步进自己的 repo,**每次构建时引用切片软件的官方 profile 集**。

引用源 = **BambuStudio 一个 pin 住的 release tag**:

| 需要什么 | 从哪取 |
|---|---|
| 父预设链(`inherits` / `include` 求解的基准) | `resources/profiles/BBL/**` |
| filament 键白名单(152) | `src/libslic3r/PrintConfig.cpp` + `src/libslic3r/Preset.cpp` |
| **"BBS 到底加载哪些预设"** | ⚠️ **`resources/profiles/BBL.json` 的 manifest,不是目录扫描** —— 见下 |

#### 🔴 提取必须走 manifest,`**` 目录扫描是错的

我先前用 `glob("filament/**/*.json")` 扫目录建索引,得到了**错误**的系统预设集。实际加载路径是([PresetBundle.cpp:5054](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L5054)):

```cpp
std::string subfile = path + "/" + vendor_name + "/" + subfile_iter.second;  // 来自 manifest 的 sub_path
```

即**逐个读 `BBL.json` 里列出的 `sub_path`**,目录本身不被遍历。

实测差异(这就是我出错的来源):

| 提取方式 | `filament_list` 条目 | 说明 |
|---|---|---|
| **manifest(正确)** | **2050** | 其中 **150 条指向子目录**:`filament/P1P/`(67)、`filament/SUNLU/`(62)、`filament/Polymaker/`(21) |
| 顶层 `*.json` 扫描 | 1904 | **漏掉 146 个真实加载的预设** |

**后果很实际**:`filament/P1P/` 里放着 `PolyLite PLA @BBL P1P.json`, `filament/Polymaker/` 里放着 `Fiberon PETG-ESD @BBL H2D.json` —— 都是**能撞上我们名字的系统预设**。按顶层扫描建排除集,**会漏 3 个撞名**(见 §2.4)。

> **落到实现上**:B-12 的排除集、V 档的系统预设名索引、R-01 的重名检查,**三处都必须以 manifest 为准**。这是"白名单必须来自上游提取"的一条具体化 —— 而且上游说的"上游"是 manifest,不是文件系统。

> ⚠️ **pin 必须是 release tag,不能是 master。** 实测对比:
>
> | | 本仓库 master | 已安装的公开版 app |
> |---|---|---|
> | `SLIC3R_VERSION` | `02.08.02.61` | `02.08.02.60` |
> | `BBL.json` 的 `version` 字段 | `02.08.00.05` | `02.08.00.04` |
>
> 两者只差**一个 patch 版本**,但 `resources/profiles/BBL` 下有 **263 个文件存在真实数值差异**(不只是 `setting_id`)—— 例如 `Bambu ABS @BBL H2S.json` 的 `filament_change_length`(前者 `4`,后者缺失)、`filament_prime_volume`(前者 `30`,后者 `35`)。
>
> 也就是说:**pin 差一个 patch,产物数值就会变。** pin 是构建的一等输入,不是实现细节,必须进版本库、进产物溯源。
>
> ⚠️ 注意有**两条独立的版本轴**:`SLIC3R_VERSION`(app 版本)和 profile 集自己的 `BBL.json.version`。溯源信息两条都要记。

**CI 取数方式**(浅克隆 + 稀疏检出,只需 32 MB profiles + 2 个源文件):

```bash
git clone --depth 1 --branch "$BBS_TAG" --filter=blob:none --sparse \
  https://github.com/bambulab/BambuStudio.git bbs
cd bbs && git sparse-checkout set resources/profiles \
  src/libslic3r/PrintConfig.cpp src/libslic3r/Preset.cpp
```

### 2.3 方案 B 的边界:没消除什么

展平消除了**导入**侧的版本耦合,但没消除**机型可用性**侧的:

| 检查 | 是否仍与用户实际版本耦合 |
|---|---|
| 能否导入 | ❌ 不再耦合 —— 已无父预设依赖 |
| `compatible_printers` 的机型是否存在 | ✅ **仍耦合**。用户的 BBS 没这个机型 → 装上了,但下拉框里选不到(R-14/R-15) |

**这个降级是可接受的**:失败模式从"静默拒绝导入、用户和你都看不见"变成"导入成功、列表里筛掉、用户看得见并能反馈"。

---

## 2.4 命名与标识(决策 7、9;6/10/11 见下方"由 PI code 派生")

### 决策 7:`name` 与 `filament_settings_id`

**决定**:产物 `name` == `filament_settings_id[0]`,格式 `<产品名> @<品牌> <机型>`。

现有三种写法**并不一致**,需要你定夺:

| 来源 | `name` | `filament_settings_id` | 一致? |
|---|---|---|---|
| 你 repo 现有 487 个 BBS 产物 | `PolyLite PLA Galaxy @BBL P2S` | 同 `name` | ✅ |
| 你给的样本文件 | `PolyLite PLA-CF @Bambu Lab A2L 0.4 nozzle` | `["PolyLite PLA-CF @BBL A2L"]` | ❌ |
| BBL 官方惯例 | `<产品> @BBL <机型>` | — | — |

> 样本本身与决策 7 矛盾(两个值不相等,且用了 `Bambu Lab` + 喷嘴后缀)。**建议按 repo 现有的 `@BBL <机型>` 走**:487 个文件已是这个格式,且与 BBL 官方一致。决策 7 字面写的 `<产品名>@<品牌> <机型>`(无空格)与两者都不同 —— 见下,它其实恰好能绕开撞名。

### 🔴 `@BBL <机型>` 与系统预设**共用命名空间**

你 repo 里现在有 **13 个文件**的 `name` 与 BBL 系统预设**完全同名**:

| 产品 | 撞的机型 | 数量 |
|---|---|---|
| PolyLite PLA | A1 / A1M / H2D / H2S / **P1P** / X1 | 6 |
| PolyTerra PLA | A1 / A1M / H2D / **P1P** / X1 | 5 |
| PolyLite ABS | H2D | 1 |
| **Fiberon PETG-ESD** | **H2D** | **1** |

按 P9 / R-01,这 13 个**导入会被直接拒绝**。**这不是未来风险,是现在就在发生的事。**

> ⚠️ **更正:我先前报的是 10 个,少了 3 个。** 原因见 §2.2 —— 我按目录扫,而 `PolyLite PLA @BBL P1P`、`PolyTerra PLA @BBL P1P` 在 `filament/P1P/`,`Fiberon PETG-ESD @BBL H2D` 在 `filament/Polymaker/`。**这三个都是 BBS 真正会加载的系统预设**(manifest 里列着),所以它们确实会挡住我们的同名文件。
>
> 副作用是好的:这个错误**正好暴露了排除集提取的正确做法** —— 必须走 manifest。如果照我原来的 10 个去写 B-12,漏掉的 3 个会在用户那里以"导入被拒"的形式出现,而我们在 CI 里看不到。

撞名的分布是结构性的(来源:**manifest 驱动的 02.08.02.60 profile 集** vs 你 repo;机型名按你 repo 的写法归一,去掉喷嘴后缀):

| 产品 | BBL 官方覆盖 | 你 repo 覆盖 | 撞名 |
|---|---|---|---|
| PolyLite PLA | A1, A1M, H2D, H2DP, H2S, P1P, X1, X1C | A1, A1M, A2L, H2C, H2D, H2S, P1P, P1S, P2S, X1, X2D | **6** |
| PolyTerra PLA | A1, A1M, H2D, H2DP, H2S, P1P, X1, X1C | A1, A1M, A2L, H2C, H2D, P1P, P2S, X1, X2D | **5** |
| PolyLite ABS | A1, H2D, H2DP, P1P, X1C, X1E | H2D, H2S, P2S, X2D | **1** |
| PolyLite ASA | A1, H2D, H2DP, P1P, X1C, X1E | — | 0 |
| PolyLite PETG | A1, A1M, H2D, H2DP, H2S, P1P, X1C | A2L, P2S, X2D | 0 |
| **Fiberon PETG-ESD** | **H2D, X1C** | H2D | **1** |
| **Fiberon 其余 6 个**(PA6-CF / PA6-GF / PA612-CF / PA12-CF / PET-CF / PETG-rCF) | **H2D, X1C**(名字带型号后缀,如 `Fiberon PET-CF`,与你 repo 的 `Fiberon PET-CF17` **不同名**) | 各 5–6 个机型 | **0** ⚠️ 但见下 |
| **PolyLite PLA-CF** | **无** | A2L, H2S, X2D | 0 |
| 其余 14 个系列 | 无 | 各 3–11 个机型 | 0 |

> ✅ **决定(V4-Q17,2026-09-12):同料异名**不排除,与 BBL 的预设**并存**。**同名**的仍走决策 9(排除)。**两条路分开,互不牵连。**
>
> 理由是这两件事的性质不同:同名是**文件装不进去**(技术故障,必须处理);同料异名是**用户看到两个条目**(体验问题,可以接受)—— 而且用户本来就能在 BBL 那条里选,存在两份不损失能力。

> ⚠️ **Fiberon 是"同料异名"的实例。** 逐条对齐(机型名按各自写法,`X1C` 与 `X1` 是**两台不同的机器**):
>
> | 你的产品 | 你的机型 | BBL 的名字 | BBL 的机型 | 重叠 |
> |---|---|---|---|---|
> | Fiberon PA12-CF10 | H2C, H2D, H2S, P2S, X1, X2D | Fiberon PA12-CF | H2D, **X1C** | **H2D** |
> | Fiberon PA6-CF20 / PA6-GF25 / PA612-CF15 / PET-CF17 / PETG-rCF08 | 同上(各 6 个) | 同名去后缀 | H2D, X1C | **H2D** |
> | Fiberon PET-GF15 / PA612-ESD / ASA-CF08 | H2C, H2D, … | — | — | 无 |
> | Fiberon PPS-CF10 / PPS-GF20 | H2C, H2D, H2S | — | — | 无 |
> | **Fiberon PETG-ESD** | A2L, H2C, H2D, P2S, X1, X2D | **Fiberon PETG-ESD** | H2D, X1C | **名字完全相同 → 撞名(R-01)** |
>
> **所以 `X1C` 那一列不构成重叠** —— BBL 的 Fiberon 用的是遗留写法 `@BBL X1C`(X1 Carbon),你用的是 `@BBL X1`(X1)。两台不同机器。**真正重叠的只有 H2D 一台。**
>
> 在 H2D 上,用户会同时拿到 BBL 的 `Fiberon PET-CF` 和我们的 `Fiberon PET-CF17` —— 名字不撞、导入不被拒(R-01 过),但**它们是同一个耗材的两份预设**。这和决策 9 的撞名问题是**两回事**:
>
> - 撞名(R-01):同名 → **文件装不进去**
> - 产品重叠(Fiberon × H2D):不同名 → **装进去了,和 BBL 的并排显示**
>
> 后者**没有任何规则覆盖**(决策 9 只管同名)—— 已按 V4-Q17 定为**并存**,不需要新规则。影响面:6 个 Fiberon 产品在 H2D 这一台上,用户会看到两份。
>
> 于是它们的 `filament_id` 也**必然不同**(BBL 是 `GFL54` 等,我们是 `J`+PI)—— 这正好是下面"与决策 9 的耦合"那条的同一件事,已经由你明确接受。
>
> 顺带说清 BBL 为什么用简写:那是 **BBL 自己的命名习惯**(`Fiberon PET-CF` 而非 `PET-CF17`),你的完整型号是**你自己的命名**。两边不是一套体系 —— 所以**不能靠"名字大概率不同"来回避这个问题**,只有逐个产品对齐才看得出来。

两个规律:

1. **只有"素色主线"产品撞名** —— Galaxy / Glow / Neon / Starlight / Translucent / Marble / Pro / PLA+ / LW-PLA / PC / PETG Translucent 这些 BBL 都没有,永远不会撞。
2. **BBL 覆盖老机型,你 repo 覆盖新机型** —— 交集就是撞名集。**BBL 每加一个 Polymaker 产品线,撞名面就会扩大。** 这是个会自己长大的问题。

三种可选做法:

| | 做法 | 代价 |
|---|---|---|
| **(a)** | 撞名的组合**直接不出产物** | 用户能从系统预设选到,不丢能力;但下发集与 BBL 自带集产生版本漂移 |
| (b) | 加后缀区分,如 `PolyLite PLA @BBL X1C (Polymaker)` | 丑,且与现有 487 个文件不一致 |
| (c) | 按决策 7 字面格式 `PolyLite PLA@Bambu Lab X1C` | 天然不撞,但与现有文件和 BBL 惯例都不一致 |

**决定(决策 9,暂定):选 (a) —— 撞名组合不出产物。**

落到规则上:

- **B-12**:产物集 = (我们要出的组合) − (pin 的系统预设名 ∩ 我们的 `name` 集)。
- **排除集从 pin 推导,不手工维护。** 反正 V 档 / R 档本来就要建这份"系统预设名索引",顺手就算出来了。BBL 加一个 Polymaker 产品线,我们的产物就自动少几个。
- 🔴 **但排除集的来源必须是 manifest(§2.2),不是目录扫描。** 我按目录扫描得出的排除集**漏了 3 个** —— `PolyLite PLA @BBL P1P`、`PolyTerra PLA @BBL P1P`(`filament/P1P/`)、`Fiberon PETG-ESD @BBL H2D`(`filament/Polymaker/`)。**这三个都是 BBS 会加载的系统预设**,漏掉就意味着这三个文件在我们的 CI 里"通过",到用户那里才发现装不进去。
- ⚠️ **这会让 `dist/` 的文件数随 pin 变化。** 所以 §5 的产物快照必须把两类信号**分开报**:
  - 「pin bump 导致某文件**被移除**」—— 决策 9 的**预期后果**,不是 bug
  - 「某文件数值变了」—— 需要人看
- 因为只是**暂定**,排除策略要**可切换**:放一个 `exclude_policy`(或等价开关),别把 (a) 焊进逻辑。将来改成 (b),只动配置。

决策 9 之后,BBL 已覆盖的产品**仍然出产物**,只是少几个机型(**排除集按 manifest 算,共 13**):

| 产品 | 仍出产物 | 被排除(13) |
|---|---|---|
| PolyLite PLA | A2L, H2C, P1S, P2S, X2D | A1, A1M, H2D, H2S, **P1P**, X1 |
| PolyTerra PLA | A2L, H2C, P2S, X2D | A1, A1M, H2D, **P1P**, X1 |
| PolyLite ABS | H2S, P2S, X2D | H2D |
| **Fiberon PETG-ESD** | A2L, H2C, P2S, X1, X2D | **H2D** |
| PolyLite PETG | A2L, P2S, X2D | — |
| PolyLite ASA | (repo 本来就没有) | — |

> 注意两处变化:P1P 从"仍出产物"挪到了"被排除"(先前那版漏了 `filament/P1P/` 里的系统预设),Fiberon PETG-ESD 新进列表。
>
> 注意 (a) 的隐含后果:**这些机型上用户装到的是 BBL 的预设,不是我们的。** 同一个耗材在不同机型上行为可能不一致 —— 这是决策 9 明确接受的。若哪天要从 (a) 换到 (b),上表就是需要补的清单。
>
> ⚠️ **Fiberon 还有 6 个产品不在这张表里但同样重叠**(名字不同、产品相同,见上文)。**决策 9(a) 管不到它们** —— 那是 V4-Q17。

### `filament_id` 从哪来(决策 6 / 10 / 11 已被下面这条取代)

先看实测事实:

1. **BBL 全库 100 个 `filament_id`,每个 `@base` 预设一个**。按机型的预设**一个都不带**,靠 `filament_id_maps` 按名字从父预设继承(F-12)。
2. **BBL 确实给第三方品牌分配 id**(我早前说"不分配",是错的)—— **但方式和 SUNLU 那一例给人的印象不同**,见下。

**id 的结构**:`GF` + 1 个**材料族字母** + 2 位序号。完整 100 个逐条读出来的分布:

| 前缀 | 材料族 | 条数 | 其中第三方 / Generic |
|---|---|---|---|
| `GFA` | PLA | 18 | — / — |
| `GFB` | ABS · ASA | 9 | **PolyLite ABS `GFB60` · PolyLite ASA `GFB61`** / Generic ASA `GFB98` · Generic ABS `GFB99` |
| `GFC` | PC | 3 | — / Generic PC `GFC99` |
| `GFG` | PETG · PCTG | 10 | **PolyLite PETG `GFG60`** / Generic 4 个 `GFG96-99` |
| `GFL` | PLA | 16 | **PolyLite PLA `GFL00` · PolyTerra PLA `GFL01` · Fiberon PETG-ESD `GFL06` · Fiberon PA6-CF/PA6-GF/PA12-CF/PA612-CF/PET-CF/PETG-rCF `GFL50-55` · eSUN PLA+ `GFL03` · Overture PLA `GFL04` · Overture Matte PLA `GFL05`** / Generic PLA `GFL95/96/98/99` |
| `GFN` | PA | 9 | — / Generic 4 个 |
| `GFP` | PP · PE | 5 | — / Generic 5 个(全 9x) |
| `GFR` | PHA · EVA | 2 | — / Generic 2 个(全 9x) |
| `GFS` | 支撑 · PVA | 17 | **SUNLU 的 7 个:`GFSNL02-08`** / Generic 3 个 |
| `GFT` | PET-CF · PPS | 4 | — / Generic 2 个 |
| `GFU` | TPU | 7 | — / Generic 2 个 |

序号带两个规律:

- **`95-99` = Generic**(各族都留了尾部一段给通用料,实际大量集中在 `98/99`)。
- **第三方没有独立命名空间,而是占用各族里剩下的空位。** Polymaker 占了 `GFL00/01`、`GFB60/61`、`GFG60`、`GFL50-55`;eSUN/Overture 占 `GFL03-05`。**唯一的例外是 SUNLU —— 它拿到的是嵌品牌码的专用块 `GFSNL0x`。**

> ⚠️ **由此得一个直接结论:第三方 id 里的字母不代表材料。** 最刺眼的是 `GFL06` = **Fiberon PETG-ESD**(PETG 产品却挂在 PLA 族的 `GFL` 下),`GFL50-55` 是 6 个 PA/PETG 工程料。BBL 是**从空闲槽位里分配**的,不是按材料归类。
>
> 所以:**我们的派生 id 不要去编码材料类型** —— 那会让人以为它有意义,而 BBL 自己的第三方 id 已经证明这个规律不成立。`<前缀>+PI code` 保持"无脑一一对应"反而更诚实。

5. **你 repo 用的是 `PM<材质><NN>` 体系**(68 个 id,`PMPL31` 在其中)。与 BBL 的 `GF**` **零交集**。
6. `PMxx` ↔ 产品是**严格一一对应**(68 id / 68 产品,无一对多、无多对一)→ 它已经是事实上的**产品级稳定标识**。

### 决定:`filament_id` = `J` + PI code,产品级一一对应

**决定(2026-09-12,取代决策 6 的"沿用 BBL"、决策 10 的"保留 `PMxx`"、决策 11 的"必须随基础数据来")**:

```
filament_id  =  "J" + PI code
```

前缀**已定 = `J`**(决策 16,2026-09-12)。选它的理由见下方"为什么是 `J`":首字符不是 `P`(绕开那条 AMS 分支),与 BBL 全库零交集,且短。

不再维护 `PMxx` 表 —— PI code 本来就是基础数据的**必填字段**([profile.py:93](preset-sync-worker/src/preset_sync_worker/domain/profile.py#L93) `ProfileField("pi_code", ..., True, False)`),而且**它就是 preset-db 的顶层目录名**,天然是**产品级**的(`${PRESET_DIR}/${pi_code}/${brand}/${model}/${slicer}/...`)。因此同一产品的所有机型产物**自动共享同一个 id**,不需要任何映射。

**为什么这个形状是对的**(逐条对应实测):

| 要求 | 为什么满足 |
|---|---|
| 同 id 的预设**基础信息必须一致** | PI code 是产品级 → 同产品所有机型同 id,`alias` / `filament_type` / `filament_vendor` 本就该一致 ✅ |
| 必须与 BBL 的 id 区分 | BBL 全库 id **全部以 `GF` 开头**(`GFA`..`GFU`),任何其他前缀都不撞 ✅ |
| 不引入多余映射表 | PI code 已在基础数据里,且是路径的一部分 ✅ |
| 将来拿 id 块好迁移 | 只改派生方式(换前缀或换表),产品级一一对应的结构不变 ✅ |

> **仍然明确的代价**:这个 id 不会出现在 BBL 的冲洗量表里(那张表目前只有 4 条,且只认 `GF**`,见 §6 Q-B)→ 换料冲洗量走预测值。

> **一处与决策 9 的耦合,已经被 V4-Q17 明确接受。** 决策 10 旧版是"BBL 已覆盖的 5 个产品沿用 `GF**`",新版统一走 `J`+PI —— 这意味着**同一个实物耗材,在我们的预设里和 BBL 的预设里 `tray_info_idx` 不同**。
>
> - 在**决策 9(a)(现状)**下,撞名的机型我们不出产物,所以**同名撞车**的机型上不会两份并存。
> - **但同料异名的机型上会**(Fiberon × H2D,见 V4-Q17)—— 那里 AMS 会当成**两种不同的材料**,材料列表出现两条,冲洗表也只认得 BBL 那条。**你已明确选了"并存"**,所以这是接受的代价,不是遗漏。
> - 若将来要从 9(a) 切到 9(b)(同名也加后缀照出),同上,影响面会从"6 个 Fiberon 产品"扩大到"所有 BBL 已覆盖的产品"。
> - 一句话:**新版 id 规则比旧版更简洁,但把"和 BBL 共享材料类"这个能力彻底放弃了。** 现在这个放弃是**有意识的**。

### 为什么是 `J`:绕开一条向 AMS 下破坏性指令的分支

`tray_info_idx` **就是 `filament_id`** —— [DeviceManager.cpp:1723](../../BambuStudio/src/slic3r/GUI/DeviceManager.cpp#L1723) `j["print"]["tray_info_idx"] = filament_id;`。

而 [DeviceManager.cpp:4785/4802/4845/4860](../../BambuStudio/src/slic3r/GUI/DeviceManager.cpp#L4785) 有 **4 处**按 `size() == 8 && [0] == 'P'` 匹配 `tray_info_idx`,其中两处会**向 AMS 下发破坏性指令**(清空该槽位的耗材设置,或改写其温度区间)。

所以先前那个 `PM` + PI code 的写法有风险:**首字符就是 `P`**,唯一让它不命中的是长度。改成 `J` 后,这个条件**从构造上就不可能成立**(`'J' != 'P'`),不再依赖长度侥幸。

**实测参考**:

- BBL 自己的 id 长度只有 **5(93 个)和 7(7 个 `GFSNL0x`)** 两种,**没有一个以 `P` 开头**。`J` 开头的 id 不在 BBL 的任何命名空间里。
- `filament_list` 这张表**只由 `is_user() && inherits() == ""` 的预设构建**([DeviceManager.cpp:4651-4681](../../BambuStudio/src/slic3r/GUI/DeviceManager.cpp#L4651-L4681)) —— **这正是方案 B 产物的形状**。我们的形态是 BBS 专门支持的路径,这点不变。

> **确定性边界,仍然照实说**:我**没有**证明那条 `P` 分支一定会对我们的产物触发 —— 它的完整条件还要求该 id 不在"该机型的用户根预设表"里。选 `J` 不是因为它已被证伪,而是因为**绕开它的成本是零**。

#### ⚠️ 要守的是**联合条件**,不是单看长度

`size() == 8 && [0] == 'P'` 是一个 `&&`。`J` 只解决了左边的首字符,**长度那一半还在** —— 但因为 `&&`,它单独已经无害了。**所以 V-06 不能写成"长度别等于 8"**,那是一条**假阳性**规则。

🔴 **这条不是假设的,已经踩到了。** 从 preset-db 的历史里取到的真实 PI code:

| PI code | 长度 | `J`+PI | 长度 | 命中 `size==8 && [0]=='P'`? |
|---|---|---|---|---|
| `L1002` / `ABS01` | 5 | `JL1002` / `JABS01` | 6 | ❌ |
| **`L2002CF`** | **7** | `JL2002CF` | **8** | ❌(**首字符是 `J`**) |
| `N600CF20` | 8 | `JN600CF20` | 9 | ❌ |

`L2002CF` 是真实用例(分支 `material/L2002CF@BBL_A2L_BambuStudio`),而 `J`+它**正好 8 字符**。按旧写法"`len != 8`",这个 PI code 会被**误杀**;按联合条件判,它完全安全。

**所以 V-06 的正确写法是**:

```
派生出的 filament_id 不得同时满足 len == 8 且 首字符 == 'P'
```

写成这样有两个好处:① 不再误杀 `L2002CF` 这类真实数据;② 保险丝还在 —— 万一将来前缀改回 `P` 开头(比如真拿到了某个 BBL 块),这条立刻恢复成硬拦截,不需要再改规则。

> **仍然独立于上面的一条**:长度太长会挤 AMS 面板的显示位。这是**体验**问题,不是安全问题,所以它**不该**进 V-06 的硬失败档 —— 要做的话是一个独立的建议档规则,且阈值要按面板宽度定,不能拍脑袋。

### 🟡 顺带更正:BBL 是给第三方分 id 的,但**没给"块"**(除了 SUNLU)

`GFSNL02-08` 表明 BBL **会**给第三方**嵌品牌码的独立块**(SUNLU)。但逐条读完 100 个 id 后要补一句:**SUNLU 是唯一一例**。

Polymaker 拿到的是 `GFL00/01` + `GFB60/61` + `GFG60` + `GFL50-55` —— **散落在三个材料族里、不成块、也不连号**;eSUN/Overture 同样只是插空(`GFL03-05`)。也就是说 SUNLU 的待遇是**特例**。

**对我们有两点影响**:

- 申请 id 块这件事**没有先例密度支撑** —— 只有一个 SUNLU 走通了。概率不好估计,别把它当成"迟早会有"的规划前提。
- 反过来看是好消息:**"插空分配"意味着我们的 `J`+PI 在形态上并不比 Polymaker 现有的更"异类"** —— BBL 自己的第三方 id 就已经是无规律的了。

> ✅ **决定(V4-Q10b,2026-09-12):暂不向 BBL 申请 `GFPM**` 块。** 于是这一节从"待观察的机会"变成"已关闭的议题" —— `J`+PI 就是终态,不再有"将来换表"的待办。上面那条"形态对了,前缀可换"的价值保留着,作为**万一**需要的退路,但**不排期**。

### ⚠️ `filament_id` 在两条加载路径上优先级相反

| 路径 | 时机 | 谁赢 | 代码 |
|---|---|---|---|
| `import_json_presets` | 导入那一刻 | **文件里的值**(先合并父预设,后写入) | [PresetBundle.cpp:1189](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1189) |
| `PresetCollection::load_presets` | 之后**每次启动** | **父预设的值**(先写文件值,后被父预设顶掉) | [Preset.cpp:1436](../../BambuStudio/src/libslic3r/Preset.cpp#L1436) → [1457](../../BambuStudio/src/libslic3r/Preset.cpp#L1457) |

→ 带 `inherits` 时,你写的 id **导入时生效、重启后变成父预设的**。这是最坏的一种失败:**测试时看着是对的。**

→ 展平(方案 B)后这条覆盖不生效,id 才真正留得住 —— §2.1 说的"附带好处"就是这个,这里给出机制。

> 顺带一条以后可能用得上的机制:`<用户预设目录>/filament/base/` 下的**自定义根预设会被优先加载**([Preset.cpp:1358](../../BambuStudio/src/libslic3r/Preset.cpp#L1358)),子预设可以 `inherits` 它们。方案 B 用不到,但如果哪天想回到继承式,这是唯一"不依赖 BBL 版本"的继承入口。

---

## 3. 校验规则(校验脚本)

分三档,按**失败的可见性**排。不可见的必须前置到 CI。

### 3.1 硬失败档 —— 导入被拒 / 预设重启后消失

| # | 规则 | 失败后果 | 依据 |
|---|---|---|---|
| R-01 | `name` 存在,且**不与目标版本任何系统/默认预设重名** | 拒绝导入(弹窗计数不含它) | P9、§2.4 |
| R-02 | `filament_settings_id` 存在,且是**非空字符串数组** | 拒绝导入 | P5 |
| R-03 | `version` 存在,且能按 **2–4 段**数字解析 | 拒绝导入 | P6 |
| R-04 | `version` 的 `maj ≤` 目标主版本 | 导入成功但重启后消失 | P7 |
| R-05 | 若带 `inherits`,目标父预设**在目标版本预设集中存在** | 拒绝导入 / 重启后消失 | P3 |
| R-06 | **每个配置键都在 filament 白名单(152)内** | 该键静默丢弃,参数不生效 | P1、P2 |
| R-07 | **绝不出现 `include`** | 自身不生效(不影响其他键) | P8 |
| R-08 | 每个配置键的值**一律数组形态**(无裸标量) | 值被替换为默认 | F-15 |

### 3.2 静默失效档 —— 装上了但值不对

| # | 规则 | 失败后果 |
|---|---|---|
| R-09 | 数组长度与 `filament_extruder_variant` 对齐 | 形状不符 → 替换为默认 |
| R-10 | 枚举值在合法集合内 | 替换为默认 |
| R-11 | 数值在 `min`/`max` 范围内 | 替换为默认 |
| R-12 | **同一批产物内:`filament_id` 同一产品必须相同、不同产品必须两两不同** | 不同产品撞 id → AMS 槽位按字典序显示**另一个产品**的名字和材料类型 |
| R-13 | **`compatible_printers` 必须存在且非空**,且**同一产品的产物不得列出同一个机型** | `alias` 被静默清空 → 部分 UI 拿到**空名字**(另一部分会兜底,所以是"看运气",见下) |

**R-12 说明** —— `filament_id` 是"材料小类"标签,不是唯一键。实测(02.08.02.60,按 manifest 的 2050 个 filament 预设):

| 事实 | 数值 |
|---|---|
| 磁盘上**带** `filament_id` 的预设 | **100 个** —— 恰好是 100 个 `@base`,每个 id 出现**一次** |
| 运行时**能拿到** id 的预设 | **2028 个** —— 其余靠 `filament_id_maps` 按预设名从父预设传播 |
| 实际被用到的 id | **100 个,全部被共享** |
| 单个 id 的最多共享数 | **43**(`GFA00` 与 `GFA01`) |
| 你的产品 PolyLite PLA(`GFL00`) | 被 **16 个**预设共享 |

所以"撞车"本身是设计如此:`filament_id` 表达的是**材料小类**,不是身份。一个 id 服务几十个预设是**常态**,不是异常。

真正会出事的是**反向查找**:[PresetBundle.cpp:648](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L648) `get_filament_by_filament_id` 遍历后返回**第一个**匹配的预设,代码注释写明它的前提是 *"basic filament info should be same in the parent preset and child preset"*。而预设集**按名字排序**([Preset.cpp:1532](../../BambuStudio/src/libslic3r/Preset.cpp#L1532)),所以"第一个"由**字典序**决定。消费方全是 AMS 面板(`StatusPanel` / `AMSMaterialsSetting` / `AMSDryControl` / `CalibUtils`)。

> 这也是**不能借用 BBL 通用 id** 的原因:若 PLA-CF 用 `GFL98`,字典序上 `Generic PLA-CF` 在 `PolyLite PLA-CF` **之前**,且该函数默认 `only_system=false` **不过滤系统预设** → 返回的永远是 BBL 的预设,我们的被完全盖掉。
> **R-12 是跨文件规则** —— 单看一个产物看不出问题,必须整批校验。因此 id **不能逐文件独立生成**,必须来自产品级来源(PI code 满足这一点)。

**R-13 说明** —— `alias` **不是 JSON 键**(BBL 全库 2050 个 filament 预设 **没有一个**写 `alias`,它也不在 127 个注册键里),而是**运行时自动推导**的([Preset.cpp:3339-3358](../../BambuStudio/src/libslic3r/Preset.cpp#L3339-L3358)):

- 只对 `is_user() && inherits() == ""` 的预设生成 —— **正是方案 B 产物的形状**;
- 值 = `name` 中**第一个 `@` 之前的部分**(再 trim);
- **唯一性按 `compatible_printers` 逐机型判定**([is_alias_exist, Preset.cpp:2887-2907](../../BambuStudio/src/libslic3r/Preset.cpp#L2887-L2907))—— 所以我们的各机型产物**各自保留自己的 alias**,这个设计成立 ✅;
- **但** `compatible_printers` 缺失时 `is_alias_exist` 直接返回 `true` → **alias 被清空**。

#### ⚠️ 更正:后果比我先前说的**轻**,但更**不可预测**

我先前写成"alias 被清空 → AMS 面板显示空白名"。**这句话不准确**,读完消费者代码后修正:

`alias` 被清空后,后果**取决于调用方怎么取这个名字**,两条路都存在:

| 取法 | 清空后拿到什么 | 使用者 |
|---|---|---|
| `get_preset_alias(preset, **true**)` | **能兜底** —— 见 [Preset.cpp:3402-3414](../../BambuStudio/src/libslic3r/Preset.cpp#L3402-L3414),`alias` 为空时**重新按 `name` 取 `@` 前段**返回,唯一性检查被绕过 | `AMSDryControl`、`AMSMaterialsSetting`(2 处)、`FilamentManagerVM`、`wgtFilaManagerPanel` |
| 直接读 `preset.alias` 成员 | **空串** | [PresetBundle.cpp:665](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L665)(`FilamentBaseInfo::filament_name` → AMS 槽位信息)、`UserPresetsDialog:121`、`wgtDeviceNozzleRackUpdate:520` |
| `Preset::label()` | 兜底回退到 `name` | 预设列表 UI |

还有一处**口径错位**:`ConfigWizard.cpp:2001` 用 `.alias` 统计冲突,空 alias 会让统计漏掉这些预设。

> **所以准确的说法是:`compatible_printers` 缺失不会让功能崩掉,而是让"这个名字"在不同界面之间变得不一致 —— 有的界面兜底了、有的没有。** 这种"看运气"的 bug 比一律空白更难查,所以仍然值得一条硬规则挡住。但我不该把它说成"必然显示空白名"。
>
> ⚠️ 另外注意 `force=true` 兜底**绕过了唯一性检查** —— 两个预设真的撞了 alias 时,界面照样各自显示同一个名字,用户看到的是**两个一模一样的条目**。这才是撞 alias 最难看的后果。

> 顺带一个好消息:`alias` 取的是 `@` 之前的部分,所以 §2.4 里 `PolyLite PLA @BBL X1C` 与 `PolyLite PLA@Bambu Lab X1C` **推出来的 alias 完全相同**(都是 `PolyLite PLA`)—— 决策 7 的空格之争**不影响** AMS 显示。

> **另一个好消息:喷嘴变体天然安全。** `m_printer_hold_alias` 的键是 `compatible_printers` 里的**整串机型名**([Preset.cpp:3366-3372](../../BambuStudio/src/libslic3r/Preset.cpp#L3366-L3372)),而喷嘴号**就写在机型名里**(`Bambu Lab A2L 0.4 nozzle`)。所以同一产品的 `0.4 nozzle` / `0.6 nozzle` 两份产物落在**不同的键**下,**都保留 alias** ✅ —— 决策 7 里 `<产品名>` 后面到底带不带喷嘴后缀,在这一层没有代价。

### 3.3 可用性档 —— 用户能不能看见

| # | 规则 | 失败后果 |
|---|---|---|
| R-14 | `compatible_printers` 每一项都匹配目标版本的机器预设名 | 不匹配 → 下拉框里被过滤掉 |
| R-15 | 目标机器型号确实存在(不能写未来机型) | 同上 |

**R-14 写哪个名字**(实测确认)——匹配是**逐字节相等**,比的是**当前激活的机器预设名**([Preset.cpp:806](../../BambuStudio/src/libslic3r/Preset.cpp#L806) `active_printer.preset.name`)。而机器预设名有两层:

| 名字 | 例 | 能不能写 |
|---|---|---|
| **带喷嘴**的机器预设名 | `Bambu Lab A2L 0.4 nozzle` | ✅ **写这个**。162 个机器预设里,用户实际选中的就是这些 |
| 裸机型名(无喷嘴) | `Bambu Lab A2L` | ❌ 只有 14 个,`inherits`/`printer_model`/`printer_variant` **全为 `None`** —— 是"父模板",不是用户选中的对象 |

> 例外在 [Preset.cpp:809](../../BambuStudio/src/libslic3r/Preset.cpp#L809):用户**改过**机器设置后,激活预设的 `inherits()` 是那份**带喷嘴的系统预设名**,此时走 `is_compatible_with_parent_printer` 兜底匹配。实测你磁盘上的 4 个用户机器预设,**`inherits` 全部是 `Bambu Lab <型号> <喷嘴> nozzle`**(如 `Bambu Lab H2D 0.4 nozzle`)。
>
> **所以结论一致:写带喷嘴的完整系统预设名。** 它同时覆盖"系统机器"和"用户改过的机器"两条路径;裸名两条都不覆盖。

> 顺带一个覆盖度事实:现有 487 个产物 `compatible_printers` **只用过 13 个值,全部是 `... 0.4 nozzle`** —— 没有 0.2 / 0.6 / 0.8 的产物。这不是错,但说明"每产品一机型"的矩阵只铺了 0.4 那一行。

> R-14/R-15 只管"写了的值对不对";**"有没有写"是 R-13**(§3.2)。两档都会让预设"用不了",但方式不同:R-14 是**被筛掉**(看得见原因),R-13 是**名字对不上**(看不见原因)。

**R-13 的精确边界**(`is_alias_exist` 的两个 return,读代码后确认,两者后果**不同**):

| `compatible_printers` | `dynamic_cast<ConfigOptionStrings*>` | 函数返回 | `alias` | 可见性 |
|---|---|---|---|---|
| **键不存在** | `nullptr` → 直接 `return true` | true = "别名已存在" | **被清空**([Preset.cpp:3352-3353](../../BambuStudio/src/libslic3r/Preset.cpp#L3352-L3353)) | 反而**全机型可见** |
| **`[]` 空数组** | 拿到对象,`values` 为空 → 循环不执行 → `return false` | false = "无冲突" | **保留** | 同样**全机型可见** |

→ **只有"键整个缺失"才清空 `alias`**。而 `[]` 不清空。
> ⚠️ 注意可见性那一列**和直觉相反**:[Preset.cpp:805](../../BambuStudio/src/libslic3r/Preset.cpp#L805) 的判断是 `!has_compatible_printers || ...`,而 `has_compatible_printers` 要求"非空"(781 行)—— **没写或写空 = 对所有机型都兼容**,不是"谁都不兼容"。所以这两行不是"显示不出来"的问题,**就是 `alias` 被清空**这一个问题。两条都要报,但理由和文案不同,别合并。

### 3.4 输出要求

- 报告按 **硬失败 / 静默失效 / 可用性** 三档分组,硬失败退出码非 0。
- 每条命中给出:`文件` / `键` / `规则号` / **一句话后果**(如"该键会被 BambuStudio 静默丢弃,参数不生效")。
- 🔴 **凡涉及"上游没有某个东西"的规则,必须报出上游的哪个文件**(见 §2.2)。实测教训:我用目录扫描代替 manifest,凭空造出了 3 个"没撞名"和 1 个"继承断链"。**失败信息里带上游文件路径,才能一眼区分"产物真错"和"索引建错"。**
- 校验器**必须能独立运行**:输入一个目录或单个文件即可,不依赖构建脚本的中间产物。
- **`inherits: ""` 不得报为"父预设缺失"**(P12)。现有产物里有 94 个这样的文件,误报会直接淹没真问题。

> **架构要求**:校验脚本的白名单**必须来自上游提取**(见 §5),**不得**引用构建脚本自己的常量。否则构建脚本写错键名时,校验脚本会跟着一起错 —— 那就等于没校验。

### 3.5 基础数据档:V 规则(决策 8)

**决定**:构建从**基础数据的校验**开始;其中最重要的是 **`inherits` 是否存在**。

落在实现上,校验器是**一个引擎、两个档位**,用 `--stage` 切换:

| 档位 | 输入 | 规则 | 何时跑 |
|---|---|---|---|
| `--stage base` | `preset-db` 基础数据 | `V-xx`(本节) | 构建**之前**(前置门) |
| `--stage artifact` | 构建产物 `.json` | `R-01..R-15`(§3.1–3.3) | CI,构建之后 |

> **为什么不拆成两个脚本**:两档共享同一套"从 pin 的官方 profile 集取事实"的机制(键白名单、预设名索引、机型索引)。拆开就要复制一遍。
> **但规则必须分开编号**:两档的索引都来自上游提取,谁也不引用构建脚本的常量(§3.4)。

| # | 规则 | 失败后果 | 依据 |
|---|---|---|---|
| V-01 | 必填字段齐全(`name` / 产品标识 / 继承预设) | 构建无法进行 | v3 决策 #5 |
| V-02 | **`inherits` 链上每一跳都存在于 pin 的官方 profile 集中** —— 不只查第一跳 | 展平拿不到基准值 | P3、决策 8 |
| V-03 | `inherits` 若为空串,按"无父预设"处理,不报错 | 误报 | P12 |
| V-04 | 引用的机型名出现在 pin 的机器预设集中 | 产物 R-14/R-15 不可见 | §2.3 |
| V-05 | PI code 能在映射表中查到产品名 | `name` 无法生成 | 决策 7、B-11 |
| V-06 | **PI code 存在、非空、字符集在约定范围内**;且派生出的 id **不得同时满足 `len == 8` 与首字符 `== 'P'`** | 派生出的 `filament_id` 触碰 AMS 破坏性分支 | §2.4。取代旧 V-06;**不是单纯的"长度 ≠ 8"**,理由见 §2.4(会误杀 `L2002CF`) |

> **V-06 变了。** 旧版写的是"基础数据自带 `filament_id`(`PMxx`)" —— 那是决策 10/11 的后果,已被 §2.4 的新决定取代。现在 `filament_id` 由 PI code **派生**([B-07](#4-构建规则构建脚本)),所以基础数据**不需要**再带一个 id,只需要 PI code 本身可用。
>
> 连带地,`pi_product_map.json` 的职责只剩一件:**PI code → 产品名**(供 `name` 用)。它**不负责** id —— 这正好把它从"阻塞构建"降级成"阻塞命名"。两张表仍是分开的。
>
> ⚠️ **V-06 在本轮又改了一次(2026-09-12):去掉"长度 ≠ 8",改成联合条件。** 起因是建 example 表时去 preset-db 历史里取真实 PI code,发现 **`L2002CF`(7 字符)是真实存在的**,`J`+它正好 8 字符 —— 旧写法会把这个真实用例**误杀**。冲突的完整分析见 §2.4。**这是"规则写严了"而不是"数据有问题"**,所以改规则。

**V-02 为什么要查整条链**:R-05 只关心第一跳(它决定导入是否被拒),但 **B-02 展平必须走完整条链** —— 链上任何一跳缺失,展平就拿不到完整的基准值。实测链长这样:

```
Generic PLA @BBL A2L  →  Generic PLA @base  →  fdm_filament_pla  →  fdm_filament_common
```

### 3.6 先跑一遍:对现有产物的实测结果

用**已安装的 02.08.02.60 profile 集(2050 个 manifest 条目)**作为索引,对 `Polymaker-Preset` 里 **487 个 BBS 产物**跑了一遍上述规则:

| 结果 | 数量 | 说明 |
|---|---|---|
| `inherits` 链**完整解析成功** | 285 | 逐跳走完整条链,不只是第一跳 |
| **无父预设**(不含该键 108 + 空串 94) | 202 | 这批**已经是展平的** |
| **`inherits` 查不到 → 导入被拒** | **0** ✱ | 见下方更正 |
| **`name` 与系统预设撞名 → 导入被拒** | **13** | 见 §2.4(先前报 10,漏了 3 个) |
| **R-12 `filament_id` 双射** | **✅ 68 产品 / 68 id,0 缺失,0 一对多** | 新规则,现状全过 |
| **R-13 `compatible_printers`** | **✅ 0 缺失、0 空数组,13 个值全部命中机器预设** | 新规则,现状全过 |

> ✱ **一处更正:我先前报"1 个 `inherits` 查不到"(`Fiberon PET-CF @BBL H2D`),现在复现不出来。** 用 manifest 索引重跑,487 个文件的继承链**全部解析成功**,master 的 `resources/profiles/BBL` 也是同样结果。我没有找到当初那次的判据,所以**按"当前不可复现"处理,不保留那条结论**。
>
> 这带来一个方法论上的提醒:**索引怎么建,直接决定校验器的结论。** 我那次多半是索引建得不对(与 §2.2 的目录扫描错误同源)。所以真做校验器时,"**报出的每个失败都要能指到具体的上游文件**"应该作为输出要求 —— 否则无法区分"产物真有问题"和"索引建错了"。

**三个结论**:

1. **规则不是空转的** —— 现有产物里已经躺着 **13 个**导入会被拒的文件。
2. **现有产物本身就是半展平状态**(202/487 无父预设,285 带继承),说明"要不要展平"这件事在流程里从来没被定死过。方案 B 的价值之一就是把这种随机状态收敛掉。
3. **R-12 / R-13 是回归护栏,不是现存缺陷。** 现有 487 个文件在这两条上**全过** —— 但它们过的方式是"人工一直小心",不是"结构上不可能错"。R-12 尤其如此:它现在是靠"人记得给新产品发新号"维持的双射,一旦由脚本生成,这个双射就必须由**派生规则**保证(§2.4 的 `PI code` 派生物正好做到),否则迟早会破。

### 这 13 个怎么处理(决策 12:重生成)

不手工修,由 v4 重新生成。**一条路径就够**:

| 问题 | 数量 | 重生成能自动修好吗 |
|---|---|---|
| `name` 撞名 | 13 | ✅ **会自动消失** —— 决策 9 把这些组合排除了,产物里不再有它们 |
| `inherits` 查不到 | 0 | — 当前不可复现(见上),不留待办 |

> 先前我在这里写过"另有一个 `inherits` 查不到的文件必须先改基础数据"。那条已撤:没有可复现的样本。**但 V-02(继承链完整解析)仍然要保留** —— 它挡的是将来基础数据里新引入的断链,不是这一批。
>
> ⚠️ **一个必须提前说清的后果:按决策 9(a),这 13 个组合会被排除,意味着这 13 个文件在 `dist/` 里直接不存在。** 对已经装了旧版的用户来说,这是一次**静默的功能回退**(他们原本能用的机型预设消失了),而且 BBS 不会提示。这不是 bug,是决策 9 选的代价 —— 但要有人知道它会发生。

### ⚠️ 「重生成」的一个隐含前提

如果 487 个产物全部改由 v4 生成,那 **`Polymaker-Preset` 就从"手工维护的仓库"变成了"构建产物"**。后果是:

- **永远不要手工编辑那 487 个文件** —— 改了会被下次构建覆盖。所有修正都回到基础数据。
- ~~**68 个 `PMxx` 目前只存在于那 487 个文件里**,重生成前必须先提取。~~ **✅ 这一条已被 §2.4 的新决定解除**:`filament_id` 改由 PI code 派生,不再依赖 `PMxx` 表。重生成时 id 会**变**(`PMxx` → `J`+PI),但**不会丢**,而且新旧都是 68 个产品一一对应 —— 也就是一次性的、可验证的 id 迁移,不是数据抢救。
- 现有那 202 个"已经是展平"的文件里如果有手工调整过的值,**那些值也会丢** —— 迁移时要先和基础数据对一遍。

---

## 4. 构建规则(构建脚本)

> 以下按**方案 B(展平根预设)** 展开。若最终选 A,只需保留 B-01/B-03/B-05/B-06。

**执行顺序**:`B-01 → V-01..V-06(§3.5 前置门)→ B-02 → B-03..B-12 → B-13(自检)`。基础数据不过 V 档就不进入展平。

| # | 规则 | 说明 |
|---|---|---|
| B-01 | 输入 = `preset-db` 基础数据 + **pin 住的 BBS release tag 官方 profile 集** | 基准来源见 §2.2。不用自己的 repo 存父预设副本。pin = `v02.08.02.60`(决策 5) |
| B-02 | **求解完整继承链**:`inherits` 链 + `include` 叠加 + 自身键 | 构建的核心工作。`include` 的语义是"相对集合默认预设做稀疏 diff 叠加",必须复刻(F-06 的反面) |
| B-03 | **写入 `filament_settings_id`** = `[name]`,与 `name` **逐字节相等** | P5、决策 7。基础数据不含它,在这一层补(v3 决策 #9 不变) |
| B-04 | 展平后**不带 `inherits`** | 方案 B 的关键(§2.1) |
| B-05 | 所有键**数组化**,并按 `filament_extruder_variant` **对齐长度** | R-08、R-09 |
| B-06 | `version` 写 **pin 的 BBS 版本**,2–4 段(此处 `2.8.2.60`) | R-03、R-04。展平后 `version` 仍必填 |
| B-07 | `filament_id` = **`"J" + PI code`**,由构建**派生**(取代决策 6/10/11);前缀是构建参数(**当前 = `J`**),不是数据 | §2.4。同一 PI code 的所有机型产物必然同 id;将来换前缀 / 换 `GFPM**` 块只改这一个参数 |
| B-08 | **不写** `include`、`setting_id`、`idle_temperature` 类无效键 | R-07、R-06 |
| B-09 | `name` 按 §2.4 的格式生成,**且必须避开系统预设名** | R-01。撞名组合由 B-12 排除 |
| B-10 | 输出**确定性**:同一输入必得同一字节 | 支撑快照测试(§5) |
| B-11 | **PI code → 产品名映射表**由构建脚本读取,不在代码里硬编码(**只管名字,不管 id**) | 决策 7。id 已由 B-07 从 PI code 派生,这张表退回纯命名用途 —— 表没到位也不阻塞 id |
| B-12 | **撞名组合直接不出产物**:产物集 = 目标组合 − (pin 系统预设名 ∩ 我们的 `name` 集) | 决策 9。排除集从 pin 推导,策略可切换 |
| B-13 | **构建结束时自检 `filament_id` 双射**:同一 PI code → 同一 id,不同 PI code → 不同 id | R-12 的构建侧对偶。与校验脚本**独立实现**(不共用代码),否则等于自己证自己 |

---

## 5. v4 交付物与 CI 形态

### 交付物

```
build_preset.py     preset-db 基础数据 + pin 的 BBS profile 集  →  可下发的 .json(每个机型/喷嘴一份)
validate_preset.py  --stage base      基础数据                    →  V 档报告(构建前置门)
validate_preset.py  --stage artifact  .json 文件或目录            →  R 档报告,硬失败非 0 退出
pi_product_map.json PI code → 产品名(example 起步,见下)
```

构建脚本自身**也要能单独跑 V 档**:`build_preset.py --validate-only` 等价于 `validate_preset.py --stage base`,方便本地迭代。

#### 本轮的验收标准:**能跑通,不是能发布**

> ✅ **决定(V4-Q15,2026-09-12):这一轮不做真实数据。真实数据你会在一切就绪后导入。本轮只验收 `build` + `validate` 两个脚本本身。**

这条决定改变了**验收标准**,而且是往好的方向改 —— 它把两个原本缠在一起的风险拆开了:

| | 本轮(测试数据) | 真实数据导入轮 |
|---|---|---|
| 验收什么 | 脚本**跑得通**:manifest 解析对、V/R 档会报错也会放行、退出码正确、快照能比对 | **产物对不对**:13 个撞名是否消失、数值是否与基础数据一致 |
| 前置 | 只要一份能触发各类规则的**人造样本** | `pi_product_map.json` 填实、V4-Q15 的手工改动对账完成 |
| 谁回答 Q15/Q14 | 不用回答 | 必须回答 |

**所以 `pi_product_map.json` 现在只需要一份 example**(V4-Q14)。它要**明显是占位**(名字里带 `EXAMPLE`/`TODO`,甚至故意留一两行指向不存在的产品),因为它的作用是**让 `build_preset.py` 走真实的读取路径**,而不是提供正确答案 —— 一份看起来像真的占位表比没有表更危险。

**已建好:放在 `preset-db` 仓库根,与 `materials/`、`preset/` 平级。** 放这里的理由:键就是 `preset/<PI Code>/` 的目录名,**同一份标识符和数据同仓**,V-05 可以顺带查"目录有而表里没有 / 表里有而目录没有"。

```jsonc
{
  "_schema_version": 1,
  "_status": "EXAMPLE",          // 填真数据时改 ACTIVE
  "_note": "…",
  "L1002":    "EXAMPLE PLA 占位产品名",
  "ABS01":    "EXAMPLE ABS 占位产品名",
  "L2002CF":  "EXAMPLE 七字符 PI 占位产品名",
  "N600CF20": "EXAMPLE 八字符 PI 占位产品名"
}
```

**接口约定**(构建脚本按此实现):

| 约定 | 说明 |
|---|---|
| 键 = PI code,**值 = 产品名** | 平铺一层,不嵌套 |
| `_` 开头的键是**元数据**,不是 PI code | 构建脚本必须跳过;PI code 不以 `_` 开头 |
| `_status` 决定**能不能出可发布产物** | `EXAMPLE` → 只允许测试构建(产物名会带着 `EXAMPLE`,肉眼可辨);`ACTIVE` → 正常 |
| 表里**查不到**某个 PI code → **硬失败** | 即 V-05。**绝不回退成"用 PI code 当产品名"** —— 那会产出看起来正常的错名 |
| 表**读不到 / 解析失败** → **硬失败** | 即 §5 第 4 点。不静默降级成空表 |

> **4 行是怎么选的**:键取自 preset-db 的真实历史(`ABS01` / `L2002CF` / `N600CF20` 都真出现过,`L1002` 来自测试夹具),这样**键的字符集和长度分布是真的**;值则全是假的。**`L2002CF` 是特意留的** —— 它是唯一让 `J`+PI 落到 8 字符的真实用例,放在这里等于给 V-06 的联合条件留了一个**回归样本**。删掉这一行,V-06 就没人测了。

**唯一不能"先随便写"的是它的接口**(见下面第 4 点):键是 PI code、值是产品名,加载失败要硬失败而不是静默降级。

对人造样本的要求:覆盖到**每一档至少一条**,特别是**该报错的时候真的报错**。只测 happy path 的校验脚本等于没有校验脚本 —— 一个"永远退出 0"的 `validate_preset.py` 在本轮也会显示为绿色的。

### 版本 pin(§2.2 的落地)

**决定(决策 5)**:首个 pin = **`v02.08.02.60`**。

已核实:

- 仓库里 tag `v02.08.02.60` **存在**(邻近的还有 `v02.08.02.61` / `v02.08.03.65` / `v02.08.03.66`)。
- 它**与你本机已安装的 app 版本一致** —— 所以本地就有一份现成的参照物:
  `~/Library/Application Support/BambuStudio/system/BBL/`,其中 `BBL.json` 的 `filament_list` 有 **2050** 条。
  → **V 档和 R 档的索引在本地不用克隆就能建**,CI 里再走下面的浅克隆。
  ⚠️ **建索引请按 `BBL.json` 的 `sub_path` 逐个读**(§2.2),不要 `ls filament/*.json` —— 后者只有 1904 条,会漏 146 个。
- `2.8.2.60` 与 `02.08.02.60` 语义等价(主版本都是 `2`),写哪种都行,但**溯源里要统一一种**。

**pin 由 CI 处理,更新频率待定。** 建议形态:**pin 是一个入库的文件(如 `bbs_version.txt`),不是 CI 变量。** 理由:pin 决定产物数值(§2.2),必须能被 review、能被产物的溯源信息引用。

```yaml
env:
  BBS_TAG: ${{ vars.BBS_TAG }}        # 或读 bbs_version.txt
steps:
  - run: ./ci/fetch_bbs.sh "$BBS_TAG"          # §2.2 的浅克隆 + 稀疏检出
  - run: python validate_preset.py --stage base --bbs ./bbs preset-db/   # 前置门
  - run: python build_preset.py --bbs ./bbs --out dist/ --provenance dist/PROVENANCE.json
  - run: python validate_preset.py --stage artifact dist/ --bbs ./bbs    # 硬失败即 fail
  - run: python validate_preset.py --stage artifact dist/ --snapshot dist.sha256   # 快照比对
  - run: ./ci/check_bbs_update.sh              # 只上报,不自动改 pin
```

> ⚠️ **不要做"检测到新版 BBS 就自动 bump pin"。** 上游一改父预设数值,产物就变 —— 自动 bump 等于让上游**静默改写我们的产品参数**。正确行为是:检测到新版 → 开 issue 或评论 → 人工跑一次 diff、看过变化再改 pin。
>
> 这一条与 §2.1 的"不跟随上游改进"是同一个取舍的两面。

### 四点建议

1. **两档规则不要混用。** R 档**不能**拿来校验基础数据(基础数据不含 `filament_settings_id`、不数组化,套 R 档等于没校验);V 档也**不能**替代产物验收(V 档看不见展平后有没有漏写键)。决策 8 的意思是**构建先过 V 档**,不是"用 R 档校验基础数据"。
2. **白名单不要硬编码,构建时从 pin 的源码提取**(附录 A.1 已跑通)。既然 §2.2 已经为了 profiles 拉了源码,提取白名单是顺手的事 —— 不必再单独维护一份 `filament_keys.json`。
   > 更稳的做法是从**目标版本的二进制** dump 键集,免去维护正则。可行性未验证,可作为 v4 之后的一项。
3. **产物快照不是可选项。** §2.2 决定了产物数值跟着 pin 走,所以"pin 变了 → 产物变了"必须**在 CI 里显式可见**。B-10 保证确定性,快照才有意义。
4. **PI code → 产品名映射表先做成 example,但接口要当真。**(✅ V4-Q14)你说表你后面补,所以 v4 先入库一份**明显是占位**的 `pi_product_map.json`(3–5 行),让 `build_preset.py` 走真实读取路径 —— 这样表一到位就只是换数据,不用改代码。B-11 要求它不硬编码,就是为了这个。
   > 占位要占得**显眼**:文件名或内容里带 `EXAMPLE`/`TODO`,别做成"看起来对"的假数据。一份像模像样的错表,比明摆着的空表危险 —— 前者会被当成真的用下去。

### 产物溯源

每个产物应带一份 `PROVENANCE.json`,记**两条版本轴**(§2.2)+ 源数据与时间:

```json
{
  "bbs_tag": "v02.08.02.60",
  "bbs_slic3r_version": "02.08.02.60",
  "bbs_profile_version": "02.08.00.04",
  "pi_map_version": "<pi_product_map.json 的 sha256 或 commit>",
  "tool_version": "<build_preset.py 版本>",
  "base_data_id": "<preset-db 里的记录标识>",
  "built_at": "2026-09-12T..."
}
```

> 两条版本轴的具体值(`02.08.02.60` / `02.08.00.04`)取自 **02.08.02.60**。注意 `bbs_profile_version` 与 pin 的 tag **不是同一个数** —— master 上 `BBL.json` 是 `02.08.00.05`,pin 的版本是 `02.08.00.04`。两个都要记,别只记 tag。
>
> 再加 `pi_map_version` 和 `tool_version`:产物数值同时受这两者影响,只记 BBS 版本不足以复现。

用户报障时,"你这份文件是对着哪个 BBS 版本构建的"是第一个要问的问题。

### 建议的脚本顺序

先 **validate**(只读、无依赖、能立刻对现有样本文件跑),再 **build**(要先解决继承链求解)。validate 先落地还能反过来给 build 当验收工具。

---

## 5.5 未来:OrcaSlicer 的边界

**决定**:现阶段一份基础数据只出一份产物(BBS)。未来可能扩到 OrcaSlicer。

> 🔴 **OrcaSlicer 是 BambuStudio 的 fork,本文所有 `F-xx` 事实都是在 BBS 上验证的,不能假定在 Orca 上成立。**
>
> Orca 还混了 PrusaSlicer 的血统,`include`、`inherits`、键集合、导入入口都可能与 BBS 不同。**扩 Orca 时必须重跑一遍 §1 的核查,不能抄结论。**

因此现在就要做的两件事(成本很低,返工成本很高):

1. **规则编号加切片器前缀。** 本文的 R-01..R-15 应理解为 `R-BBS-01..R-BBS-15`;Orca 另起一套 `R-ORCA-*`,允许同名规则结论不同。
2. **构建脚本留出适配层边界。** `build_preset.py` 的内部结构应是 `求解基准 → 叠加我们的值 → 按切片器成形`,第三段是唯一需要按切片器分叉的部分。**现在不写 Orca 代码,但不要把 BBS 的成形逻辑焊死在主流程里。**

---

## 6. 你问的三个问题

### Q-A `filament_settings_id` 被什么消费?

不是切片参数,是**"这份配置指向哪个耗材预设"的名字标签**。三处消费:

1. **导入时的类型识别** —— `config.has("filament_settings_id")` 决定它被当成耗材预设([PresetBundle.cpp:1132](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1132))。这是 R-02 的来源。
2. **打开 3MF/工程文件时按名字回查预设** —— [PresetBundle.cpp:3898-3975](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L3898-L3975)、`validate_presets`（[PresetBundle.cpp:1368](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1368)）。
3. **自定义 G-code 里的 `[filament_preset]` 占位符** —— [PrintApply.cpp:1513](../../BambuStudio/src/libslic3r/PrintApply.cpp#L1513)。切片时会被替换成这个名字。

> 推论:R-02 的"非空"是硬要求,但**值本身**只在第 2、3 条被用到。值写错不会导致导入失败,只会让 3MF 回查和 gcode 占位符对不上。建议值 = 最终 `name`。

### Q-B `filament_id` 在切片软件哪里被消费?

**它不是切片参数,是"耗材小类"标识**(类似 AMS 的 `tray_info_idx`)。注册表里**没有**这个配置项 —— JSON 里的 `filament_id` 只走 `key_values` 旁路,成为 `Preset` 的元数据,不进 config。三处消费:

1. **实测冲洗量查表** —— 按 `(from_filament_id, to_filament_id)` **有向对**查 `resources/flush/flush_data_material_pair.json`,命中则取 **`min(max(预测值, 实测值), 上限)`** —— 是**取大**,不是覆盖([FlushVolCalc.cpp:132-137](../../BambuStudio/src/libslic3r/FlushVolCalc.cpp#L132-L137))。查不到则**静默回退**到预测值,不报错。
   > ⚠️ 更正:我早前写的是"用实测值**覆盖**预测值",**错了**。这行代码上一句注释自己写的就是 `// Override with measured purge`,但实际做的是 `max` —— BBS 自己的注释和代码不一致,很容易看错。后果:实测值只能**调高**冲洗量,永远不会调低。
2. **AMS 槽位匹配** —— 换料时按 filament_id 是否一致加权([FilamentGroup.cpp:31-36](../../BambuStudio/src/libslic3r/FilamentGroup.cpp#L31-L36))。
3. **校准记录归档** —— 写进 app config([AppConfig.cpp:882](../../BambuStudio/src/libslic3r/AppConfig.cpp#L882))。

> 推论:自定义 `filament_id`(如样本的 `PMPL31`)**不会出现在 BBL 的冲洗量表里** → 该耗材的换料冲洗量永远走预测值。若要利用实测表,得用 BBL 已有的 id;否则接受这个回退(不影响正确性,只影响冲洗量精度)。

**补充实测**:

- **⚠️ 表是"几乎空的"。** 02.08.02.60 里 `pairs` 只有 **4 条**:

  ```json
  {"from": "GFG00", "to": "GFA00", "purge": 650}
  {"from": "GFA00", "to": "GFG00", "purge": 650}
  {"from": "GFB00", "to": "GFN08", "purge": 400}
  ...
  ```

  也就是说**「沿用 BBL 的 `GF**` 就能吃到这张实测冲洗量表」这个好处,今天基本是 0** —— 你的 `PolyLite PLA`(`GFL00`)跟谁配对都不在这 4 条里。
  但**位子是对的**:文件自己的注释写着 *"Update this file to refine values without recompiling"*,BBL 显然打算往里加数据。所以这个好处是**未来可能兑现**,不是现在。**所以 V4-Q10' 放弃这个好处,代价比看起来小** —— 它本来就还没兑现。
- 表里的 id **全部是 `GF**`** —— 再次确认:`PMxx`(旧)和 `<前缀>+PI code`(新)**都永远查不到**。这不是新决定引入的损失。
- `filament_id` **只写在 `@base` 预设上**,按机型的预设一个都不带 —— 所以**不能从直接父预设读 id**。有 `inherits` 时父预设的值会覆盖你的(§2.4);展平后你写什么就是什么。
- 更正我早前的一个错判:我说过"BBL 不给第三方品牌分配独立 id 空间",**错了**。BBL 确实给第三方分配,而且 SUNLU 拿到的是嵌品牌码的独立块 `GFSNL02-08`。完整分布在 §2.4。

### Q-C 没有 `inherits` 时,没写到的键怎么处理?

**落到全局默认值,不是某个耗材预设的值。**

加载路径([Preset.cpp:1466-1472](../../BambuStudio/src/libslic3r/Preset.cpp#L1466-L1472)):

```cpp
preset.config = default_preset.config;   // ← "Default Filament"
preset.config.apply(std::move(config));  // ← 文件里的键覆盖上去
```

`default_preset_for()` 对耗材集合**没有重载**([Preset.hpp:617](../../BambuStudio/src/libslic3r/Preset.hpp#L617)),直接返回 `default_preset()` = **"Default Filament"**([PresetBundle.cpp:363](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L363)),它是 `Preset::filament_options()` 的键 × `FullPrintConfig::defaults()` 的值。

**所以**:

| 键的情况 | 结果 |
|---|---|
| 文件里写了 | 用文件的值 ✅ |
| 文件里没写 | 用**全局默认值**(如 `filament_flow_ratio = 1`)—— 不是 Generic PLA 的值 ⚠️ |

> 这正是 B-02 必须存在的理由:**展平必须写全所有生效键**,否则没写的键会掉到全局默认 —— 例如把 PLA 预设展平却漏写 `nozzle_temperature`,它会变成全局默认而不是 230。具体数值建议用样本实测一次确认。

---

## 7. 决策记录与待办

### 已决策(2026-09-12)

| # | 问题 | 结论 | 落在 |
|---|---|---|---|
| V4-Q1 | 下发形态选 A 还是 B | **方案 B**:展平根预设,不带 `inherits` | §2.1 |
| V4-Q2 | 展平基准值从哪来 | **构建时引用切片软件官方 profile 集**,不同步进自己 repo | §2.2 |
| V4-Q3 | 目标版本谁 pin | **CI 自动处理**,更新频率待定;pin 入库、只上报不自动 bump | §5 |
| V4-Q4 | 一份基础数据出几份产物 | 现阶段**一份**(BBS);未来可能扩 Orca | §5.5 |
| V4-Q5 | 首个 pin 定哪个 release tag | **`v02.08.02.60`** | §5 |
| V4-Q6 | `filament_id` 写什么 | ~~**沿用 BBL**~~ → **被 V4-Q10' 取代** | §2.4 |
| V4-Q7 | `name` / `filament_settings_id` 怎么生成 | **两者相等**;格式 `<产品名>@<品牌> <机型>`;需要 PI code → 产品名映射表(**example 起步**,见 V4-Q14) | §2.4 |
| V4-Q8 | 基础数据里的"继承预设"是不是展平起点 | **是**;构建从基础数据校验开始,最重要的是 `inherits` 是否存在 | §3.5 |
| V4-Q9 | 撞名策略 | **(a) 暂定不出产物**;排除集从 pin 推导,策略可切换 | §2.4 |
| V4-Q10 | 非 BBL 覆盖产品的 `filament_id` | ~~**保留 `PMxx`**~~ → **被 V4-Q10' 取代** | §2.4 |
| V4-Q11 | `PMxx` 与 PI code 是不是同一套 | ~~**不是**;映射尚不确定 → id 必须随基础数据来~~ → **问题本身被消解**(不再用 `PMxx`,也不再用映射) | §3.5 V-06 |
| V4-Q12 | 那 13 个坏文件怎么办 | **重生成**(13 个撞名会自动消失;先前记的 1 个继承缺失实测不可复现,已撤) | §3.6 |
| **V4-Q10'** | **`filament_id` 到底写什么** | **`"J" + PI code`**(前缀由 V4-Q16 定为 `J`),产品级一一对应;不再维护 `PMxx` 表。BBL 已覆盖的那 5 个 PolyLite/PolyTerra 产品**也走同一规则**(不再沿用 `GF**`) | §2.4 |
|  | ↑ 采纳你自己的提案 | 理由(同 id 共享基础信息 / 与 BBL 零交集 / 零映射表 / 便于将来上 id 块)逐条对上了实测 | |
| **V4-Q13** | **`PMxx` 还留不留** | ✅ **不再用 `PMxx`**。68 个 id 由 PI code 派生,那套表连同"谁维护"的问题一起作废 | §2.4 |
| **V4-Q16** | **前缀定 `J`** | ✅ `filament_id` = **`"J" + PI code`** | §2.4 |
| **V4-Q17** | **同料异名要不要排除** | ✅ **不排除,并存**;同名的仍走决策 9。两条路分开 | §2.4 |
| **V4-Q10b** | 要不要向 BBL 申请 `GFPM**` id 块 | ✅ **暂不申请**。`J`+PI 即终态,保留"将来可换前缀"作为退路但不排期 | §2.4 |
| **V4-Q14** | PI code → 产品名映射表 | ✅ **先起一个 example**(明显占位、3–5 行),让构建走真实读取路径;接口当真,数据后补 | §5 |
| **V4-Q15** | 真实数据什么时候上 | ✅ **本轮不上**。只验收 `build` + `validate` 两个脚本能跑通;真实数据等一切就绪后由你导入 | §5 |

> **这一轮的验收标准因此变了**(V4-Q15):不是"产物可发布",而是"**脚本跑得通**" —— manifest 解析对、V/R 档会报错也会放行、退出码正确、快照能比对。详细的对照表在 §5。

### 待办(已无待决策项)

**六个编号一次性清空** —— Q10b / Q13 / Q14 / Q15 / Q16 / Q17 全部落地,见上表。剩下的不是"待你回答"的问题,而是**等数据**:

| 事项 | 卡在哪 | 谁 |
|---|---|---|
| `pi_product_map.json` 填真数据 | 需要 PI code → 产品名的权威对照;起点可从现有 487 个产物的目录名 + `name` 字段反推 | 你,等一切就绪 |
| 202 个"已展平"文件里的手工改动对账 | 进真实数据轮前必须做,否则重生成会静默丢值(§3.6) | 你 + 构建脚本的 diff |
| 13 个撞名文件重生成 | 真实数据轮执行;排除集已确定要按 manifest 建(§2.2) | 构建脚本 |


> **阻塞性说明**:这些**都不阻塞本轮**。本轮只要有能触发各类规则的**人造样本**就能跑完 —— 而且现在做正是时候,因为校验脚本的"该报错时会不会报错"只能在有意的坏样本上验出来。等真实数据到位再写校验脚本,就等于拿一个**没测过会不会红**的门去挡真实数据。

---

## 8. 本轮(第二轮)更正汇总

这一轮我改掉的**我自己先前说错的**东西,单列一节,方便你只看差异:

| # | 先前说法 | 实测更正 | 影响 |
|---|---|---|---|
| 1 | 系统预设集 = 扫 `filament/**` | **必须走 `BBL.json` 的 manifest**;顶层扫描漏 146 个真实加载的预设 | §2.2。B-12 / V 档 / R-01 三处提取方式都要改 |
| 2 | 撞名 **10** 个 | **13** 个(漏了 `filament/P1P/` 2 个、`filament/Polymaker/` 1 个) | §2.4、§3.6。P1P 机型从"出产物"变成"被排除" |
| 3 | `inherits` 查不到 **1** 个 | **0**,当前**不可复现** | §3.6。那条待办撤销,V-02 仍然保留 |
| 4 | `GFA00` 被 42 个预设共享 | **`GFA00`/`GFA01` 各 43 个**;`@base` 100 个、运行时可解析 2028 个 | §3.2 R-12 |
| 5 | 第三方 id 有"专门的块" | **只有 SUNLU 是块**(`GFSNL0x`);Polymaker/eSUN/Overture 都是**插空、不成块、不连号** | §2.4。`GFPM**` 申请无先例支撑 |
| 6 | `alias` 清空 → AMS 显示空白名 | **说得太重**:一半 UI 用 `force=true` 会兜底,只有直接用 `.alias` 成员的才空白 | §3.2 R-13。结论保留(仍值得挡),理由改写 |
| 7 | — | **新增发现**:Fiberon 6 个产品在 H2D 上与 BBL 产品级重叠,名字不同,R-01 挡不住 | §2.4、**V4-Q17** |
| 8 | V-06 = "`len("J"+PI) != 8`" | **规则本身写错了**:真实 PI code `L2002CF` 恰好 7 字符 → 会被误杀。改成**联合条件**(`len==8 && 首字符=='P'`) | §2.4、§3.5。建 example 表取真实键时才暴露 —— 这就是为什么 example 的键要用真的 |

> 第 1、2 两条**同源** —— 索引建错导致下游结论错。这也是为什么 §3.4 要加"失败必须指到具体上游文件":否则校验器报错时,你分不清是产物错还是索引错。
