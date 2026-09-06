"""Bitable 表格列名的唯一事实来源(P2:集中字段映射)。

domain 解析与 BitableClient 读写都引用这里的常量,
表格列改名时只需改这一个文件。
"""

# 触发信号(按钮 -> Automation 置 true)
REQUESTED = "已请求"

# 工作流状态(单选)
STATUS = "状态"

# 材料核心字段(提交内容)
NAME = "品名"
NOZZLE_TEMP = "喷嘴温度"
MAX_VOL_SPEED = "最大体积流速"

# 材料身份(可选;缺省用品名)
MATERIAL_ID = "材料ID"

# 提交元数据(P3:worker 回写)
SUBMISSION_ID = "提交 ID"
PR_URL = "PR URL"
ERROR_MSG = "错误信息"
RETRY_COUNT = "重试次数"

# 期望 worker 自动保证存在的文本/数字列:
#   提交 ID / PR URL / 错误信息(文本)、重试次数(数字)。
# 「状态」(单选)与「已请求」(复选框)由人工在表格中创建,
# 缺失时 worker 启动会报错并给出提示。
