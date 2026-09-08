# Material Profile Worker

Feishu Bitable 按钮 → Automation → `已请求=true` → worker 轮询 → 材料档案 JSON 写入 Git 仓库并开 PR → 回写 `状态=审核中`。

Git 仓库是材料档案的唯一真相源(不引入数据库)。文档见 [docs/agent-instruction.md](docs/agent-instruction.md)。

## 架构

```
domain/profile.py        材料档案领域模型(序列化/校验,不依赖任何 SDK)
domain/status.py         显式状态转换表(单次提交生命周期 + 终态判定)
domain/submission.py     一次提交的领域对象 + 提交 ID 解析规则
adapters/bitable.py      飞书 Bitable SDK 唯一入口(列名映射/过滤/schema 保障)
adapters/git.py          Git 提供方适配器(GitHub/Gitea 纯 HTTP API,幂等)
services/submission_service.py  业务编排(校验 -> claim -> Git -> 回写,错误分流)
worker.py                轮询 daemon(单记录异常隔离,daemon 级异常不终止)
main.py                  dependency composition / 入口
fields.py                Bitable 列名的唯一事实来源
```

## 触发链路(保持不变)

```
按钮 ──> Automation 置「已请求」= true ──> worker 每 N 秒轮询 ──> 处理
```

worker 每轮拉取全表后按 `已请求 = true` **本地过滤**取待处理行——实测飞书服务端公式/结构化过滤对复选框字段会静默返回空(语法合法、code=0 但匹配不到已勾选行),故不采用服务端过滤;表格量级小(单次分页 500 行)全表拉取成本可忽略。

## 表格要求(建表指南)

worker 启动时会自动补齐缺失的**文本/数字/附件**列(提交元数据列、`Profile JSON` 附件列与全部结构/耗材参数列,见下),但以下两列类型特殊,**需人工创建**,缺失时 worker 启动即报错:

| 列名 | 类型 | 说明 |
|---|---|---|
| `状态` | 单选 | 选项需包含:`草稿/待处理/处理中/审核中/已通过/已拒绝/失败` |
| `已请求` | 复选框 | 按钮 → Automation 置为已勾选 |

> **列类型/选项校验(V2-P1)**:worker 启动时不仅检查 `状态` 列存在,还会按字段定义校验其**类型必须为单选**且**选项覆盖上述 7 个状态**,不符即启动失败并给出修复指引——飞书不支持直接把字段改成别的类型,需人工删除后用单选重建并重新配置选项;缺选项则人工在字段设置中补齐即可。

### 数据列(V2-P4,一行 = 一种材料 × 一个机型配置)

- **结构参数(文本,必填)**:`品名`/`品牌`/`机型`/`切片器`。
  `切片器` 白名单:`BambuStudio`/`Orcaslicer`/`OrcaSlicer`/`ElegooSlicer`/`CrealityPrint`
  (PrusaSlicer 产物为 `.ini` 预设,暂不支持,填写即报错)。
- **耗材参数(数字,必填 10 项)**:`线材密度`(g/cm³)/`软化温度`(°C)/`冷却开启层时`(秒)/
  `最大风扇速度`(%)/`最小风扇速度`(%)/`降速层时`(秒)/`喷嘴温度`(°C)/`流量比例`/
  `最大体积流速`(mm³/s)/`回抽距离`(mm)——键名与取值范围对齐 BambuStudio 官方 key registry。
- **可选**:
  - `材料ID`(文本):只作备注,不再参与文件名;Git JSON 里 `id` 缺省用品名;
  - `压力提前`(数字):仅第三方机器适用;`品牌`=BBL(官方机型)时填写即报错;
  - `继承预设`/`切片器版本`/`调参方法版本`/`PI Code`(文本):留空则 Git JSON 省略对应键;
    PI Code 是预留 seam(将来映射为产品名),当前仅建模存储、**不进入 Git JSON**。

手工填表与 JSON 附件导入共用同一份 canonical schema(`MaterialProfile`),没有两套规则;必填缺失/数值越界一律逐字段报错并落 `错误信息`,不会产生 Git 提交。

## JSON 附件导入(上传 = 新建材料,V2-P4)

用户可以把材料 Profile JSON 作为 `Profile JSON` 附件列上传(建行后挂附件即可,不必手动逐格输入);worker 每轮扫描**未进入提交生命周期**的行(状态空/`草稿`),满足「附件存在 + 标准字段有空位」就自动解析反写并**自动进入提交流程**:

- **严格单 JSON**:该列须恰好挂 1 个 `.json` 文件;无/多个 JSON、混入图片等其它文件一律报错,绝不静默选一个;
- **键名规范(放宽)**:附件 JSON 只要含全部 14 个必填英文键(`name`/`brand`/`model`/`slicer` + 10 项耗材参数,键名同下节 Git 示例)就认为合法;**其余键一律忽略**——真实 BambuStudio 系文件的附加键(plate 温度、`filament_type`、`from` 等)不再报错;worker 派生键(`submission`/`generated_at`/`filament_settings_id`/`enable_pressure_advance`)由 worker 自己生成,附件携带同名键不读取;
- **数值形态**:接受标量数字/数字字符串,也兼容 BambuStudio 系字符串数组(单元素 `["220"]` 或各值一致的 `["220","220"]`);含 `nil`/空、或多喷头取值不一致的数组报错提示改单值或手填——绝不静默挑一个值;输出到 Git 仍是标量;
- **同一套校验**:附件 JSON 与手工填表共用 `MaterialProfile` + `validate()`(无两套业务规则);缺失必填、数值非法、越界都逐条报错;
- **只填空格**:已填写的单元格绝不被附件覆盖;反写一次完成,失败不产生部分脏数据;
- **自动提交(V2-P4)**:上传 JSON 一定是新建材料——反写成功(必填字段齐)即与清错误信息同一次原子回写置「已请求」,下一轮轮询走与按钮完全相同的常规流程(claim → Git 幂等提交 → `审核中` + PR),无需再点按钮;
- **失败呈现**:解析/校验失败把原因(前缀「附件解析失败: …」)写入 `错误信息` 列,**不置「已请求」、不提交**;该附件(file_token)在 worker 进程内不重复尝试;替换附件或重启进程后自然重试,瞬时下载失败则下轮自动重试;
- 字段已齐的行(人工填写完成)即使挂着附件也不解析、不自动提交;过程照片等 archive-only 列 worker 一律不读取。

## 状态机

一次「提交」的 worker 驱动生命周期:

```
待处理 ──claim──> 处理中 ──Git 成功──> 审核中(记录 PR URL)──PR 合并──> 已通过
  │                │                                              └PR 关闭未合并──> 已拒绝
  └──永久失败──> 失败    └──瞬态失败──> 保持 处理中 + 已请求=true,自动重试(≤MAX_RETRIES)

重复点击(行处于 审核中,V2-P0):
  审核中 + 已请求=true ──确认在途 PR── 仍打开 ──> 保持 审核中(清除已请求,不建新 PR)
                                        └─ 已合/已关 ──> 由本轮审查同步置 已通过/已拒绝
```

- **瞬时故障**(Git/网络 5xx/429/限流等)绝不直接标失败:状态保持 `处理中`、`已请求` 置回 true,下一轮自动重试;超过 `MAX_RETRIES` 才置 `失败`。
- **永久失败**(数据校验错误 / Git 鉴权失败等)直接置 `失败` 并写 `错误信息`,等用户修正后重新点击按钮。
- **审查同步**:worker 每轮还检查所有 `审核中` 行,对照 Git 侧 PR 状态自动推进——
  - PR **已合并** → `状态=已通过`;
  - PR **关闭未合并**(人工撤销等)→ `状态=已拒绝`,并回写「关闭理由」(V2-P3);
  - PR 仍打开 → 保持 `审核中`;Git 查询失败/找不到 PR 时保持 `审核中` 不误标,下轮自然重试。
- **关闭理由(V2-P3)**:PR 关闭未合并时,worker 取**关闭前最后一条普通评论正文**写入 `关闭理由` 列;Git 侧无评论则写兜底文案「PR 已关闭,未说明原因」;评论读取失败(瞬时)→ 仍落 `已拒绝` 但理由留空、打印日志,由人工补充(不阻塞状态推进)。新一轮提交 claim 时自动清空上一轮残留的关闭理由。
- 重复点击语义(V2-P0,按行当前状态区分):
  - `待处理/处理中/失败` → **复用同一提交 ID**(同一 branch/PR,幂等更新);
  - `审核中`(在途提交)→ **绝不开启新一轮、绝不产生第二个 PR**:worker 先确认该提交在 Git 上的实际 PR 状态——
    PR 仍打开 → 保持 `审核中` 并清除请求信号,本次重复触发就此打住;
    PR 已合并 / 已关闭未合并 → 交回本轮随后的审查同步,落 `已通过 / 已拒绝`(关闭理由见 V2-P3);
    Git 上查不到该 PR → 保持 `审核中`、清除请求信号并一次性告警,需人工处理;
    查询失败(瞬时)→ 不动行,下轮自动重试。
  - `已通过/已拒绝`(上一轮提交已收尾)→ **开启新一轮提交**(新提交 ID / 新 branch / 新 PR);新一轮 claim 时会清空上一轮留下的「关闭理由」(V2-P3)。
  - 快速双击:两次点击落在同一轮询周期内只置一次 `已请求=true`,只处理一次、只产生一个 PR。

## 幂等与重试(P4)

- 每次提交有稳定 `提交 ID`(写回 Bitable `提交 ID` 列),branch 名由其确定:`material/<提交ID>`。
- `GitRepository.submit_profile()` 先查既有 PR,存在即复用;**Git 成功但 Bitable 回写失败**后自动重试,只会收敛到同一个 PR,绝不产生重复 PR。
- 文件内容与远端一致时不产生空提交;与默认分支完全一致(如已合并后无改动再次提交)不会建空 PR,会以失败提示。
- claim 后回读校验,多 worker 并发时后写者获胜,败者让行;双 worker 强互斥依赖 Bitable 无 CAS 不支持,**生产建议单 worker 部署**。

## 环境变量

复制 `.env.example` 为 `.env`:

| 变量 | 必填 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | ✅ | 飞书自建应用 |
| `FEISHU_APP_TOKEN` / `FEISHU_TABLE_ID` | ✅ | 目标多维表格 |
| `GIT_REPOSITORY_URL` | ✅ | Git 仓库 **https** URL(SSH 形式不支持,HTTP API 模式需要 token)。host 为 github.com 走 GitHub API,其余按 Gitea `/api/v1` |
| `GIT_ACCESS_TOKEN` | ✅ | GitHub / Gitea Personal Access Token(仓库读写权限) |
| `POLL_INTERVAL` | 默认 5 | 轮询间隔(秒) |
| `MAX_RETRIES` | 默认 5 | 瞬态失败自动重试上限 |

## Git 仓库内容

每次提交写一个文件(可重复提交/更新同一路径,由 PR 串联审查);目录布局照搬 Polymaker-Preset 实况(V2-P4),品名/品牌/机型/切片器各为一段目录,空格与中文原样保留:

```
preset/<品名>/<品牌>/<机型>/<切片器>/<品名> @<品牌> <机型>.json
```

文件内容 = 材料档案 + worker 派生的提交溯源(数值为标量,整数不写 `.0`;
`name`/`filament_settings_id`/文件名主干 = worker 派生的复合名 `<品名> @<品牌> <机型>`,品名不单独成键;`pi_code` 等纯输入备注不进入文件):

```json
{
  "id": "Test PLA",
  "name": "Test PLA @BBL H2C",
  "brand": "BBL",
  "model": "H2C",
  "slicer": "BambuStudio",
  "filament_settings_id": "Test PLA @BBL H2C",
  "filament_density": 1.24,
  "temperature_vitrification": 59,
  "fan_cooling_layer_time": 100,
  "fan_max_speed": 100,
  "fan_min_speed": 100,
  "slow_down_layer_time": 8,
  "nozzle_temperature": 220,
  "filament_flow_ratio": 0.98,
  "filament_max_volumetric_speed": 16,
  "filament_retraction_length": 0.4,
  "submission": "a1b2c3…",
  "generated_at": "2026-09-06T12:00:00+08:00"
}
```

可选键按是否填写出现:`inherits`/`version`/`pm_method_version`;
填了 `压力提前`(仅第三方机器)时额外输出 `pressure_advance` 与派生开关 `enable_pressure_advance: 1`。

## 运行与测试

```bash
pip install -e ".[dev]"
material-worker            # 或 python -m material_worker.main
python -m pytest tests/ -v
```

worker 启动即检查表格 schema,缺列自动补;核心测试用 fake 的 Bitable/Git 适配器,不触网、不需要真实凭证。
