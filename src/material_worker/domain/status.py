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
        """该行对 worker 而言已「收尾」:再次点击应开启新一轮提交。

        已通过/已拒绝 不再由 worker 自动推进,再次点击 = 新一轮;
        审核中 是在途状态,worker 仍通过审查同步推进它,重复点击
        只会复用当前 submission(见 RETRYABLE_FOR_SAME_SUBMISSION,
        V2-P0)。
        """
        return self in TERMINAL_STATES

    def retry_reuses_same_submission(self) -> bool:
        """重复点击/自动重试是否复用现有提交 ID。"""
        return self in RETRYABLE_FOR_SAME_SUBMISSION


# ----------------------------------------------------------------------
# 显式状态转换表(P3 #16,Phase 2 注释更新)。描述同一条「提交」
# 生命周期内允许的边:
#
#   草稿 ─► 待处理 ─► 处理中 ─► 审核中 ─► 已通过
#     │        │         │                 已拒绝
#     │        │         └─► 失败
#     │        └────► 失败
#     └────────► 处理中
#
#   失败 ─► 处理中   (用户修正后再次点击,复用同一提交 ID 重试)
#
#   V2-P0 重复点击语义(不再经过转换表,worker 在 service 层短路处理):
#   审核中 + 提交 PR 仍打开 -> 保持 审核中(清除 已请求,不建新 PR);
#   审核中 + PR 已合/已关   -> 由审查同步置 已通过/已拒绝(同轮完成);
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

# 该行处于这些状态时,再次点击按钮视为对同一提交的处理,绝不开启新一轮:
# - 待处理/处理中/失败:同一次提交的重试,复用提交 ID(V2-P0 前既有语义);
# - 审核中:该提交仍在途(PR 已建),重复点击只在 service 层确认 PR 后
#   保持 审核中/交回审查同步,不会走到 claim(见 service,方案 B,V2-P0)。
RETRYABLE_FOR_SAME_SUBMISSION: frozenset[SubmissionStatus] = frozenset(
    {
        SubmissionStatus.PENDING,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.FAILED,
        SubmissionStatus.REVIEWING,
    }
)

# 终态:该行上一轮提交已收尾,再次点击 = 开启新一轮提交。
# 审核中 不再属于终态:它在途,由 worker 审查同步继续推进,且重复点击
# 只复用当前提交(V2-P0)。
TERMINAL_STATES: frozenset[SubmissionStatus] = frozenset(
    {
        SubmissionStatus.APPROVED,
        SubmissionStatus.REJECTED,
    }
)
