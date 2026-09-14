# Material Profile Worker — Agent Instruction

## 0. 任务背景

本项目是 Material Profile Worker，用于将飞书多维表格中的材料参数转换为 Git 仓库中的标准材料 Profile，并通过 PR 进入审核流程。

阶段 1 已完成并验收：

- agent-instruction.md P0–P7
- 共 55 项测试
- Git 仓库仍是材料档案的唯一真相源
- 不引入数据库

本阶段为 **Phase 2**，目标是处理 V2-P0～V2-P4 中记录的缺陷和需求。

---

# 1. 硬约束

以下约束在 Phase 2 中继续有效，任何实现不得破坏：

1. **不引入数据库**
   - 不增加 SQLite、PostgreSQL、MySQL、Redis 等持久化数据库。
   - Git 仓库仍然是材料档案的唯一真相源。

2. **阶段 1 不回退**
   - 阶段 1 P0–P7 已验收。
   - 55 项现有测试是回归基线。
   - Phase 2 完成后必须全部通过。

3. **MaterialProfile 是规范数据模型的单一来源**
   - JSON 输入解析、Bitable 输入、最终 Git Profile 均应围绕同一套 Profile schema。
   - 不允许为 JSON 上传、Bitable 提交等场景复制一套独立的校验规则。

4. **worker 必须具备防御能力**
   - 不能依赖飞书 Automation、按钮行为或前端约束保证数据正确。
   - 对重复触发、错误字段类型、非法 JSON、PR 状态变化等情况，worker 必须能够安全处理。

5. **不擅自确定开放问题**
   - 本文中明确标记为“待确认 / 待定 / 开放问题”的设计，不得由 agent 自行拍板后改变产品语义。
   - 可以提出推荐方案，但实现前必须将其作为明确设计决策。
   - 如果某个开放问题不影响当前 P0/P1 等功能的独立实现，可以先保持 TODO。

---

# 2. Phase 2 实现顺序

推荐按以下顺序实施：

1. V2-P1 — Bitable Schema 类型校验
2. V2-P0 — 重复点击导致重复 PR
3. V2-P3 — PR 关闭理由同步
4. V2-P2 — JSON 附件导入
5. V2-P4 — 全量 Profile 参数集

原因：

- P1 是基础设施级 schema 防护；
- P0 直接影响提交幂等性；
- P3 是现有 PR 审查同步逻辑的扩展；
- P2 依赖稳定的 Profile schema；
- P4 会进一步扩大 Profile schema，是 P2 的最终数据模型基础。

每完成一个条目：

- 增加对应测试；
- 更新 README；
- 更新状态机/流程图（如受影响）；
- 保证 55 项阶段 1 回归测试全部通过。

---

# 3. V2-P0 — 重复点击导致重复 PR

## 3.1 问题

当前同一 Bitable 行在第一次提交后进入：

```text
审核中
````

对应一个仍然打开的 PR。

如果用户再次点击“提交”，worker 会将其视为新的提交轮次：

```text
新 submission_id
→ 新 branch
→ 新 PR
```

最终同一行产生多个内容相同或高度相似的 PR。

这是错误行为。

---

## 3.2 当前实现锚点

重点检查：

```text
src/material_worker/domain/status.py
src/material_worker/domain/submission.py
src/material_worker/services/submission_service.py
README.md
tests/test_service.py
```

特别关注：

### status.py

当前：

* `TERMINAL_STATES` 将 `审核中` 视为已收尾；
* `RETRYABLE_FOR_SAME_SUBMISSION` 不包含 `REVIEWING`。

### submission.py

当前：

```text
resolve_submission_id(...)
```

对于终态可能直接生成新的 submission ID。

### submission_service.py

`process_record()` 根据当前快照状态决定 submission ID。

### test_service.py

已有测试：

```text
test_terminal_state_click_opens_new_submission
```

该测试固化了旧语义，必须根据新语义修改。

---

## 3.3 目标行为

至少必须保证：

```text
审核中 + 对应 PR 仍然打开
        ↓
再次点击提交
        ↓
不得创建第二个 PR
```

最终仍然只有一个在途 PR。

---

## 3.4 推荐实现方向

优先考虑：

### 方案 B：审核中复用当前 submission

将：

```text
审核中
```

视为当前 submission 仍处于 active/in-flight 状态。

再次触发时：

```text
已请求=true
状态=审核中
       ↓
找到现有 submission
       ↓
确认已有 PR
       ↓
不创建新的 branch / PR
       ↓
保持审核中
```

如果当前实现已经具备 submission ID 和 PR URL，应优先复用这些信息。

不要仅依赖：

```text
状态 == 审核中
```

判断是否存在 PR。

必须尽可能确认当前 submission 的实际 PR 状态。

---

## 3.5 快速双击

必须保持：

```text
同一个轮询周期内两次点击
→ 已请求最终只出现一次 true
→ 只创建一个 PR
```

增加回归测试锁定该行为。

---

## 3.6 已通过 / 已拒绝

以下语义目前仍属于开放设计：

```text
已通过 + 再次提交
已拒绝 + 再次提交
```

是否意味着：

```text
开启新的 submission
```

还是：

```text
其他行为
```

不得在没有设计决策的情况下擅自改变。

当前实现如需要修改，应：

1. 保持明确的状态机语义；
2. 增加测试；
3. 更新 README；
4. 更新状态机图。

---

## 3.7 空 PR 防护

现有行为：

```text
提交内容与默认分支完全一致
→ 永久错误
→ 不创建空 PR
```

必须保持。

---

## 3.8 验收标准

必须满足：

* [ ] 审核中 + PR 打开时重复点击，不增加 PR；
* [ ] 快速双击只产生一个 PR；
* [ ] 阶段 1 幂等重试行为不回归；
* [ ] 失败路径不回归；
* [ ] `test_terminal_state_click_opens_new_submission` 等测试根据新语义更新；
* [ ] README 重复点击语义更新；
* [ ] 状态机图更新；
* [ ] 全部测试通过。

代码/测试注释统一使用：

```text
V2-P0
```

---

# 4. V2-P1 — 状态列必须是单选

## 4.1 问题

当前 README 和 `fields.py` 要求：

```text
状态
```

必须为飞书多维表格的单选列。

但是：

```text
ensure_schema()
```

目前只检查：

```text
列是否存在
```

没有检查：

```text
列类型
```

因此错误的表结构可能正常启动。

---

## 4.2 目标 Schema

`状态` 必须是：

```text
单选
```

并且选项与状态机一致：

```text
草稿
待处理
处理中
审核中
已通过
已拒绝
失败
```

---

## 4.3 实现要求

重点修改：

```text
src/material_worker/fields.py
src/material_worker/adapters/bitable.py
```

重点检查：

```text
ensure_schema()
```

---

## 4.4 类型检查

启动时通过 field ID 获取字段定义。

不能只根据字段名称判断。

检查：

```text
field_id
→ field definition
→ field type
```

如果不是单选：

```text
worker 启动失败
```

错误信息至少包含：

* 字段名称；
* 当前字段类型；
* 期望类型；
* 修复建议。

修复建议必须明确：

```text
飞书不支持直接将现有字段类型转换为目标类型。
请人工删除/重建“状态”字段，并重新配置状态选项。
```

---

## 4.5 状态选项

建议同时校验已有单选列是否包含完整状态集合：

```text
草稿
待处理
处理中
审核中
已通过
已拒绝
失败
```

如果缺少必要选项：

```text
启动失败
```

并明确指出缺失选项。

---

## 4.6 测试

至少新增：

```text
状态字段不存在
状态字段类型正确
状态字段为文本
状态字段为多选
状态字段缺少选项
状态字段完整正确
```

验收：

* [ ] 文本 → 启动失败；
* [ ] 多选 → 启动失败；
* [ ] 正确单选 → 正常启动；
* [ ] 缺状态选项 → 启动失败；
* [ ] 全量测试通过。

代码/测试注释：

```text
V2-P1
```

---

# 5. V2-P2 — JSON 附件上传 → 解析 → 重构 → 反写

## 5.1 目标

用户可以直接把一个材料 Profile JSON 作为：

```text
多维表格附件列
```

上传。

上传后创建 Bitable 行。

worker 自动：

```text
附件
 ↓
下载 JSON
 ↓
解析
 ↓
规范化
 ↓
MaterialProfile
 ↓
validate()
 ↓
反写标准字段
 ↓
用户点击提交
 ↓
既有提交流程
 ↓
Git PR
```

---

## 5.2 重要边界

上传入口必须是：

```text
Bitable 附件列
```

旧设计中“直接写 Git 仓库”已经废弃。

JSON 上传本身：

```text
不是新的提交旁路
```

而只是：

```text
快速填充 Bitable 标准字段
```

因此：

```text
JSON 上传
≠ 自动提交 PR
```

默认设计：

```text
附件存在
+
标准字段尚未填充
→ worker 解析并反写
→ 用户确认后点击提交
```

---

## 5.3 数据规则

JSON 解析和 Bitable 提交必须共享：

```text
MaterialProfile
```

以及其：

```text
validate()
```

不得出现：

```text
JSONValidator
BitableValidator
```

两套彼此独立的业务规则。

---

## 5.4 附件读取

Bitable 附件字段通常提供：

```text
file_token
```

worker 需要：

```text
file_token
→ 飞书媒体下载 API
→ 文件内容
→ JSON parser
```

必须处理：

* 没有附件；
* 附件不是 JSON；
* JSON 文件为空；
* JSON 格式非法；
* 缺必填字段；
* 字段类型错误；
* 数值无法解析；
* 文件过大；
* 多附件。

---

## 5.5 多附件

当前规则尚未最终确定：

```text
一行多个 JSON 附件取哪个？
```

实现前必须明确规则。

推荐默认：

```text
只允许一个 JSON Profile 附件
```

多个符合条件的 JSON：

```text
报错
```

不要静默选择第一个。

---

## 5.6 原附件

默认：

```text
解析成功后保留原附件
```

因为附件本身具有输入溯源价值。

---

## 5.7 JSON 键名

最终规范尚待确定。

推荐最终使用与 Git Profile 同构的英文 key，例如：

```json
{
  "name": "...",
  "nozzle_temperature": 220,
  "filament_max_volumetric_speed": 12
}
```

但如果需要兼容中文列名，必须显式定义兼容层。

不得让解析器“随便猜”。

---

## 5.8 失败行为

非法 JSON：

```text
不得反写部分数据
```

应：

```text
parse
→ normalize
→ validate
→ 全部成功
→ 一次性反写
```

而不是：

```text
解析到一个字段
→ 立即写入
→ 后续字段失败
```

避免产生脏数据。

失败时至少记录：

```text
错误信息
```

具体是否把：

```text
状态
```

置为 `失败`

需要与现有状态语义协调。

修正附件后必须能够再次处理。

---

## 5.9 验收标准

* [ ] 合法 JSON 可以自动填充标准字段；
* [ ] 不需要人工逐项输入；
* [ ] 非法 JSON 不产生部分脏数据；
* [ ] 错误信息明确；
* [ ] 修正后可以重试；
* [ ] 用户点击提交后走与手工填表完全相同的流程；
* [ ] 幂等、审核、失败语义与普通行一致；
* [ ] worker 不自动因为附件上传而提交 PR；
* [ ] fake/mock API 测试覆盖附件下载；
* [ ] 全量测试通过。

代码/测试注释：

```text
V2-P2
```

---

# 6. V2-P3 — PR 关闭理由同步到 Bitable

## 6.1 问题

当前审查同步已经能够识别：

```text
closed + unmerged
```

并设置：

```text
状态 = 已拒绝
```

但是没有把 Gitea 中的关闭原因同步到 Bitable。

---

## 6.2 目标

当 PR：

```text
closed
+
未 merged
```

时：

```text
状态 = 已拒绝
关闭理由 = PR 关闭时留下的评论文字
```

---

## 6.3 字段

新增文本列：

```text
关闭理由
```

worker 启动时自动创建。

涉及：

```text
src/material_worker/fields.py
src/material_worker/adapters/bitable.py
```

---

## 6.4 理由来源

Gitea：

```text
Issue comments / timeline
```

GitHub：

```text
timeline events
```

实际 API 结构必须通过当前 provider 的真实 API 行为确认。

不要假设 GitHub/Gitea API 完全一致。

---

## 6.5 merged 边界

如果：

```text
PR merged
```

则：

```text
状态 = 已通过
```

并且：

```text
不读取关闭理由
```

只有：

```text
closed && !merged
```

才读取理由。

---

## 6.6 没有评论

当前存在两种候选：

```text
关闭理由 = 空
```

或：

```text
关闭理由 = PR 已关闭，未说明原因
```

实现前确定。

---

## 6.7 理由读取失败

理由读取失败：

```text
不能阻塞状态推进
```

仍然：

```text
状态 = 已拒绝
```

然后：

```text
关闭理由留空
日志记录错误
```

本阶段不设计自动补偿机制。

---

## 6.8 与下一轮提交

如果以后：

```text
已拒绝
→ 新 submission
```

则：

```text
关闭理由
```

是保留历史还是清空，必须与 V2-P0 中：

```text
已拒绝后再次提交
```

的语义一起确定。

不要单独决定。

---

## 6.9 验收

* [ ] closed + unmerged + comment → 已拒绝 + 关闭理由；
* [ ] closed + unmerged + no comment → 按确定规则处理；
* [ ] merged → 已通过，不写关闭理由；
* [ ] 理由 API 失败 → 状态仍为已拒绝；
* [ ] 新增 fake Gitea PR/comment 测试；
* [ ] 全量测试通过。

代码/测试注释：

```text
V2-P3
```

---

# 7. V2-P4 — 全量 Material Profile

## 7.1 目标

阶段 1 只验证：

```text
品名
喷嘴温度
最大体积流速
```

Phase 2 需要进行一次更接近真实生产的全量测试。

目标：

```text
Bitable 全量参数
→ MaterialProfile
→ validate
→ JSON Profile
→ Git
→ PR
```

---

# 8. Profile 参数分类

参数分为：

## A. 耗材参数

物理实测参数。

必须进入 Profile JSON。

当前规划：

| 中文名    | key                             |   示例 |
| ------ | ------------------------------- | ---: |
| 线材密度   | `filament_density`              | 1.17 |
| 软化温度   | `temperature_vitrification`     |   60 |
| 冷却开启层时 | `fan_cooling_layer_time`        |   20 |
| 最大风扇速度 | `fan_max_speed`                 |  100 |
| 最小风扇速度 | `fan_min_speed`                 |  100 |
| 降速层时   | `slow_down_layer_time`          |    6 |
| 喷嘴温度   | `nozzle_temperature`            |  220 |
| 流量比例   | `filament_flow_ratio`           | 0.94 |
| 最大体积流速 | `filament_max_volumetric_speed` |   12 |
| 压力提前   | 待定                              |   待定 |
| 回抽距离   | `filament_retraction_length`    |  0.4 |

---

## 8.1 键名来源

键名与取值范围必须以：

```text
BambuStudio 官方 profile / key registry
```

为准。

特别是：

```text
压力提前
```

不得手写猜测 key。

需要实际检查 BambuStudio 官方 profiles/key registry。

---

# 9. B. 结构参数

结构参数进入 Profile JSON。

当前规划：

| 中文名     | key                    | 示例                               |
| ------- | ---------------------- | -------------------------------- |
| PI Code | 待定                     | L1003                            |
| 继承预设    | `inherits`             | Generic PLA @BBL H2C 0.4 nozzle  |
| 导入 ID   | `filament_settings_id` | —                                |
| 版本      | `version`              | —                                |
| 调参方法版本  | `pm_method_version`    | v1.0                             |
| 提交溯源    | `submission`           | 032d5ac951eb4501b631cd920c2fe6c5 |
| 生成时间    | `generated_at`         | worker 生成                        |

---

# 10. C. 过程记录

过程记录：

```text
照片
调参过程
其他 troubleshooting 资料
```

只存 Bitable。

明确要求：

```text
不进入 Profile JSON
不参与 Git submission
不参与 profile validation
```

worker 不应读取这些列。

---

# 11. MaterialProfile 扩展要求

当前：

```text
MaterialProfile
```

只有 POC 字段。

Phase 2 需要扩展成完整 schema。

重要原则：

```text
MaterialProfile
    ↓
统一定义 Profile schema
```

Bitable JSON 输入：

```text
→ MaterialProfile
```

JSON 附件输入：

```text
→ MaterialProfile
```

最终 Git：

```text
MaterialProfile
→ JSON
```

所有路径共享：

```text
validate()
```

---

# 12. Profile 文件组织

最终 Git 文件组织规划：

```text
material/
└── <material name>/
    └── <printer brand>/
        └── <printer model>/
            └── <slicer>/
                └── <profile>
```

实际目录命名规则需要结合现有 Polymaker preset 仓库确认。

不得在没有确认前随意设计新的目录结构。

---

# 13. 行粒度

目前存在设计问题：

一个 Bitable 行到底代表：

```text
材料
```

还是：

```text
材料 × 打印机型号
```

由于：

```text
inherits
```

示例：

```text
Generic PLA @BBL H2C 0.4 nozzle
```

已经包含：

```text
机型
喷嘴
```

并且 Git 文件组织也包含：

```text
printer brand
printer model
```

因此实现前必须确认最终 row granularity。

当前倾向：

```text
一行 = 一个材料 × 一个机型/打印配置 Profile
```

但不得把这个倾向直接当成已经确认的产品要求。

---

# 14. PI Code

PI Code 的最终角色待确认：

可能：

```text
Profile ID
```

也可能：

```text
文件名的一部分
```

或：

```text
普通结构字段
```

实现前确定。

---

# 15. generated_at / submission

这两个字段原则上由 worker 生成：

```text
generated_at
submission
```

其中：

```text
submission
```

应与阶段 1 submission ID 使用同一语义/格式。

不要让用户输入一个可能覆盖真实 submission identity 的值。

---

# 16. 过程照片

worker：

```text
不得读取过程照片列
```

原因：

* 过程照片用于 troubleshooting；
* 不属于 Profile canonical data；
* 不参与提交；
* 不进入 Git；
* 不影响 Profile validation。

因此：

```text
Bitable photo/attachment column
```

应被视为：

```text
archive-only
```

---

# 17. V2-P4 验收

必须完成一个真实的全量测试行：

```text
完整 A 类参数
+
完整 B 类结构参数
+
过程照片
```

点击提交后：

```text
Bitable
 ↓
worker
 ↓
MaterialProfile
 ↓
validate
 ↓
JSON
 ↓
Git branch
 ↓
PR
```

必须确认：

* [ ] 所有 A 类字段正确进入 JSON；
* [ ] 所有 B 类字段正确进入 JSON；
* [ ] `submission` 正确生成；
* [ ] `generated_at` 正确生成；
* [ ] 文件路径符合约定；
* [ ] 缺少必填字段时逐字段报错；
* [ ] 照片不进入 JSON；
* [ ] 照片不影响提交；
* [ ] JSON 附件导入可以一次填充全量字段；
* [ ] JSON 附件导入与手工填表使用同一个 MaterialProfile schema；
* [ ] 全量测试通过。

代码/测试注释：

```text
V2-P4
```

---

# 18. 测试要求

Phase 2 不允许只验证 happy path。

每个条目至少覆盖：

```text
正常
错误
重复
重试
边界
回归
```

---

## 18.1 回归基线

必须始终运行：

```text
pytest
```

并保证：

```text
Phase 1 55 tests
```

全部通过。

如果因为 Phase 2 语义变化导致旧测试必须改变：

1. 修改测试；
2. 明确测试对应的 V2 编号；
3. 不得简单删除测试；
4. README 必须同步。

---

# 19. README 要求

Phase 2 修改完成后必须同步 README。

至少更新：

## 重复提交语义

明确说明：

```text
审核中 + PR 打开
→ 再次点击不会创建新的 PR
```

如果最终确定：

```text
已通过
已拒绝
```

的再次提交语义，也必须写入 README。

---

## 状态机

状态机图必须反映最终状态语义。

至少包含：

```text
草稿
待处理
处理中
审核中
已通过
已拒绝
失败
```

以及：

```text
重复触发
重试
PR merged
PR closed
```

等关键转移。

---

# 20. 不允许的实现方式

以下做法禁止：

### 20.1 引入数据库

禁止：

```text
SQLite
PostgreSQL
Redis
MongoDB
```

等作为 submission 状态持久化。

---

### 20.2 复制 Profile 校验逻辑

禁止出现：

```text
BitableValidator
JsonValidator
GitProfileValidator
```

分别维护同一业务规则。

应该：

```text
输入
 ↓
MaterialProfile
 ↓
validate()
```

---

### 20.3 依赖按钮防重复

不能认为：

```text
飞书按钮不会重复触发
```

worker 必须自己保证幂等。

---

### 20.4 静默选择多个 JSON 附件

多个 JSON 附件时不得：

```text
取第一个
```

然后继续运行。

必须有明确规则。

---

### 20.5 部分反写 JSON

不得：

```text
解析一个字段
→ 立即写一个字段
→ 后续失败
```

必须先：

```text
parse
→ normalize
→ validate
→ 完整成功
→ 一次性反写
```

---

### 20.6 PR API 失败导致状态错误推进

例如：

```text
PR 已创建
↓
Bitable 更新失败
```

不能重新创建第二个 PR。

必须保持阶段 1 已有的幂等/恢复语义。

---

# 21. 实现工作流

每个 V2 条目按照：

```text
1. 阅读现有实现
2. 阅读相关测试
3. 明确现有行为
4. 写/修改测试
5. 实现
6. 运行相关测试
7. 运行全部测试
8. 更新 README
9. 检查状态机
10. 检查 Phase 1 回归
```

执行。

---

# 22. 提交粒度

推荐每个需求独立 commit：

```text
fix(V2-P1): validate Bitable status field type

fix(V2-P0): prevent duplicate PR for reviewing submission

feat(V2-P3): sync PR close reason

feat(V2-P2): import material profile from Bitable attachment

feat(V2-P4): support full material profile schema
```

不要把所有 Phase 2 修改压成一个不可审查的大 commit。

---

# 23. 完成定义 Definition of Done

Phase 2 完成必须同时满足：

* [ ] V2-P0 已实现并验收；
* [ ] V2-P1 已实现并验收；
* [ ] V2-P2 已实现并验收；
* [ ] V2-P3 已实现并验收；
* [ ] V2-P4 已实现并验收；
* [ ] 所有开放问题已经有明确设计决策，或明确延期；
* [ ] 55 项 Phase 1 回归测试全部通过；
* [ ] Phase 2 新增测试全部通过；
* [ ] README 更新；
* [ ] 状态机图更新；
* [ ] 无数据库引入；
* [ ] MaterialProfile 仍然是 canonical schema；
* [ ] Bitable 仍然只是输入/操作界面；
* [ ] Git 仓库仍然是材料档案唯一真相源；
* [ ] 重复提交不会产生重复在途 PR；
* [ ] JSON 附件导入不会绕过正常提交/审核流程。

---

# 24. 当前开放问题清单

实现过程中必须维护以下问题，不得遗忘：

### Q1 — 已通过/已拒绝后再次点击

确定：

```text
审核中
已通过
已拒绝
```

三种状态下再次点击分别意味着什么。

---

### Q2 — 压力提前字段

确认：

```text
BambuStudio 官方 key
```

以及：

```text
字段所在层级
取值类型
取值范围
```

---

### Q3 — PI Code

确定：

```text
是否作为 profile ID
是否作为文件名
是否仅作为 metadata
```

---

### Q4 — 过程照片

确定：

```text
附件数量
Bitable 存储限制
是否需要额外约定
```

但无论如何：

```text
照片不进入 Profile JSON
```

---

### Q5 — 全量测试范围

确定：

```text
单个材料 × 单个机型
```

是否足够，还是需要覆盖：

```text
材料 × 打印机型号 × 喷嘴配置
```

等结构矩阵。

---

### Q6 — 新表还是旧表

推荐：

```text
Phase 2 全量测试使用新 Bitable 表
```

避免破坏：

```text
Phase 1 已验收 POC 表
```

---

# 25. Agent 行为要求

在实现过程中：

* 不要为了“设计完整”而大规模重构已经验收的 Phase 1；
* 优先做最小必要修改；
* 每次修改都保持测试可运行；
* 遇到开放问题时先记录，不要偷偷决定；
* 所有异常都应有可诊断错误信息；
* 所有外部 API 调用都应通过 adapter 隔离；
* domain 层不得依赖飞书/Git API；
* 不得将 Bitable 当作 canonical database；
* 不得把过程照片等非 canonical 数据写入 Git Profile；
* 不得为了实现幂等而引入数据库；
* 优先复用现有 submission / branch / PR 信息；
* 对 PR 创建、状态同步、Bitable 更新等操作必须考虑部分成功。

最终目标不是“让 demo 跑起来”，而是让：

```text
Bitable
   ↓
MaterialProfile
   ↓
Validation
   ↓
Git
   ↓
PR
   ↓
Review
   ↓
Bitable
```

形成一个**可重复、可恢复、可审查、不会制造重复 PR 的闭环**。
