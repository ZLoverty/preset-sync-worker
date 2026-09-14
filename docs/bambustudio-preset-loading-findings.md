# BambuStudio 预设装载与导入 —— 源码核查记录

> 目的：我们下发给用户的 preset 最终要经过 BambuStudio 的装载/导入逻辑。这份文档把该逻辑的**行为事实**逐条列出，每条都带源码行号、复现命令、以及对本项目的影响判断。
>
> 用法：每条 `F-xx` 是一个**可独立验证的断言**。请在「反馈」一栏标注 `确认 / 驳回 / 部分确认`，驳回的条目我再补证或撤回。**不要基于未确认的条目改代码。**

---

## 1. 核查口径

| 项 | 值 |
|---|---|
| 源码仓库 | `/Users/zhengyang/Documents/GitHub/BambuStudio` |
| commit | `66e405477604cf9346ea7aa9e439a7854a01029d` (`v02.08.02.61-14-g66e405477`) |
| `SLIC3R_VERSION` | `"02.08.02.61"`（[version.inc:16](../../BambuStudio/version.inc#L16)） |
| 安装态 | `/Applications/BambuStudio.app`（用于核对随包分发资源） |
| 样本文件 | `/Users/zhengyang/Downloads/PolyLite PLA-CF @Bambu Lab A2L 0.4 nozzle.json` |
| 方法 | 读源码定位行号；写静态脚本对 `resources/profiles/BBL/**` 与选项注册表做统计。**未编译运行 BambuStudio** |

> 源码路径统一写相对于 BambuStudio 仓库的路径。本文档位于 `preset-sync-worker/docs/`，跨仓库链接用 `../../BambuStudio/...`。

**本项目的语境**：worker 产**基础数据 JSON**（标量、仅含关心的键）→ 构建脚本转成切片器形态 → 用户下载导入。所以本文只关心 **BambuStudio 侧**的行为，不涉及 PrusaSlicer。

---

## 2. 结论速览

| # | 一句话结论 | 组 | 置信度 |
|---|---|---|---|
| F-01 | 导入（一次性）与启动扫描（每次）是两条不同代码路径，判定不一致 | A | 高 |
| F-02 | `version` 必须能被 semver 解析，否则导入静默失败 | A | 高 |
| F-03 | 版本门禁**只有上界**（`maj() >`），没有下界 | A | 高 |
| F-04 | **未注册的键被静默丢弃，不报错、不中断解析** | A | 高（有实证） |
| F-05 | 未注册键**不会**弹任何替换提示 | A | 高 |
| F-06 | `include` 对用户预设是 **no-op**（静默丢弃，不抛异常） | A | 高 |
| F-07 | `inherits` 找不到父预设 → 静默失败 | A | 高 |
| F-08 | 导入后唯一的用户可见信号是结束弹窗，计数 = **成功导入数** | A | 高 |
| F-09 | 与系统/默认预设重名 → 拒绝导入 | A | 高 |
| F-10 | 类型识别靠 `filament_settings_id`，**不靠** `type` 字段 | A | 高 |
| F-11 | 合法的 filament 键集合只有 **~152** 个，不是全局注册表的 760 个 | B | 高 |
| F-12 | `filament_id` 导入时生效，但**每次启动被父预设覆盖** | B | 高 |
| F-13 | `setting_id` 在本地下发路径 **inert**；在云端路径 **必需** | B | 高 |
| F-14 | 样本文件：120 个配置键中 4 个未注册，其余全部合法 | B | 高 |
| F-15 | 样本文件有 3 个键写成了**标量**而非数组，形态不一致 | B | 高 |
| F-16 | `remove_invalid_keys` 是**第二道**静默删除，只写日志 | B | 高 |
| F-17 | **拖拽 `.json` 完全无效**；唯一入口是 File → Import → Import Configs | C | 高 |
| F-18 | `.zip`/`.bbscfg`/`.bbsflmt` 是 zip 容器，逐个解出后走同一路径 | C | 高 |
| F-19 | `--load-filaments` **不能**替代导入验收（且父预设解析必然失败） | C | 高（后果为推断） |
| F-20 | `.info` sidecar 由 app 管理，不是我们的产物 | C | 中 |

---

## 3. 逐条发现

### A 组：装载流水线（决定"装不装得上"）

---

#### F-01 导入与启动扫描是两条不同路径，判定不一致

**结论**
- **导入**（File → Import → Import Configs）走 `PresetBundle::import_json_presets`，只跑一次。
- **启动扫描**走 `PresetCollection::load_presets`，每次启动都跑。
- 两者对**版本号**的判定不同：导入路径的 major 检查**被注释掉了**，启动路径的**没有**。

**证据**
- 导入路径注释掉的检查：[PresetBundle.cpp:1122-1125](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1122-L1125)
  ```cpp
  /*if (version->maj() > app_version.maj()) {
      BOOST_LOG_TRIVIAL(warning) << __FUNCTION__ << " Preset incompatibla, not loading: " << name;
      return false;
  }*/
  ```
- 启动路径的检查是活的：[Preset.cpp:1428-1432](../../BambuStudio/src/libslic3r/Preset.cpp#L1428-L1432)
  ```cpp
  if ( version->maj() >  app_version.maj()) {
      BOOST_LOG_TRIVIAL(warning) << "Preset incompatibla, not loading: " << name;
      continue;
  }
  ```

**后果**：`version` 的 major 高于用户 app 的 major 时，会出现**「导入成功 → 重启后消失」**。这是本地下发场景里最难排查的一类故障。

**自行验证**
```bash
cd /Users/zhengyang/Documents/GitHub/BambuStudio
grep -n "Preset incompatibla" src/libslic3r/PresetBundle.cpp src/libslic3r/Preset.cpp
```

**对本项目的影响**：生成器写 `version` 时必须写**目标 app 的版本**，不能写"未来版本"或自造版本号。

**反馈** 确认

---

#### F-02 `version` 必须可被 semver 解析，否则导入静默失败

**结论**：`version` 缺失或无法解析 → 导入函数直接 `return false`，**无任何用户提示**。

**证据**
- [PresetBundle.cpp:1117-1120](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1117-L1120)
  ```cpp
  std::string version_str = key_values[BBL_JSON_KEY_VERSION];
  boost::optional<Semver> version = Semver::parse(version_str);
  if (!version) return false;
  ```
- 启动扫描同样：解析失败直接 `continue`（[Preset.cpp:1426-1427](../../BambuStudio/src/libslic3r/Preset.cpp#L1426-L1427)）。
- 解析规则（[semver.c:175-215](../../BambuStudio/src/semver/semver.c#L175-L215)）：**至少两段**数字，最多取四段；**第 4 段会被折进 patch**：`patch = patch*100 + 第4段`。所以 `"02.08.02.61"` 的 patch 是 `261`，不是 `2`。

**自行验证**
```bash
sed -n '175,215p' /Users/zhengyang/Documents/GitHub/BambuStudio/src/semver/semver.c
```

**对本项目的影响**：`version` 是**必填**键，且格式必须是 `AA.BB.CC` 或 `AA.BB.CC.DD`。

**反馈**：☐ 确认 ｜ 备注：实测格式可以是 a.b.c.d 或 a.b.c 或 a.b, 单个数字 a 时才无法导入。

---

#### F-03 版本门禁只有上界，没有下界

**结论**：全仓库对预设版本的比较**只有** `maj() > app.maj()` 一种。**不存在** `maj() <` 的检查。所以任何 `maj` 小于等于 app 的版本都会通过，包括 `0.x`。

**证据**：`grep -rn "\.maj()"` 的全部结果：

| 位置 | 用途 |
|---|---|
| [Preset.cpp:1429](../../BambuStudio/src/libslic3r/Preset.cpp#L1429) | 启动扫描门禁（上界） |
| [Preset.cpp:1956](../../BambuStudio/src/libslic3r/Preset.cpp#L1956) | 云端用户预设门禁（上界） |
| [PresetBundle.cpp:1122](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1122) | 导入门禁（**已注释**） |
| [Plater.cpp:8462](../../BambuStudio/src/slic3r/GUI/Plater.cpp#L8462) | 3MF 版本提示（上界） |
| [Plater.cpp:8532](../../BambuStudio/src/slic3r/GUI/Plater.cpp#L8532) | 3MF "非 BBL 文件"启发式：`maj()==0 && min()==0 && patch()==0` |
| [SendSystemInfoDialog.cpp:189-190](../../BambuStudio/src/slic3r/GUI/SendSystemInfoDialog.cpp#L189-L190) | 遥测上报，与门禁无关 |

**注意** [Plater.cpp:8532](../../BambuStudio/src/slic3r/GUI/Plater.cpp#L8532)：`maj()==0` 被用作"这不是 Bambu Lab 生成的文件"的判据 —— 但**仅限 3MF 路径**，配置导入路径不受影响。

**自行验证**
```bash
cd /Users/zhengyang/Documents/GitHub/BambuStudio
grep -rn "\.maj()" src/libslic3r/*.cpp src/slic3r/GUI/*.cpp
```

**对本项目的影响**：版本号写低了**不会**被拦，但也**不携带任何兼容性信息**。建议仍写真实目标版本，便于排查。

**反馈**：☐ 确认 ｜ 备注：

---

#### F-04 未注册的键被静默丢弃，不报错、不中断解析 ⭐

**结论**：文件里出现 BambuStudio 不认识的键时，该键被**清空并记录为 `unrecogized_keys`，然后跳过，解析继续**。不抛异常，不会导致后续键丢失。

> ⚠️ **这条是我先前判断错误的更正点**，见 §4。它是整个校验方案里最需要重视的一条，因为它**只在下游日志里留痕，用户和我们都看不到**。

**证据链（四环，缺一不可）**

1. 兜底清空 —— [PrintConfig.cpp:7411-7414](../../BambuStudio/src/libslic3r/PrintConfig.cpp#L7411-L7414)（`PrintConfigDef::handle_legacy` 末尾）
   ```cpp
   if (! print_config_def.has(opt_key)) {
       opt_key = "";
       return;
   }
   ```
2. 空键名 → 记 `unrecogized_keys` 后 `return true`，**不抛** —— [Config.cpp:603-611](../../BambuStudio/src/libslic3r/Config.cpp#L603-L611)
3. `throw UnknownOptionException` 确实存在（[Config.cpp:648](../../BambuStudio/src/libslic3r/Config.cpp#L648)），但它在兜底**之后**，该分支**不可达**
4. **实证**：`resources/profiles/BBL/filament/fdm_filament_common.json` 含三个**未注册**键 —— `filament_ingredients_safe` / `filament_emission_safe` / `filament_contact_safe`。而 `BBL.json` 里 **1928 个可实例化 filament 预设，1928 个**的 `inherits` 链都以它为祖先。

> 若未注册键会中断解析，BBL 一个耗材预设都装不上 —— 事实显然相反。

**自行验证**
```bash
cd /Users/zhengyang/Documents/GitHub/BambuStudio
python3 docs-repro/check_keys.py          # 见附录 A.1
```

**对本项目的影响**：**校验方案的第一优先级**就是「每个键都必须在注册表里」。我们的生成器一旦写错键名（typo、或从别的切片器抄来的键），该参数会**彻底失效且无人知晓**。

**反馈**：☐ 确认 

---

#### F-05 未注册键不会弹任何替换提示

**结论**：`unrecogized_keys` **不进入** `ConfigSubstitutions` 的返回值，因此导入路径的 `if (!config_substitutions.empty())` 判定看不到它 → **不弹窗**。

**证据**
- 导入路径只检查 substitutions：[PresetBundle.cpp:1202-1203](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1202-L1203)
- 启动扫描同理：[Preset.cpp:1412](../../BambuStudio/src/libslic3r/Preset.cpp#L1412)
- 全仓库 `unrecogized_keys` 只在 `PresetBundle.cpp:4811-4815` 被合并、`Plater.cpp:8503-8508`（3MF 开发者模式）被展示。

**自行验证**
```bash
grep -rn "unrecogized_keys" /Users/zhengyang/Documents/GitHub/BambuStudio/src/
```

**对本项目的影响**：**不能指望用户截图报错**。用户只会说"参数没生效"。所以键名校验必须在**我们出包前**完成。

**反馈**：☐ 确认 

---

#### F-06 `include` 对用户预设是 no-op

**结论**：系统预设的 `include` 语义**完全不适用于**用户预设。用户预设里出现 `include`，会被当作**未注册键**走 F-04 丢掉。

> ⚠️ 这条也是更正点：我先前说 `include` 会抛异常并截断解析，**是错的**。

**证据**
- `include` 只在 `!load_inherits_to_config` 时被分流到 `key_values`：[Config.cpp:951-956](../../BambuStudio/src/libslic3r/Config.cpp#L951-L956)
  ```cpp
  else if (!load_inherits_to_config && boost::iequals(it.key(), BBL_JSON_KEY_INCLUDES)) {
      key_values.emplace(BBL_JSON_KEY_INCLUDES, it.value().dump());
  }
  ```
- 用户路径的两参重载**硬编码传 `true`**：[Config.cpp:841](../../BambuStudio/src/libslic3r/Config.cpp#L841)
  ```cpp
  ret = load_from_json(file, substitutions_ctxt, true, key_values, reason);
  ```
- `include` 不在注册表中（附录 A.1 输出 `registered=False`）→ 走 F-04 丢弃。
- 真正消费 `include` 的只有 vendor 装载器：[PresetBundle.cpp:4861](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L4861)、[5111](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L5111)。

**自行验证**
```bash
cd /Users/zhengyang/Documents/GitHub/BambuStudio
grep -n "BBL_JSON_KEY_INCLUDES" src/libslic3r/Config.cpp src/libslic3r/PresetBundle.cpp
```

**对本项目的影响**：**生成器绝不能输出 `include`**。若照抄系统预设的写法，模板那一层带来的全部数值会**整层消失**，而且没有任何报错。

**反馈**：☐ 部分确认 ｜ 备注：写了 include 似乎没有影响导入的值，只是 include 内容并没有生效。

---

#### F-07 `inherits` 找不到父预设 → 静默失败 ⭐

**结论**
- **导入时**：父预设找不到 → `return false`，文件不进 `result`（= 用户看到的计数里没有它）。
- **启动扫描时**：父预设找不到 → `continue`，**文件留在磁盘上，但列表里没有**。

**证据**
- 导入：[PresetBundle.cpp:1174-1179](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1174-L1179)
  ```cpp
  if (inherits_config2 && !inherits_config2->value.empty()) {
      BOOST_LOG_TRIVIAL(warning) << ... << ", can not find inherit preset for user preset %1%, just skip" % name;
      return false;
  }
  ```
- 启动：[Preset.cpp:1462-1466](../../BambuStudio/src/libslic3r/Preset.cpp#L1462-L1466)
  ```cpp
  BOOST_LOG_TRIVIAL(error) << ... "can not find parent %1% for config %2%!" ...
  continue;
  ```

**自行验证**
```bash
grep -n "can not find parent\|can not find inherit preset" \
  /Users/zhengyang/Documents/GitHub/BambuStudio/src/libslic3r/Preset.cpp \
  /Users/zhengyang/Documents/GitHub/BambuStudio/src/libslic3r/PresetBundle.cpp
```

**对本项目的影响**：🔴 **这是下载分发模式的头号风险**。父预设名（如 `Generic PLA @BBL A2L`）与 BambuStudio 版本强耦合：用户装了旧版 BBS，父预设不存在 → 导入被静默拒绝。**我们必须按 BBS 版本分档管理父预设名，并给每个档位准备兜底父预设。**

**反馈**：☐ 部分确认 ｜ 备注：inherits 不存在实测还是可以导入（已经把 inherits 展平的 preset 删掉 inherits 键），再看看有什么其他约束；

---

#### F-08 导入后唯一的用户可见信号是结束弹窗，计数 = 成功导入数

**结论**：`import_presets` 结束时执行 `files = result;`，把入参**原地替换**成"成功导入的文件列表"。`MainFrame` 随后用 `cfiles.size()` 拼弹窗文案 —— 所以**弹窗里的数字是成功数，不是选择数**。

**证据**
- [PresetBundle.cpp:1098](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1098)：`files = result;`
- `result.push_back(file)` 只在**完整成功路径**上（[PresetBundle.cpp:1206](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1206)）；所有 `return false` 分支和两个 catch 分支都在它之前。
- 弹窗：[MainFrame.cpp:4033-4036](../../BambuStudio/src/slic3r/GUI/MainFrame.cpp#L4033-L4036)
  ```cpp
  MessageDialog dlg2(this, wxString::Format(_L_PLURAL("There is %d config imported. (Only non-system and compatible configs)", ...), cfiles.size()), ...);
  ```

> ⚠️ 文案里的 **"(Only non-system and compatible configs)"** 中 "compatible" 在导入路径里**找不到对应检查** —— 文案与实现不符，属上游遗留。

**对本项目的影响**：**用户可见的失败信号 = 弹窗计数变小。** 官方安装指引里要写明："导入后弹窗显示的数字应等于你选的文件数；若为 0 或偏小，说明该文件未能导入。"

**反馈**：☐ 确认 

---

#### F-09 与系统/默认预设重名 → 拒绝导入

**证据**：[PresetBundle.cpp:1139-1143](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1139-L1143)
```cpp
if (auto p = collection->find_preset(name, false)) {
    if (p->is_default || p->is_system) {
        BOOST_LOG_TRIVIAL(warning) << ... "Preset already present and is system preset, not loading: " << name;
        return false;
    }
```
（与已有**用户**预设重名则会弹 `ConfigsOverwriteConfirmDialog` 让用户选。）

**对本项目的影响**：`name` 命名要避开所有系统预设名。样本用的 `PolyLite PLA-CF @Bambu Lab A2L 0.4 nozzle` 这种「品牌+型号 @机型 喷嘴」格式是安全的。

**反馈**：☐ 确认 ｜ 备注：name 与系统预设撞车确实无法导入。

---

#### F-10 类型识别靠 `filament_settings_id`，不靠 `type` 字段

**结论**：导入路径用 `config.has("filament_settings_id")` / `print_settings_id` / `printer_settings_id` 判断类型。文件顶部那个 `"type": "filament"` 在这里**不被读取**（它进了 `key_values` 但没人消费）。

**证据**：[PresetBundle.cpp:1127-1137](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1127-L1137)
```cpp
if (config.has("printer_settings_id"))      collection = &printers;
else if (config.has("print_settings_id"))   collection = &prints;
else if (config.has("filament_settings_id"))collection = &filaments;
if (collection == nullptr) {
    BOOST_LOG_TRIVIAL(warning) << ... "Preset type is unknown, not loading: " << name;
    return false;
}
```

**对本项目的影响**：⚠️ 与 `demands-v3.md` 决策 #9（「不写 `filament_settings_id`」）**存在潜在冲突**。若最终下发给用户的是**不带** `filament_settings_id` 的文件，导入会直接失败。
需要区分两个形态：worker 产出的 **preset-db 基础数据 JSON**（不写，符合决策 #9）vs 构建脚本产出的**用户可导入文件**（**必须写**，且必须是字符串数组）。

**反馈**：☐ 确认 ｜ 备注：filament_settings_id 不存在确实无法导入（但我不清楚这个 id 存在的意义，可以看看它被什么消费吗？）

---

### B 组：键与值（决定"装上了但值对不对"）

---

#### F-11 合法的 filament 键集合只有 ~152 个，不是全局注册表的 760 个 ⭐

**结论**：校验键名时**不能**拿 `print_config_def` 的 760 个键当白名单 —— 那样会**漏掉一整类错误**：注册了、但**不属于 filament 预设**的键（例如打印参数）。

**证据**
| 集合 | 大小 | 定义位置 |
|---|---|---|
| 全局选项注册表 | **760** | `this->add("...")` 字面量 + 两个 `add_nullable` 循环列表 |
| `s_Preset_filament_options` | **144** | [Preset.cpp:1087](../../BambuStudio/src/libslic3r/Preset.cpp#L1087) |
| `filament_options_with_variant` | **44** | [PrintConfig.cpp](../../BambuStudio/src/libslic3r/PrintConfig.cpp)（`extern` 声明在 [PrintConfig.hpp:710](../../BambuStudio/src/libslic3r/PrintConfig.hpp#L710)） |
| `filament_dev_options` | **8** | [PrintConfig.cpp:7576](../../BambuStudio/src/libslic3r/PrintConfig.cpp#L7576) |
| **并集（filament 预设合法键）** | **152** | 上述三者 + 若干 ID/条件键 |

默认预设的 config 是按 `Preset::filament_options()` 建的（[PresetBundle.cpp:363](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L363)），而 `remove_invalid_keys` 拿它做基准（F-16）—— 所以 **152 才是正确的白名单**。

**自行验证**：附录 A.1 脚本会打印这五个数字。

**对本项目的影响**：校验器的白名单取 **`s_Preset_filament_options ∪ filament_options_with_variant ∪ filament_dev_options`**，不是全局注册表。两者都要查：不在全局注册表 → F-04 丢弃；在全局注册表但不在 filament 集合 → F-16 丢弃。

**反馈**：☐ 确认 ☐ 驳回 ☐ 部分确认 ｜ 备注：

---

#### F-12 `filament_id` 导入时生效，但每次启动被父预设覆盖

**证据**
- 导入时采用文件里的值：[PresetBundle.cpp:1188-1189](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1188-L1189)
  ```cpp
  if (key_values.find(BBL_JSON_KEY_FILAMENT_ID) != key_values.end())
      preset.filament_id = key_values[BBL_JSON_KEY_FILAMENT_ID];
  ```
- 启动扫描时**无条件**用父预设的覆盖：[Preset.cpp:1457](../../BambuStudio/src/libslic3r/Preset.cpp#L1457)
  ```cpp
  preset.filament_id = inherit_preset->filament_id;
  ```

**对本项目的影响**：自定义 `filament_id` 只活到下一次重启。**不要依赖它做标识**；AMS 侧的耗材识别另有机制。

**反馈**：☐ 确认 ☐ 驳回 ☐ 部分确认 ｜ 备注：filament_id 在切片软件的哪里被消费？

---

#### F-13 `setting_id` 在本地下发路径 inert，在云端路径必需

**结论**（对 §4 更正的补充限定）
- **本地文件导入 / 启动扫描**：`setting_id` **不被读取**。它进 `key_values`，但没有消费方。真正的 id 存在 `.info` sidecar 里，由 cloud sync 申请。
- **云端用户预设**：`setting_id` **必需**，缺失直接 `return false`（[Preset.cpp:1961-1965](../../BambuStudio/src/libslic3r/Preset.cpp#L1961-L1965)）。

**证据**：`BBL_JSON_KEY_SETTING_ID` 的全部消费点 —— `update_system_preset_setting_ids`（[PresetBundle.cpp:1304](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1304)）、vendor 装载（[4824](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L4824)/[5074](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L5074)）、云端（[Preset.cpp:1962](../../BambuStudio/src/libslic3r/Preset.cpp#L1962)）。**用户文件导入与启动扫描均不在列。**

**自行验证**
```bash
grep -rn "BBL_JSON_KEY_SETTING_ID" /Users/zhengyang/Documents/GitHub/BambuStudio/src/
```

**对本项目的影响**：本地下发场景下写不写都行（样本里的 `GFSL99_24` 是死数据）。**若将来走 Bambu 云同步分发，则必须写。**

**反馈**：☐ 确认 ☐ 驳回 ☐ 部分确认 ｜ 备注：实测 setting_id 缺失仍可导入。尚不知如何测试云同步。

---

#### F-14 样本文件的键核查结果

**结论**：`PolyLite PLA-CF @Bambu Lab A2L 0.4 nozzle.json` 共 128 个顶层键，其中元数据 8 个、配置键 120 个：

| 类别 | 数量 | 键 |
|---|---|---|
| ✅ 合法（在 152 键集合内） | **116** | — |
| ❌ 不在全局注册表（→ F-04 静默丢弃） | **4** | `idle_temperature`、`filament_ingredients_safe`、`filament_emission_safe`、`filament_contact_safe` |
| ⚠️ 已注册但不属于 filament 集合（→ F-16 丢弃） | **0** | — |

**关于那 4 个键，要分开看**：
- `filament_ingredients_safe` / `filament_emission_safe` / `filament_contact_safe` —— **BBL 自己也在用**（就在 `fdm_filament_common.json` 里）。它们是"已知的未知键"，丢弃无害，与我们无关。
- `idle_temperature` —— **在整个 BBL profile 集里都不存在**。这是**我们生成器自己产出的死键**，来源待查（见 §6 Q3）。

**另外注意**：样本文件的 `version` 是 **`"0.7.0.8"`**（第 365 行），major = 0。按 F-03 它**能通过**门禁（`0 > 2` 为假）。但它与 BBS 的版本谱系（`02.08.xx.xx`）完全对不上，会让排查失去参照。

**自行验证**：附录 A.1。

**反馈**：☐ 部分确认 ｜ 备注：如果没有 inherits，且键数没有覆盖到注册的 152 个键，剩下的键会被如何处理？

---

#### F-15 样本文件有 3 个键写成了标量而非数组

**结论**：文件里几乎所有键都是 `["值"]` 形态，但这三个是**裸字符串**：

```json
"filament_ingredients_safe": "1",
"filament_emission_safe": "1",
"filament_contact_safe": "1",
```
（[样本文件:256-258](../../../../Downloads/PolyLite%20PLA-CF%20@Bambu%20Lab%20A2L%200.4%20nozzle.json)）

**为什么现在无害**：它们是未注册键，形态**无人校验**，在 F-04 就被丢掉了。

**为什么仍要修**：说明生成器的「字符串数组化」规则有**漏网分支**。如果同样的分支在某个**已注册**键上也触发，那个键就会被静默替换成默认值（`ConfigBase::diff` / `set_deserialize` 对形状不符的处理）。

**对本项目的影响**：构建脚本要保证**所有配置键一律数组化**，不能有例外。

**反馈**：☐ 确认 

---

#### F-16 `remove_invalid_keys` 是第二道静默删除

**结论**：装载流程在解析之后还会删掉「不在默认预设 config 里」的键，**只写 error 日志**。

**证据**：[Preset.cpp:486-503](../../BambuStudio/src/libslic3r/Preset.cpp#L486-L503)
```cpp
std::string Preset::remove_invalid_keys(DynamicPrintConfig &config, const DynamicPrintConfig &default_config)
{
    std::string incorrect_keys;
    for (const std::string &key : config.keys())
        if (! default_config.has(key)) {
            ... // 拼接 incorrect_keys
            config.erase(key);          // ← 删除
        }
    return incorrect_keys;
}
```
调用点：[Preset.cpp:1477](../../BambuStudio/src/libslic3r/Preset.cpp#L1477)（启动扫描）、[1730](../../BambuStudio/src/libslic3r/Preset.cpp#L1730)（编辑保存）、[2078](../../BambuStudio/src/libslic3r/Preset.cpp#L2078)（云端）。

**与 F-04 的区别**：
- F-04 删的是**没注册过**的键 —— 发生在 JSON 解析阶段。
- F-16 删的是**注册了但不在 filament 键集合里**的键（如误写 `layer_height`）—— 发生在解析**之后**。

**对本项目的影响**：印证 F-11 —— 白名单必须用 filament 集合，不能用全局注册表。

**反馈**：☐ 确认 

---

### C 组：分发方式（决定"用户怎么装"）

---

#### F-17 拖拽 `.json` 完全无效

**结论**：把 `.json` 拖到 BambuStudio 窗口**什么都不会发生**（日志写 "can not find valid path, return directly"）。唯一入口是菜单 **File → Import → Import Configs**。

**证据**
- 拖拽白名单（[Plater.cpp:21609](../../BambuStudio/src/slic3r/GUI/Plater.cpp#L21609)）只接受模型与 gcode：
  ```cpp
  const std::regex pattern_drop(".*[.](stp|step|stl|oltp|obj|amf|3mf|svg|gltf|glb|fbx)", std::regex::icase);
  const std::regex pattern_gcode_drop(".*[.](gcode|g)", std::regex::icase);
  ```
- 菜单项 `_L("Import Configs")` 在 `_L("Import")` 子菜单下：[MainFrame.cpp:3008-3012](../../BambuStudio/src/slic3r/GUI/MainFrame.cpp#L3008-L3012)
- 文件对话框通配符：[MainFrame.cpp:4004](../../BambuStudio/src/slic3r/GUI/MainFrame.cpp#L4004)
  ```
  "Config files (*.json;*.zip;*.bbscfg;*.bbsflmt)|*.json;*.zip;*.bbscfg;*.bbsflmt"
  ```

**对本项目的影响**：🔴 官网/PI 说明页**必须**写「File → Import → Import Configs」，不能写"拖入窗口"。写错了用户会以为文件损坏。

**反馈**：☐ 确认 

---

#### F-18 `.zip` / `.bbscfg` / `.bbsflmt` 是 zip 容器

**结论**：这三种扩展名被当作 zip 解开，逐个条目走与单文件 `.json` **完全相同**的导入路径。`BUNDLE_STRUCTURE_JSON_NAME` 指定的清单文件会被跳过。

**证据**：[PresetBundle.cpp:1031-1096](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1031-L1096)

**对本项目的影响**：一次分发多个预设时，打包成 zip 的体验优于逐个导入（用户只选一次文件、只看一次结果弹窗）。**但注意**：F-08 的计数此时是**条目数**，用户仍然能看出有几个失败了。

**反馈**：☐ 确认 

---

#### F-19 `--load-filaments` 不能替代导入验收

**结论**（两层）
1. 它**不解析 `inherits` 链**：只读取文件里的裸值，把 `inherits` 当字符串存着。
2. 它尝试解析父预设时，路径写死为 `resources_dir()/profiles/BBL/filament_full/<inherits>.json` —— 而**这个目录在源码树和已安装的 app 里都不存在**（实测 0 个文件）。所以父预设合并被**静默跳过**。

**证据**
- [BambuStudio.cpp:2519-2531](../../BambuStudio/src/BambuStudio.cpp#L2519-L2531)
  ```cpp
  if ((config_from == "User")||(config_from == "user")) {
      inherits = config.option<ConfigOptionString>("inherits", true)->value;
      if (!inherits.empty()) {
          std::string parent_filament_path = resources_dir() + "/profiles/BBL/filament_full/"+inherits+".json";
          if (boost::filesystem::exists(parent_filament_path)) {   // ← 不存在则静默跳过
              ...
          }
      }
  }
  ```
- 实测目录缺失：
  ```bash
  ls /Applications/BambuStudio.app/Contents/Resources/profiles/BBL/filament_full/ | wc -l   # → 0
  ```

**后果（此处为推断，非直接读数）**：对 `from: User` 的预设跑 `--load-filaments`，切片用的是**只有文件里那 120 个键、没有继承值的残缺配置** → 切片结果**不代表用户实际导入后的行为**。用它做验收会得出错误的结论。

**自行验证**
```bash
ls /Applications/BambuStudio.app/Contents/Resources/profiles/BBL/ | grep filament_full || echo "filament_full 不存在"
```

**对本项目的影响**：**CLI 切片只能用于验证"值本身"，不能用于验证"导入是否被接受"。** 后者必须走 GUI 导入 + 看 F-08 的计数。

**反馈**：☐ 确认 

---

#### F-20 `.info` sidecar 由 app 管理

**结论**：用户预设 = `data_dir()/user/<preset_folder>/filament/<name>.json` **+ 同名 `.info`**。`.info` 由 app 写入并维护（含真正的 `setting_id` 等），**不是我们的产物**，我们也不应分发它。

**置信度**：中。目录布局与 `.info` 的存在已确认，但 `.info` 的完整字段集我**没有**逐字段核对。

**反馈**：☐ 确认 

---

## 4. 更正（我先前在对话中说错的三处）

| # | 我先前的说法 | 事实 | 依据 |
|---|---|---|---|
| 1 | `setting_id` 可能与系统预设 id 撞车，需要处理 | **本地下发路径下 `setting_id` 完全不被读取**，不存在撞车问题（云端路径才必需） | F-13 |
| 2 | `idle_temperature` 可能导致解析中断 / 触发替换弹窗 | **既不中断也不弹窗**，就是静默丢弃 | F-04、F-05 |
| 3 | `include` 会抛异常并截断解析 | **不会**。它按未注册键处理，静默丢弃 | F-06 |

错误根因：我最初追到 `throw UnknownOptionException`（[Config.cpp:648](../../BambuStudio/src/libslic3r/Config.cpp#L648)）就下了结论，但没注意到同一文件更上游的 `handle_legacy` 兜底（[PrintConfig.cpp:7411](../../BambuStudio/src/libslic3r/PrintConfig.cpp#L7411)）**先一步把未知键清空了**，那个 throw 分支实际不可达。F-04 里的 1928/1928 实证就是为了堵死这个错误。

**另外一处需要你核对**：样本文件的 `version`，我这轮读到的是 **`0.7.0.8`**，而我在上一轮对话中记作 `2.7.0.8`。两者都能过门禁（major 分别为 0 和 2，都不大于 2），但请你确认手上那份文件的实际值 —— 如果你手里是 `2.7.0.8`，说明文件被改过。

---

## 5. 落地：分层校验清单

按「失败是否可见」分层。**L0 必须在出包前拦住，因为它的失败用户看不见。**

### L0 致命级 —— 不满足则导入失败或预设静默消失

| 校验项 | 失败后果 | 依据 |
|---|---|---|
| `version` 存在且可被 semver 解析（≥2 段数字） | 导入 `return false`，无提示 | F-02 |
| `version` 的 `maj ≤` 目标 app 的 `maj` | 导入成功但重启消失 | F-01、F-03 |
| `filament_settings_id` 存在且为字符串数组 | 类型识别失败 → 拒绝导入 | F-10 |
| `name` 存在且不与任何系统/默认预设重名 | 拒绝导入 | F-09 |
| `inherits` 目标**在目标 app 版本的预设集里存在** | 静默拒绝 / 重启后消失 | F-07 |
| **每个配置键都在 filament 合法键集合（152）内** | 静默丢弃该参数 | F-04、F-11、F-16 |
| **绝不出现 `include`** | 整层值消失，无报错 | F-06 |
| 所有配置键**一律数组形态** | 值被替换为默认 | F-15 |

### L1 值语义级 —— 装上了但值不对

| 校验项 | 失败后果 |
|---|---|
| 数组长度与 `filament_extruder_variant` 对齐 | 形状不符 → 替换为默认 |
| 枚举值在合法集合内 | 替换为默认，并在 substitutions 里留痕 |
| 数值在 `min`/`max` 范围内 | 同上 |

### L2 可用性级 —— 用户能不能看见

| 校验项 | 失败后果 |
|---|---|
| `compatible_printers` 匹配目标机器预设名 | 不匹配 → 下拉框里被过滤掉（需勾"显示不兼容"才可见） |
| `compatible_printers` 为空 | 语义是"兼容所有机器" |

### L3 分发方式

- 安装指引写 **File → Import → Import Configs**，不写拖拽（F-17）。
- 告知用户：**导入结束弹窗里的数字应等于所选文件数**；为 0 或偏小即为失败（F-08）。
- 多文件用 zip 打包（F-18）。

### 白名单怎么建（两种口径）

1. **静态提取**（已跑通，附录 A.1）：从 `PrintConfig.cpp` / `Preset.cpp` 正则提取。已知坑：正则必须允许**大写字母**（否则 `required_nozzle_HRC` 会被误报为未注册）。
2. **从目标版本二进制 dump**（更稳，免去维护正则）：CI 里跑一次 BambuStudio 导出选项注册表键集。**尚未验证可行性**，需要评估。

---

## 6. 待你反馈的问题

| # | 问题 | 为什么需要你 |
|---|---|---|
| Q1 | 我们实际**支持的 BambuStudio 版本区间**是什么？ | 决定 F-07 的父预设兜底策略要覆盖几档 |
| Q2 | 下发给用户的最终文件里，`filament_settings_id` 到底写不写？ | `demands-v3.md` 决策 #9 说不写，但 F-10 要求必须有 —— 需明确「基础数据 JSON」与「可导入文件」是两个形态 |
| Q3 | 样本里的 `idle_temperature` 从哪来的？ | BBL profile 集里不存在这个键，是生成器自己产的死键，需定位来源 |
| Q4 | 样本 `version` 到底是 `0.7.0.8` 还是被我记错的 `2.7.0.8`？ | 见 §4 末 |
| Q5 | 是否需要**跨版本矩阵**校验（对每个支持的 BBS 版本各跑一次 L0）？ | 决定校验器是单目标还是多目标 |
| Q6 | F-20（`.info` sidecar）需要我进一步核实吗？ | 目前置信度中等 |

回答：
1. 支持当前最新主版本（2）
2. 下发给用户的文件里要写`filament_settings_id`，只是不写在基础数据里，而是在构建层完成；
3. 我也不知道，我后面会要你写一个校验脚本，这类无效键需要被过滤出来；
4. version 0.7.0.8 是我测试导入时手动改的；
5. 不需要，只校验当前主版本；
6. 不用，我不关心 sidecar

---

## 附录 A：复现脚本

> 脚本放在 `preset-sync-worker/docs-repro/` 下（**尚未创建**，等你确认结论后再落地为正式代码）。以下为可直接运行的版本。

### A.1 键核查

从 BambuStudio 源码提取三套键集合，与待发布文件比对。

```python
#!/usr/bin/env python3
"""check_keys.py <bambustudio_repo> <preset.json> [...]"""
import json, re, sys, pathlib

def brace_list(text, name):
    m = re.search(re.escape(name) + r'\s*=\s*\{(.*?)\n\};', text, re.S) \
        or re.search(re.escape(name) + r'\s*\{(.*?)\n\};', text, re.S)
    return set(re.findall(r'"([A-Za-z0-9_]+)"', m.group(1))) if m else set()

def build(bbs: pathlib.Path):
    pc = (bbs / "src/libslic3r/PrintConfig.cpp").read_text(errors="replace")
    pr = (bbs / "src/libslic3r/Preset.cpp").read_text(errors="replace")

    # 注意：必须允许大写字母，否则 required_nozzle_HRC 会被误报
    registry = set(re.findall(r'this->add\("([A-Za-z0-9_]+)"', pc))
    for lst in ("filament_extruder_override_keys", "filament_overhang_override_keys"):
        m = re.search(r'std::vector<std::string>\s+' + lst + r'\s*=\s*\{(.*?)\};', pc, re.S)
        if m:
            registry |= set(re.findall(r'"([A-Za-z0-9_]+)"', m.group(1)))

    allowed = (brace_list(pr, "s_Preset_filament_options")
               | brace_list(pc, "filament_options_with_variant")
               | brace_list(pc, "filament_dev_options")
               | {"filament_settings_id", "compatible_printers",
                  "compatible_printers_condition", "inherits", "filament_id",
                  "filament_colour", "filament_notes", "filament_extruder_variant"})
    return registry, allowed

META = {"type", "name", "from", "instantiation", "version", "description",
        "filament_id", "setting_id"}

def main():
    bbs, *files = sys.argv[1:]
    registry, allowed = build(pathlib.Path(bbs))
    print(f"registry={len(registry)}  filament_allowed={len(allowed)}\n")

    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        keys = [k for k in d if k not in META]
        unknown  = [k for k in keys if k not in registry]           # → F-04 丢弃
        wrong_scope = [k for k in keys if k in registry and k not in allowed]  # → F-16 丢弃
        print(f"{f}")
        print(f"  config keys        : {len(keys)}")
        print(f"  not registered     : {unknown}")
        print(f"  registered/wrong set: {wrong_scope}")
        print(f"  version            : {d.get('version')!r}")
        print(f"  has filament_settings_id: {'filament_settings_id' in d}")

if __name__ == "__main__":
    main()
```

运行：
```bash
python3 check_keys.py /Users/zhengyang/Documents/GitHub/BambuStudio \
  "/Users/zhengyang/Downloads/PolyLite PLA-CF @Bambu Lab A2L 0.4 nozzle.json"
```

### A.2 选项注册表规模

```bash
cd /Users/zhengyang/Documents/GitHub/BambuStudio
python3 -c "
import re
src=open('src/libslic3r/PrintConfig.cpp',errors='replace').read()
ks=set(re.findall(r'this->add\(\"([A-Za-z0-9_]+)\"',src))
for l in ['filament_extruder_override_keys','filament_overhang_override_keys']:
    m=re.search(r'std::vector<std::string>\s+'+l+r'\s*=\s*\{(.*?)\};',src,re.S)
    ks|=set(re.findall(r'\"([A-Za-z0-9_]+)\"',m.group(1)))
print(len(ks))
"
```

### A.3 `fdm_filament_common` 依赖度实证（F-04 的关键证据）

```python
#!/usr/bin/env python3
"""验证：若未注册键会中断解析，BBL 将没有任何耗材预设可用。"""
import json, os

root = "resources/profiles"
fl = json.load(open(f"{root}/BBL.json"))["filament_list"]
by = {e["name"]: e["sub_path"] for e in fl}

def load(n):
    sp = by.get(n)
    p = f"{root}/BBL/{sp}" if sp else None
    return json.load(open(p)) if p and os.path.exists(p) else None

tot = hit = 0
for e in fl:
    d = load(e["name"])
    if d and d.get("instantiation") in (False, "false"):
        continue
    tot += 1
    seen, p = set(), e["name"]
    while p and p not in seen:
        seen.add(p)
        if p == "fdm_filament_common":
            hit += 1
            break
        d = load(p)
        if d is None:
            break
        p = d.get("inherits")

print(f"instantiable filament presets: {tot}")
print(f"  chain reaches fdm_filament_common: {hit}")
```

预期输出：
```
instantiable filament presets: 1928
  chain reaches fdm_filament_common: 1928
```

---

## 附录 B：关键行号索引

| 行为 | 位置 |
|---|---|
| 导入入口（单文件） | [PresetBundle.cpp:1103](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1103) |
| 导入入口（zip bundle） | [PresetBundle.cpp:1016](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1016) |
| 导入：version 检查 | [PresetBundle.cpp:1117-1125](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1117-L1125) |
| 导入：类型识别 | [PresetBundle.cpp:1127-1137](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1127-L1137) |
| 导入：重名 / inherits / filament_id | [1139](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1139) / [1174](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1174) / [1188](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1188) |
| 导入：`files = result` | [PresetBundle.cpp:1098](../../BambuStudio/src/libslic3r/PresetBundle.cpp#L1098) |
| 启动扫描 | [Preset.cpp:1349](../../BambuStudio/src/libslic3r/Preset.cpp#L1349) 起 |
| 启动：版本门禁 | [Preset.cpp:1428-1432](../../BambuStudio/src/libslic3r/Preset.cpp#L1428-L1432) |
| 启动：filament_id 被覆盖 | [Preset.cpp:1457](../../BambuStudio/src/libslic3r/Preset.cpp#L1457) |
| 启动：父预设缺失 | [Preset.cpp:1462-1466](../../BambuStudio/src/libslic3r/Preset.cpp#L1462-L1466) |
| `remove_invalid_keys` | [Preset.cpp:486](../../BambuStudio/src/libslic3r/Preset.cpp#L486)、调用点 [1477](../../BambuStudio/src/libslic3r/Preset.cpp#L1477) |
| filament 键集合 | [Preset.cpp:1087](../../BambuStudio/src/libslic3r/Preset.cpp#L1087) |
| 未知键兜底清空 | [PrintConfig.cpp:7411-7414](../../BambuStudio/src/libslic3r/PrintConfig.cpp#L7411-L7414) |
| 未知键记录（不抛） | [Config.cpp:603-607](../../BambuStudio/src/libslic3r/Config.cpp#L603-L607) |
| `key_values` 分流 | [Config.cpp:919-956](../../BambuStudio/src/libslic3r/Config.cpp#L919-L956) |
| 用户路径硬编码 `true` | [Config.cpp:841](../../BambuStudio/src/libslic3r/Config.cpp#L841) |
| semver 解析规则 | [semver.c:175-215](../../BambuStudio/src/semver/semver.c#L175-L215) |
| 拖拽白名单 | [Plater.cpp:21609](../../BambuStudio/src/slic3r/GUI/Plater.cpp#L21609) |
| Import Configs 菜单 | [MainFrame.cpp:3008-3012](../../BambuStudio/src/slic3r/GUI/MainFrame.cpp#L3008-L3012) |
| 导入结果弹窗 | [MainFrame.cpp:4033-4036](../../BambuStudio/src/slic3r/GUI/MainFrame.cpp#L4033-L4036) |
| CLI `--load-filaments` 父预设路径 | [BambuStudio.cpp:2519-2531](../../BambuStudio/src/BambuStudio.cpp#L2519-L2531) |
