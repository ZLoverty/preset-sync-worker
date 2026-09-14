from __future__ import annotations

from dataclasses import dataclass

from material_worker.domain.profile import MaterialProfile
from material_worker.domain.status import SubmissionStatus


@dataclass
class MaterialSubmission:
    """一次「提交」操作的领域对象(每次处理尝试会重建一个新实例)。

    V3-P5:表格里**没有**「提交 ID」列 —— 一行 = 一个身份,每次提交都提交
    这一行,提交 ID 会被覆盖而失去意义(用户确认)。因此:

    - branch 由**行身份**确定(`<PI Code>@<打印机型号>` × 切片软件),
      同一行永远同一 branch,重复点击/自动重试天然幂等;
    - 本轮提交**不产生任何 id 或时间戳**:文件、PR 正文、commit message
      都只讲「谁在什么时候改了什么」,内部 uuid 一概不写(用户确认:
      提交的文件里 submission/generated_at 没用);
    - 重试计数只存在于 worker 进程内(service 的内存表),重启清零。
    """

    record_id: str
    profile: MaterialProfile

    status: SubmissionStatus = SubmissionStatus.PENDING

    branch_name: str | None = None
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
