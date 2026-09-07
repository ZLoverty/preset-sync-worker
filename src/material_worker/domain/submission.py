from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from material_worker import fields
from material_worker.domain.profile import MaterialProfile
from material_worker.domain.status import SubmissionStatus


@dataclass
class MaterialSubmission:
    """一次「提交」操作的领域对象(每次处理尝试会重建一个新实例)。

    注意:处理失败后的自动重试/用户重按按钮,会复用同一
    ``submission_id``(branch 因此确定不变),但每次尝试都会新建本对象,
    初始状态恒为 PENDING —— 表格行的真实状态以 Bitable 为准。
    """

    submission_id: str
    record_id: str
    profile: MaterialProfile
    submitted_at: datetime

    status: SubmissionStatus = SubmissionStatus.PENDING
    retry_count: int = 0

    branch_name: str | None = field(default=None)
    pull_request_url: str | None = None
    error_message: str | None = None

    def _transition_to(self, target: SubmissionStatus) -> None:
        self.status.assert_can_transition(target)
        self.status = target

    def mark_processing(self) -> None:
        self._transition_to(SubmissionStatus.PROCESSING)

    def mark_reviewing(self, pull_request_url: str) -> None:
        self._transition_to(SubmissionStatus.REVIEWING)
        self.pull_request_url = pull_request_url
        self.error_message = None

    def mark_failed(self, message: str) -> None:
        self._transition_to(SubmissionStatus.FAILED)
        self.error_message = message


def new_submission_id() -> str:
    return uuid.uuid4().hex


def resolve_submission_id(current_fields: dict[str, Any]) -> str:
    """决定本次点击复用还是新开一个提交 ID(P3 #18 / V2-P0 语义更新)。

    规则(按行当前状态区分):
    - 已有提交 ID 且行处于 待处理/处理中/失败/审核中
      -> 复用该 ID(同一次提交的处理,保证不会产生第二个 branch/PR)。
      注意:审核中 行的重复点击在 service 层短路处理(按 PR 实况收敛),
      不会走到本函数;这里把 审核中 归入复用组只表达"它是在途提交",
      唯一例外是「审核中 但无提交 ID」的行(没有可复用的在途提交);
    - 行处于 已通过/已拒绝(上一轮提交已收尾,再次点击 = 新一轮)
      或无提交 ID -> 生成新 ID,开启新一轮提交。
    """
    status = SubmissionStatus.from_table(current_fields.get(fields.STATUS))
    existing = current_fields.get(fields.SUBMISSION_ID)

    if existing is not None and str(existing).strip() != "":
        if status.retry_reuses_same_submission():
            return str(existing).strip()
        # 行属于上一轮已收尾的提交:即使带旧 ID 也开新一轮。

    return new_submission_id()
