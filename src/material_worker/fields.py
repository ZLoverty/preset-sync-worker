"""Bitable 表格列名的唯一事实来源(P2:集中字段映射)。

domain 解析与 BitableClient 读写都引用这里的常量,
表格列改名时只需改这一个文件。
"""

# 触发信号(按钮 -> Automation 置 true)
REQUESTED = "已请求"

# 工作流状态(单选)
# V2-P1:worker 启动时会校验「状态」列的类型(必须为单选)与选项
# (必须覆盖状态机全集:草稿/待处理/处理中/审核中/已通过/已拒绝/失败),
# 类型或选项不对则启动失败并给出修复指引。
STATUS = "状态"

# 材料核心字段(V2-P4 全量 schema;canonical 字段表见 domain/profile.py
# FIELD_SCHEMA,这里只声明列名)
# —— 结构参数 B(品牌/机型/切片器 决定 Git 目录布局)——
NAME = "品名"
BRAND = "品牌"
MODEL = "机型"
SLICER = "切片器"
# —— 耗材参数 A(§8;除压力提前外全量必填,进入 Git JSON)—— 中文名
# 与 BambuStudio 官方 key registry 的字段一一对应,见 profile.py。
FILAMENT_DENSITY = "线材密度"
VITRIFICATION = "软化温度"
FAN_COOLING_LAYER_TIME = "冷却开启层时"
FAN_MAX_SPEED = "最大风扇速度"
FAN_MIN_SPEED = "最小风扇速度"
SLOW_DOWN_LAYER_TIME = "降速层时"
NOZZLE_TEMP = "喷嘴温度"
FLOW_RATIO = "流量比例"
MAX_VOL_SPEED = "最大体积流速"
RETRACTION_LENGTH = "回抽距离"
PRESSURE_ADVANCE = "压力提前"
# —— 可选 B 结构参数(留空 -> Git JSON 省略对应键)——
INHERITS = "继承预设"
SLICER_VERSION = "切片器版本"
PM_METHOD_VERSION = "调参方法版本"
# V2-P4 PI Code seam:仅建模存储(映射到 product name 的映射表尚未确定),
# 不进入 Git JSON。
PI_CODE = "PI Code"

# 材料身份(可选;V2-P4 起仅作备注,不再参与文件名)
MATERIAL_ID = "材料ID"

# V2-P2:JSON 附件导入列(附件类型,worker 上传入口)。严格单 JSON:
# 该列须恰好挂 1 个 .json 文件,worker 解析后反写标准字段,
# 绝不因此自动提交 PR。过程照片(如有)另列存放,archive-only。
PROFILE_JSON = "Profile JSON"

# 提交元数据(P3:worker 回写)
SUBMISSION_ID = "提交 ID"
PR_URL = "PR URL"
ERROR_MSG = "错误信息"
RETRY_COUNT = "重试次数"
# V2-P3:PR 关闭(未合并)时的关闭理由,由 worker 从 Git 侧最后一条评论同步;
# 新一轮 claim 时清空。合并通过不写理由。
CLOSE_REASON = "关闭理由"

# 期望 worker 自动保证存在的文本/数字/附件列(V2-P4 起含全部数据列):
# 「状态」(单选)与「已请求」(复选框)由人工在表格中创建,
# 缺失时 worker 启动会报错并给出提示。
