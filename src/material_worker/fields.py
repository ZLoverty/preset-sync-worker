"""Bitable 表格列名的唯一事实来源(P2:集中字段映射)。

domain 解析与 BitableClient 读写都引用这里的常量,
表格列改名时只需改这一个文件。

V3-P1:列名按 2026-09-11 的线上表格重写 —— 删除 品名/品牌/机型/切片器/
材料ID/切片器版本/提交 ID/重试次数(V2 时代的列),补入 热床温度/
玻璃化温度/过程记录/提交人/提交时间,并将附件列改名为
「Profile JSON extract」。
"""

# 触发信号(按钮 -> Automation 置 true)
REQUESTED = "已请求"

# 工作流状态(单选)
# V2-P1:worker 启动时会校验「状态」列的类型(必须为单选)与选项
# (必须覆盖状态机全集:草稿/待处理/处理中/审核中/已通过/已拒绝/失败),
# 类型或选项不对则启动失败并给出修复指引。
STATUS = "状态"

# —— V3 行身份三要素(单选列):PI Code × 打印机型号 × 切片软件 ——
# 身份决定 Git 路径,branch 亦由身份派生(V3-P4/P5)。
PI_CODE = "PI Code"
PRINTER_MODEL = "打印机型号"
SLICER = "切片软件"

# —— 耗材参数(数字)——
# 键名与取值范围对齐 BambuStudio 官方 key registry,见 domain/profile.py。
# 「热床温度」在输出里映射到 textured_plate_temp(V3:只用这一个键)。
BED_TEMP = "热床温度"
NOZZLE_TEMP = "喷嘴温度"
FAN_COOLING_LAYER_TIME = "冷却开启层时"
FAN_MAX_SPEED = "最大风扇速度"
FAN_MIN_SPEED = "最小风扇速度"
SLOW_DOWN_LAYER_TIME = "降速层时"
FLOW_RATIO = "流量比例"
MAX_VOL_SPEED = "最大体积流速"
RETRACTION_LENGTH = "回抽距离"
PRESSURE_ADVANCE = "压力提前"
FILAMENT_DENSITY = "线材密度"
VITRIFICATION = "玻璃化温度"

# —— 结构参数(文本)——
# V3:继承预设升为必填(为空即报错、不生成 PR)。
INHERITS = "继承预设"
PM_METHOD_VERSION = "调参方法版本"

# V3-P2:过程记录(附件)—— worker 只取文件名写进 commit message,
# 附件本体不进 Git;提交成功后清空该列(V3-P8)。
PROCESS_RECORD = "过程记录"

# V3-P1:按钮列(3001),人工/Automation 触发入口;类型由人工维护。
SUBMIT_BUTTON = "提交审核"

# V3-P3:附件导入列(附件类型)。上传 .json 或 PrusaSlicer .ini,
# worker 宽容解析后「只填空」反写;**不再自动提交**(提交由用户点按钮)。
PROFILE_JSON = "Profile JSON extract"

# 提交元数据(P3:worker 回写)
PR_URL = "PR URL"
ERROR_MSG = "错误信息"
# V2-P3:PR 关闭(未合并)时的关闭理由,由 worker 从 Git 侧最后一条评论同步;
# 新一轮 claim 时清空(与 PR URL 一起,保证列内永远指向本轮 PR)。
CLOSE_REASON = "关闭理由"

# V3-P7:提交人(人员)/提交时间(日期)—— 按钮 Automation 写入,
# worker 读出后写进 PR 正文与 commit message,不改写这两列。
SUBMITTER = "提交人"
SUBMIT_TIME = "提交时间"
