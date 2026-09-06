from __future__ import annotations

from enum import Enum


class InvalidTransitionError(ValueError):
    pass


class SubmissionStatus(str, Enum):
    """行(记录)级工作流状态。值与 Bitable「状态」单选列的选项一致。"""

    DRAFT = "草稿"
    PENDING = "待处理"
    PROCESSING = "处理中"
    REVIEWING = "审核中"
    APPROVED = "已通过"
    REJECTED = "已拒绝"
    FAILED = "失败"

    @classmethod
    def from_table(cls, value: object) -> "SubmissionStatus":
        """表格值(选项文本) -> 状态枚举。空值按 待处理 处理。"""
        if value is None or value == "":
            return cls.PENDING
        try:
            return cls(str(value))
        except ValueError:
            return cls.PENDING

    def can_transition(self, to: "SubmissionStatus") -> bool:
        return to in TRANSITIONS.get(self, frozenset())

    def assert_can_transition(self, to: "SubmissionStatus") -> None:
        if not self.can_transition(to):
            raise InvalidTransitionError(
                f"非法的状态转换: {self.value} -> {to.value}"
            )

    def is_terminal(self) -> bool:
        """该提交对 worker 而言已「收尾」:再次点击应开启新一轮提交。

        注意 审核中/已通过/已拒绝 可能仍由人工流转(审核中 -> 已通过),
        但 worker 不再对这些状态下的行执行同一提交的处理。
        """
        return self in TERMINAL_STATES

    def retry_reuses_same_submission(self) -> bool:
        """重复点击/自动重试是否复用现有提交 ID。"""
        return self in RETRYABLE_FOR_SAME_SUBMISSION


# ----------------------------------------------------------------------
# 显式状态转换表(P3 #16)。描述同一条「提交」生命周期内允许的边:
#
#   草稿 ─► 待处理 ─► 处理中 ─► 审核中 ─► 已通过
#     │        │         │                 已拒绝
#     │        │         └─► 失败
#     │        └────► 失败
#     └────────► 处理中
#
#   失败 ─► 处理中   (用户修正后再次点击,复用同一提交 ID 重试)
#   已通过 / 已拒绝 为终态:再次点击 = 新一轮提交(新提交 ID,
#   行状态由新一轮的 claim 直接置为 处理中,不属于同一次提交的转换)。
# ----------------------------------------------------------------------
TRANSITIONS: dict[SubmissionStatus, frozenset[SubmissionStatus]] = {
    SubmissionStatus.DRAFT: frozenset(
        {SubmissionStatus.PENDING, SubmissionStatus.PROCESSING,
         SubmissionStatus.FAILED}
    ),
    SubmissionStatus.PENDING: frozenset(
        {SubmissionStatus.PROCESSING, SubmissionStatus.FAILED}
    ),
    SubmissionStatus.PROCESSING: frozenset(
        {SubmissionStatus.REVIEWING, SubmissionStatus.FAILED}
    ),
    SubmissionStatus.REVIEWING: frozenset(
        {SubmissionStatus.APPROVED, SubmissionStatus.REJECTED,
         SubmissionStatus.FAILED}
    ),
    SubmissionStatus.APPROVED: frozenset(),
    SubmissionStatus.REJECTED: frozenset(),
    SubmissionStatus.FAILED: frozenset({SubmissionStatus.PROCESSING}),
}

# 该行处于这些状态时,再次点击按钮视为对同一提交的重试(复用提交 ID);
# 其余状态(审核中/已通过/已拒绝)视为新一轮提交(新提交 ID)。
RETRYABLE_FOR_SAME_SUBMISSION: frozenset[SubmissionStatus] = frozenset(
    {
        SubmissionStatus.PENDING,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.FAILED,
    }
)

TERMINAL_STATES: frozenset[SubmissionStatus] = frozenset(
    {
        SubmissionStatus.REVIEWING,
        SubmissionStatus.APPROVED,
        SubmissionStatus.REJECTED,
    }
)
