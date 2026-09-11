"""SubmissionService 测试:用 fake 的 BitableClient / GitRepository(P7 #35)。

V3 重点:
- 附件导入改为**宽容取值 + 不自动提交**(P3):只反写认得的键,缺键跳过,
  是否提交由用户点按钮决定;
- 提交人 / 提交时间 / 过程记录写进 commit message 与 PR(P7/P8),
  成功后清空「过程记录」列;
- 重复身份(PI Code × 打印机型号 × 切片软件)永久失败(P9);
- 表里没有「提交 ID」「重试次数」列:PR URL 是唯一锚点,重试计数只在内存。
"""
from helpers import (
    DEFAULTS,
    DEFAULT_SUBMITTER,
    DEFAULT_SUBMIT_TIME,
    make_profile as make_valid_profile,
    prusa_ini,
    profile_attachment_json,
    row_fields,
)

from material_worker import fields
from material_worker.adapters.bitable import (
    AttachmentItem,
    BitableClient,
)
from material_worker.adapters.git import GitRepository, PullRequestResult
from material_worker.domain.status import SubmissionStatus
from material_worker.exceptions import BitableError, PermanentError, RetryableError
from material_worker.services.submission_service import (
    CLOSE_REASON_ERROR_DETAIL_MAX,
    CLOSE_REASON_FALLBACK,
    CLOSE_REASON_READ_ERROR,
    JSON_ATTACHMENT_MAX_BYTES,
    JSON_PARSE_ERROR_PREFIX,
    SubmissionService,
)


def make_profile(record_id="rec-1"):
    profile = make_valid_profile()
    profile.source_record_id = record_id
    return profile


class FakeBitable:
    """内存版 BitableClient:记录变更可见,可注入单次回写失败。"""

    def __init__(self, record_id, initial_fields):
        self.records = {record_id: dict(initial_fields)}
        self.fail_next_review = False
        self.mark_reviewing_calls = 0
        self.marked: list[tuple[str, str]] = []  # 终态回写流水
        self.cleared_process_record: list[str] = []
        # 附件导入(file_token -> 原始字节;下载调用记录/可注入失败)
        self.attachments: dict[str, bytes] = {}
        self.download_tokens: list[str] = []
        self.download_error: Exception | None = None

    def record(self, record_id):
        return self.records[record_id]

    def update_record(self, record_id, values):
        self.records[record_id].update(values)

    def clear_request(self, record_id):
        """仅清除 已请求(在途行重复触发打住)。"""
        self.records[record_id][fields.REQUESTED] = False

    def get_record(self, record_id):
        return record_id, dict(self.records[record_id])

    # -- 附件接口(镜像真实 BitableClient) ----------------------------
    def attachment_items(self, row_fields, column=fields.PROFILE_JSON):
        cell = row_fields.get(column)
        if not isinstance(cell, list):
            return []
        items = []
        for entry in cell:
            if not isinstance(entry, dict):
                continue
            if entry.get("file_token") is None or entry.get("name") is None:
                continue
            items.append(
                AttachmentItem(
                    file_token=str(entry["file_token"]),
                    name=str(entry["name"]),
                )
            )
        return items

    def download_attachment(self, file_token):
        self.download_tokens.append(file_token)
        if self.download_error is not None:
            raise self.download_error
        if file_token not in self.attachments:
            raise BitableError(f"附件不存在: {file_token}")
        return self.attachments[file_token]

    def process_record_names(self, row_fields):
        return [i.name for i in self.attachment_items(row_fields, fields.PROCESS_RECORD)]

    # 复用真实实现的展示文本规则(纯静态,无副作用)
    submitter_text = staticmethod(BitableClient.submitter_text)
    submit_time_text = staticmethod(BitableClient.submit_time_text)

    def list_records(self):
        return [(rid, dict(f)) for rid, f in self.records.items()]

    # -- 状态回写 ------------------------------------------------------
    def mark_processing(self, record_id):
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.PROCESSING.value,
                fields.PR_URL: "",
                fields.CLOSE_REASON: "",
                fields.ERROR_MSG: "",
            }
        )

    def mark_reviewing(self, record_id, pull_request_url, *, clear_process_record=False):
        self.mark_reviewing_calls += 1
        if self.fail_next_review:
            self.fail_next_review = False
            raise BitableError("mark_reviewing 模拟失败")
        values = {
            fields.REQUESTED: False,
            fields.STATUS: SubmissionStatus.REVIEWING.value,
            fields.PR_URL: pull_request_url,
            fields.ERROR_MSG: "",
        }
        if clear_process_record:
            values[fields.PROCESS_RECORD] = []
            self.cleared_process_record.append(record_id)
        self.records[record_id].update(values)

    def mark_failed(self, record_id, error_message):
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.FAILED.value,
                fields.ERROR_MSG: error_message,
            }
        )

    def mark_approved(self, record_id):
        self.marked.append((record_id, SubmissionStatus.APPROVED.value))
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.APPROVED.value,
                fields.ERROR_MSG: "",
                fields.CLOSE_REASON: "",
            }
        )

    def mark_rejected(self, record_id, close_reason=""):
        self.marked.append((record_id, SubmissionStatus.REJECTED.value))
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.REJECTED.value,
                fields.ERROR_MSG: "",
                fields.CLOSE_REASON: close_reason,
            }
        )

    def mark_retryable(self, record_id, error_message):
        self.records[record_id].update(
            {
                fields.REQUESTED: True,
                fields.STATUS: SubmissionStatus.PROCESSING.value,
                fields.ERROR_MSG: error_message,
            }
        )


class FakeGitRepository:
    """幂等 fake:同一身份(identity × slicer)只产生一个打开的 PR。"""

    def __init__(self):
        self.pull_requests: dict[str, PullRequestResult] = {}  # pr_url -> result
        self.submit_calls: list[str] = []  # 每次提交的 branch(V3:表里已无提交 ID)
        self.submissions: list[dict] = []  # 每次提交收到的 kwargs
        self._pr_seq = 0
        self.next_error: Exception | None = None
        self.next_error_times = 0
        # 审查同步用:pr_url -> "merged"/"open"/"closed";查不到返回 None
        self.pr_states: dict[str, str] = {}
        self.pr_state_errors: dict[str, Exception] = {}
        self.close_reasons: dict[str, str | None] = {}
        self.close_reason_errors: dict[str, Exception] = {}

    def pr_close_reason(self, pr_url):
        if pr_url in self.close_reason_errors:
            raise self.close_reason_errors[pr_url]
        return self.close_reasons.get(pr_url)

    def pr_state(self, pr_url):
        if pr_url in self.pr_state_errors:
            raise self.pr_state_errors[pr_url]
        return self.pr_states.get(pr_url)

    def submit_profile(
        self,
        profile,
        pull_request_url="",
        submitter="(未记录)",
        submit_time="(未记录)",
        process_records=(),
    ):
        branch = GitRepository.branch_name_for(profile.identity, profile.slicer)
        self.submit_calls.append(branch)
        self.submissions.append(
            {
                "profile": profile,
                "pull_request_url": pull_request_url,
                "submitter": submitter,
                "submit_time": submit_time,
                "process_records": list(process_records),
            }
        )
        if self.next_error and self.next_error_times > 0:
            self.next_error_times -= 1
            raise self.next_error

        if pull_request_url and self.pr_states.get(pull_request_url) == "open":
            return PullRequestResult(
                branch_name=branch, pull_request_url=pull_request_url
            )
        open_pr = next(
            (
                r
                for r in self.pull_requests.values()
                if r.branch_name == branch
                and self.pr_states.get(r.pull_request_url) == "open"
            ),
            None,
        )
        if open_pr is not None:
            return open_pr

        self._pr_seq += 1
        result = PullRequestResult(
            branch_name=branch,
            pull_request_url=f"https://example.invalid/pr/pr-{self._pr_seq}",
        )
        self.pull_requests[result.pull_request_url] = result
        self.pr_states[result.pull_request_url] = "open"
        return result


def snapshot(requested=True, status=None, **extra):
    """一行全必填字段的快照(V3 必填 = 身份四要素 + 继承预设 + 8 项核心参数)。"""
    data = row_fields()
    data[fields.REQUESTED] = requested
    data[fields.STATUS] = (status or SubmissionStatus.PENDING).value
    data.update(extra)
    return data


def reviewing_row(record_id="rec-1", token="sid-1"):
    """审核中的行:PR URL 是本轮 PR 的唯一锚点(V3-P5)。"""
    row = snapshot(status=SubmissionStatus.REVIEWING)
    row[fields.PR_URL] = f"https://example.invalid/pr/{token}"
    row[fields.REQUESTED] = True  # 按钮点击的语义
    return row


def attach(name="profile.json", token="tok-1"):
    return [{"file_token": token, "name": name}]


# ======================================================================
# 提交主流程
# ======================================================================
def test_happy_path():
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False
    assert row[fields.PR_URL].startswith("https://example.invalid/pr/")
    assert not row[fields.ERROR_MSG]
    assert len(repo.pull_requests) == 1
    # V3:branch 由身份派生
    assert {pr.branch_name for pr in repo.pull_requests.values()} == {
        "material/L1002@BBL_P2S_BambuStudio"
    }


def test_claimed_row_clears_previous_pr_url_and_close_reason():
    """V3-P5:claim 清空上一轮的 PR URL / 关闭理由 —— PR URL 只指向本轮。"""
    row = snapshot(status=SubmissionStatus.REJECTED, requested=True)
    row[fields.PR_URL] = "https://example.invalid/pr/old-round"
    row[fields.CLOSE_REASON] = "上一轮的理由"
    bitable = FakeBitable("rec-1", row)
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/old-round"] = "closed"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.PR_URL] != "https://example.invalid/pr/old-round"
    assert row[fields.CLOSE_REASON] == ""
    assert len(repo.pull_requests) == 1  # 新一轮,旧 PR 不复活


def test_validation_failure_never_touches_git():
    bad = snapshot()
    bad.pop(fields.MAX_VOL_SPEED)  # 不完整输入
    bitable = FakeBitable("rec-1", bad)
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert row[fields.REQUESTED] is False
    assert "数据校验失败" in row[fields.ERROR_MSG]
    assert repo.submit_calls == []


def test_git_retryable_failure_keeps_request_alive():
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    repo.next_error = RetryableError("Git API 网络错误")
    repo.next_error_times = 1
    service = SubmissionService(bitable=bitable, repository=repo, max_retries=5)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.PROCESSING.value  # 非永久失败
    assert row[fields.REQUESTED] is True  # 下轮自动重试
    assert "网络错误" in row[fields.ERROR_MSG]
    assert len(repo.pull_requests) == 0


def test_retry_count_lives_in_memory_not_in_table():
    """V3-P5:表里没有「重试次数」列,计数只在本进程内存里。"""
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    repo.next_error = RetryableError("boom")
    repo.next_error_times = 1
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert service._retry_counts == {"rec-1": 1}
    assert "重试次数" not in bitable.records["rec-1"]  # 该列已废弃(V3)

    # 成功后内存计数清空
    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    assert service._retry_counts == {}


def test_retry_exhaustion_marks_failed():
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    repo.next_error = RetryableError("Git API 网络错误")
    repo.next_error_times = 99
    service = SubmissionService(bitable=bitable, repository=repo, max_retries=2)

    for _ in range(3):  # 三次失败,前两次自动重试,第三次超限
        service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert row[fields.REQUESTED] is False
    assert "已自动重试 2 次" in row[fields.ERROR_MSG]


def test_git_permanent_error_marks_failed():
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    repo.next_error = PermanentError("Git API 鉴权失败 (HTTP 401)")
    repo.next_error_times = 1
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert "鉴权失败" in row[fields.ERROR_MSG]


def test_git_ok_but_bitable_update_failed_then_retried():
    """Git 成功但 Bitable 回写失败 -> 重试复用既有 PR,不重复建。"""
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    bitable.fail_next_review = True
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.PROCESSING.value
    assert row[fields.REQUESTED] is True  # 自动重试信号
    assert len(repo.pull_requests) == 1

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert len(repo.pull_requests) == 1  # 复用既有 PR
    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.ERROR_MSG] == ""


def test_claim_lost_lets_the_row_go():
    """claim 回读发现状态被他人改动 -> 本次不处理,绝不贸然提交。"""

    class HijackBitable(FakeBitable):
        def mark_processing(self, record_id):
            super().mark_processing(record_id)
            # 模拟另一 worker 抢走:回读时状态已不是 处理中
            self.records[record_id][fields.STATUS] = SubmissionStatus.REVIEWING.value

    bitable = HijackBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert repo.submit_calls == []


# ======================================================================
# V3-P7/P8:提交人 / 提交时间 / 过程记录
# ======================================================================
def test_submitter_time_and_records_go_into_submission():
    row = snapshot()
    row[fields.SUBMITTER] = [{"id": "u1", "name": DEFAULT_SUBMITTER}]
    row[fields.SUBMIT_TIME] = 1770000000000
    row[fields.PROCESS_RECORD] = attach(name="调参记录.md", token="rec-tok")
    bitable = FakeBitable("rec-1", row)
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    sent = repo.submissions[0]
    assert sent["submitter"] == DEFAULT_SUBMITTER
    assert sent["submit_time"] != "(未记录)"
    assert sent["process_records"] == ["调参记录.md"]


def test_missing_submitter_and_time_fall_back_to_placeholder():
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert repo.submissions[0]["submitter"] == "(未记录)"
    assert repo.submissions[0]["submit_time"] == "(未记录)"


def test_process_record_column_cleared_after_pr_created():
    """V3-P8:附件本体不进 Git -> PR 建立后清空「过程记录」列。"""
    row = snapshot()
    row[fields.PROCESS_RECORD] = attach(name="调参记录.md", token="rec-tok")
    bitable = FakeBitable("rec-1", row)
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert bitable.records["rec-1"][fields.PROCESS_RECORD] == []
    assert bitable.cleared_process_record == ["rec-1"]


def test_process_record_column_untouched_when_empty():
    """没有过程记录时不写该列(不制造无谓的表格变更)。"""
    bitable = FakeBitable("rec-1", snapshot())
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert bitable.cleared_process_record == []


def test_process_record_survives_failed_submission():
    """提交失败(不产生 PR)-> 过程记录必须保留,下一轮重试还用得上。"""
    row = snapshot()
    row[fields.PROCESS_RECORD] = attach(name="调参记录.md", token="rec-tok")
    bitable = FakeBitable("rec-1", row)
    repo = FakeGitRepository()
    repo.next_error = PermanentError("鉴权失败")
    repo.next_error_times = 1
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.FAILED.value
    assert bitable.records["rec-1"][fields.PROCESS_RECORD] != []
    assert bitable.cleared_process_record == []


# ======================================================================
# V3-P9:重复身份
# ======================================================================
def test_duplicate_identity_fails_without_pr():
    bitable = FakeBitable("rec-1", snapshot(requested=True))
    bitable.records["rec-2"] = snapshot(requested=False)
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    records = bitable.list_records()
    service.process_pending_rows(records)

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert "重复" in row[fields.ERROR_MSG]
    assert "rec-2" in row[fields.ERROR_MSG]
    assert repo.submit_calls == []
    # 另一行不受影响(它没请求提交)
    assert bitable.records["rec-2"][fields.STATUS] == SubmissionStatus.PENDING.value


def test_different_slicer_is_a_different_identity():
    """切片软件是身份的一部分:同 PI Code/机型 + 不同切片器 -> 不重复。"""
    bitable = FakeBitable("rec-1", snapshot(requested=True))
    bitable.records["rec-2"] = snapshot(requested=False, **{fields.SLICER: "PrusaSlicer"})
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_pending_rows(bitable.list_records())

    assert repo.submit_calls != []
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value


def test_blank_identity_rows_are_not_duplicates():
    """身份不全的行由必填校验拦截,不参与重复判定。"""
    index = SubmissionService.duplicate_index(
        [("rec-1", {}), ("rec-2", {fields.PI_CODE: "L1002"})]
    )
    assert index == {}


def test_process_pending_rows_isolates_per_record_errors():
    """单行异常不中断本轮其余行(P6 #30)。"""
    bitable = FakeBitable("rec-1", snapshot(requested=True))
    bitable.records["rec-2"] = snapshot(
        requested=True, **{fields.SLICER: "PrusaSlicer"}
    )
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    calls: list[str] = []
    original = service.process_record

    def boom(record_id, fields_snapshot, duplicate_index=None):
        calls.append(record_id)
        if record_id == "rec-1":
            raise RuntimeError("单行炸了")
        return original(record_id, fields_snapshot, duplicate_index)

    service.process_record = boom  # type: ignore[assignment]
    service.process_pending_rows(bitable.list_records())

    assert calls == ["rec-1", "rec-2"]
    assert bitable.records["rec-2"][fields.STATUS] == SubmissionStatus.REVIEWING.value


# ======================================================================
# V2-P0:审核中(在途提交)重复点击 —— 按 PR 实况收敛,绝不产生第二个 PR
# ======================================================================
def test_reviewing_click_with_open_pr_keeps_reviewing_no_second_pr():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "open"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False  # 本轮重复触发就此打住
    assert row[fields.PR_URL].endswith("sid-1")  # PR 锚点不被替换
    assert repo.submit_calls == []  # 完全没走 Git 提交


def test_reviewing_click_merged_pr_then_sync_approves():
    """重复点击时 PR 已合并 -> 不开新一轮;同轮审查同步置 已通过。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "merged"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert repo.submit_calls == []
    assert bitable.records["rec-1"][fields.REQUESTED] is False

    service.sync_reviewing_rows(bitable.list_records())
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.APPROVED.value


def test_reviewing_click_closed_pr_then_sync_rejects():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "closed"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    assert repo.submit_calls == []

    service.sync_reviewing_rows(bitable.list_records())
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REJECTED.value


def test_reviewing_click_pr_query_failure_keeps_request_alive():
    """确认 PR 实况时 Git 查询失败(瞬时)-> 不动行,下轮自然重试。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_state_errors["https://example.invalid/pr/sid-1"] = RetryableError("git down")
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is True  # 保持触发信号,由下一轮再确认
    assert repo.submit_calls == []


def test_reviewing_click_without_pr_url_clears_request_and_warns():
    """审核中但没有 PR 锚点 -> 清 已请求 + 告警,绝不贸然开第二个 PR。"""
    row = reviewing_row()
    row[fields.PR_URL] = ""
    bitable = FakeBitable("rec-1", row)
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    bitable.records["rec-1"][fields.REQUESTED] = True  # 再点一次
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert bitable.records["rec-1"][fields.REQUESTED] is False
    assert ("rec-1", "(无 PR URL)") in service._warned_no_pr
    assert repo.submit_calls == []


def test_fast_double_click_produces_single_pr():
    """快速双击(第二次点击落在下一轮询)只产生一个 PR。"""
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    assert len(repo.pull_requests) == 1
    submitted_branch = repo.submit_calls[0]

    # 第二次点击落在下一轮询(已请求=true,行处于 审核中,PR 打开)
    bitable.records["rec-1"][fields.REQUESTED] = True
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert len(repo.pull_requests) == 1
    assert repo.submit_calls == [submitted_branch]  # 第二次没有再次提交
    assert bitable.records["rec-1"][fields.REQUESTED] is False
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value


# ======================================================================
# 终态行再次点击 = 新一轮提交
# ======================================================================
def test_approved_click_opens_new_submission():
    row = snapshot(status=SubmissionStatus.APPROVED, requested=True)
    row[fields.PR_URL] = "https://example.invalid/pr/old"
    bitable = FakeBitable("rec-1", row)
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/old"] = "merged"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.PR_URL] != "https://example.invalid/pr/old"
    assert len(repo.pull_requests) == 1


def test_rejected_click_opens_new_submission():
    row = snapshot(status=SubmissionStatus.REJECTED, requested=True)
    row[fields.PR_URL] = "https://example.invalid/pr/old"
    bitable = FakeBitable("rec-1", row)
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/old"] = "closed"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert len(repo.pull_requests) == 1


# ======================================================================
# 审查同步
# ======================================================================
def test_review_sync_merged_marks_approved():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "merged"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.APPROVED.value
    assert row[fields.REQUESTED] is False
    assert row[fields.ERROR_MSG] == ""
    assert row[fields.PR_URL].endswith("sid-1")  # 历史保留
    assert bitable.marked == [("rec-1", SubmissionStatus.APPROVED.value)]


def test_review_sync_closed_writes_close_reason_from_git():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "closed"
    repo.close_reasons["https://example.invalid/pr/sid-1"] = "缺材料 ID,请补充后重提"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REJECTED.value
    assert row[fields.CLOSE_REASON] == "缺材料 ID,请补充后重提"


def test_review_sync_closed_without_comments_writes_fallback():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "closed"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows(bitable.list_records())

    assert bitable.records["rec-1"][fields.CLOSE_REASON] == CLOSE_REASON_FALLBACK


def test_review_sync_close_reason_fetch_failure_writes_placeholder():
    """理由读取失败不阻塞状态推进:落 已拒绝,格子里写**可区分**的占位文案。

    V3:原先写空串 —— 跟「PR 确实没人写理由」在表上完全无法区分,而失败
    成因常是永久性的(Gitea token 缺 read:issue -> /issues/{n}/comments
    一直 403),行落终态后 worker 不再回看,理由就无声地丢了。
    """
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "closed"
    repo.close_reason_errors["https://example.invalid/pr/sid-1"] = RetryableError(
        "git down"
    )
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REJECTED.value
    assert row[fields.CLOSE_REASON].startswith(f"({CLOSE_REASON_READ_ERROR}: ")
    assert "git down" in row[fields.CLOSE_REASON]  # 带上异常摘要,便于直接排查
    assert row[fields.CLOSE_REASON] != CLOSE_REASON_FALLBACK  # 与「无评论」可区分


def test_review_sync_close_reason_fetch_failure_truncates_long_detail():
    """占位文案附带的异常摘要超长时截断,避免撑爆单元格。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "closed"
    repo.close_reason_errors["https://example.invalid/pr/sid-1"] = PermanentError(
        "x" * 5000
    )
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows(bitable.list_records())

    reason = bitable.records["rec-1"][fields.CLOSE_REASON]
    assert reason.startswith(f"({CLOSE_REASON_READ_ERROR}: ")
    assert reason.endswith("…)")
    assert len(reason) < CLOSE_REASON_ERROR_DETAIL_MAX + 100


def test_review_sync_merged_writes_no_close_reason():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "merged"
    repo.close_reasons["https://example.invalid/pr/sid-1"] = "不应出现"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.APPROVED.value
    assert row[fields.CLOSE_REASON] == ""


def test_review_sync_open_pr_keeps_reviewing():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "open"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows(bitable.list_records())

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert bitable.marked == []


def test_review_sync_missing_pr_keeps_reviewing():
    """Git 上找不到 PR(如被手动删除)-> 保持 审核中,不误标终态。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.sync_reviewing_rows(bitable.list_records())
    service.sync_reviewing_rows(bitable.list_records())  # 第二次告警不重复

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert bitable.marked == []
    assert service._warned_no_pr == {
        ("rec-1", "https://example.invalid/pr/sid-1")
    }


def test_review_sync_skips_row_without_pr_url():
    """V3-P5:PR URL 为空(非本轮锚点)-> 不猜、不推进,告警一次。"""
    row = reviewing_row()
    row[fields.PR_URL] = ""
    bitable = FakeBitable("rec-1", row)
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "merged"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows(bitable.list_records())

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert bitable.marked == []
    assert repo.pr_state_errors == {}


def test_review_sync_isolates_per_record_errors():
    """单行 Git 查询失败不阻塞其余行;失败行保持 审核中 下轮自愈。"""
    bitable = FakeBitable("rec-1", reviewing_row("rec-1", "sid-1"))
    bitable.records["rec-2"] = reviewing_row("rec-2", "sid-2")
    repo = FakeGitRepository()
    repo.pr_states["https://example.invalid/pr/sid-1"] = "merged"
    repo.pr_states["https://example.invalid/pr/sid-2"] = "merged"
    repo.pr_state_errors["https://example.invalid/pr/sid-2"] = RetryableError(
        "git 查询失败"
    )
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows(bitable.list_records())

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.APPROVED.value
    assert bitable.records["rec-2"][fields.STATUS] == SubmissionStatus.REVIEWING.value


def test_exception_does_not_leak_from_service():
    """瞬态路径的 mark_retryable 异常(bitable 也挂了)会冒泡给 worker 兜底。"""
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    repo.next_error = PermanentError("boom")
    repo.next_error_times = 1
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.FAILED.value

    class DownBitable:
        def attachment_items(self, *a, **k):
            return []

        def mark_retryable(self, *a, **k):
            raise BitableError("bitable down")

        def mark_failed(self, *a, **k):
            raise BitableError("bitable down")

    repo2 = FakeGitRepository()
    repo2.next_error = RetryableError("git down")
    repo2.next_error_times = 1
    service2 = SubmissionService(bitable=DownBitable(), repository=repo2)  # type: ignore[arg-type]

    try:
        service2.process_record("rec-1", snapshot())
        raise AssertionError("应抛出异常交由 worker 兜底")
    except BitableError:
        pass


# ======================================================================
# V3-P3:附件反写 —— 宽容取值、只填空、**不自动提交**
# ======================================================================
def valid_file(**overrides):
    """全键的附件(含单选列键 —— 它们不该被反写)。"""
    return profile_attachment_json(**overrides) + "\n"


def json_draft_row(attachment=None, status="", requested=False, **extra):
    """未进入提交生命周期的行(状态空/草稿),标准字段留空。"""
    data = {
        fields.REQUESTED: requested,
        fields.STATUS: status,
        fields.PROFILE_JSON: attachment,
    }
    data.update(extra)
    return data


def test_backfill_fills_blanks_but_never_auto_submits():
    """V3-P3:反写认得的键,但**不置 已请求** —— 是否提交由用户点按钮决定。"""
    bitable = FakeBitable(
        "rec-1",
        json_draft_row(attachment=attach(), **{fields.ERROR_MSG: "旧的解析错误"}),
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.NOZZLE_TEMP] == 220
    assert row[fields.MAX_VOL_SPEED] == DEFAULTS["filament_max_volumetric_speed"]
    assert row[fields.INHERITS] == "Panchroma PLA"
    assert row[fields.ERROR_MSG] == ""  # 成功清掉历史错误
    assert row[fields.REQUESTED] is False  # **不自动提交**
    assert row[fields.STATUS] in ("", SubmissionStatus.DRAFT.value)
    assert repo.submit_calls == []


def test_backfill_never_writes_single_select_columns():
    """V3-P3:身份三要素 + 调参方法版本是单选列,必须人工在下拉中选择。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    # 附件里带了这些键(值故意与下拉选项不同),也不该被反写
    bitable.attachments["tok-1"] = valid_file(
        pi_code="HACKED", printer_model="HACKED X", slicer="HACKED",
        pm_method_version="HACKED",
    ).encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    for column in (
        fields.PI_CODE,
        fields.PRINTER_MODEL,
        fields.SLICER,
        fields.PM_METHOD_VERSION,
    ):
        assert row.get(column) in (None, ""), column


def test_backfill_is_lenient_about_missing_keys():
    """V3-P3:附件缺键 -> 跳过该键,不报错(与 V2「缺必填即失败」相反)。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = valid_file(
        nozzle_temperature=None,   # 从附件里删掉
        filament_flow_ratio=None,
        inherits=None,
    ).encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.ERROR_MSG] == ""
    assert row.get(fields.NOZZLE_TEMP) in (None, "")  # 缺键 -> 留空
    assert row[fields.BED_TEMP] == 55  # 认得的键照常反写


def test_backfill_silently_skips_attachment_with_no_known_keys():
    """一个关心的键都没认出来 -> 静默跳过(不写错误,不反写)。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = b'{"unrelated": 1, "vendor": "x"}'
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row.get(fields.ERROR_MSG) in ("", None)
    assert row.get(fields.BED_TEMP) in (None, "")
    assert row[fields.REQUESTED] is False


def test_backfill_only_fills_blank_cells_keeps_manual_values():
    bitable = FakeBitable(
        "rec-1",
        json_draft_row(attachment=attach(), **{fields.NOZZLE_TEMP: 200}),
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.NOZZLE_TEMP] == 200  # 手填值不被覆盖
    assert row[fields.BED_TEMP] == 55  # 空位由附件填充


def test_backfill_accepts_realistic_preset_json():
    """真实 BambuStudio 系 JSON(附加键/数组值)-> 附加键忽略,数组单值可解。"""
    payload = valid_file(
        cool_plate_temp=["35"],
        filament_type="PLA",
        filament_id="PMPL27",
        **{"from": "User", "type": "filament"},
    )
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = payload.encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.ERROR_MSG] == ""
    assert row[fields.NOZZLE_TEMP] == 220


def test_backfill_reads_nozzle_temp_from_multi_slot_array_with_nil():
    """实测回归:`nozzle_temperature: ["300", "nil"]` 必须取到 300。

    多喷头数组的未启用槽位写 `nil`,旧逻辑「数组含 nil 即跳过」会把整个
    键丢掉 —— 用户实测 nozzle_temperature 反写不出来就是这个原因。
    现在是先剔 nil 槽位、再要求剩余值一致。
    """
    payload = valid_file(nozzle_temperature=["300", "nil"])
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = payload.encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.NOZZLE_TEMP] == 300.0
    assert row.get(fields.ERROR_MSG) in ("", None)


def test_backfill_skips_ambiguous_multi_nozzle_array():
    """多喷头取值真的不同 -> 跳过该键,绝不静默挑一个。

    同时验证「跳过」不等于「失败」:其余键照常反写。
    """
    payload = valid_file(nozzle_temperature=["220", "240"])
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = payload.encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row.get(fields.NOZZLE_TEMP) in (None, "")
    assert row[fields.BED_TEMP] == 55  # 其余键不受影响


def test_backfill_imports_prusa_ini():
    """V3-P6:上传 PrusaSlicer `.ini` 也能反写(唯一的切片器知识在 worker 里)。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach("profile.ini")))
    bitable.attachments["tok-1"] = prusa_ini().encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.NOZZLE_TEMP] == 200  # 稳态温度优先(不是 210)
    assert row[fields.BED_TEMP] == 60
    assert row[fields.FLOW_RATIO] == 0.99
    assert row[fields.INHERITS] == "Prusament PLA @COREONE"
    # `filament_retract_length = nil` -> 跳过,玻璃化温度/压力提前无对等键
    assert row.get(fields.RETRACTION_LENGTH) in (None, "")
    assert row.get(fields.VITRIFICATION) in (None, "")
    assert row.get(fields.PRESSURE_ADVANCE) in (None, "")
    assert row[fields.ERROR_MSG] == ""


def test_backfill_rejects_unsupported_extension():
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach("photo.png")))
    bitable.attachments["tok-1"] = b"png"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.ERROR_MSG].startswith(JSON_PARSE_ERROR_PREFIX)
    assert ".json" in row[fields.ERROR_MSG] and ".ini" in row[fields.ERROR_MSG]
    assert bitable.download_tokens == []  # 规则不符,连下载都不发生


def test_backfill_rejects_multiple_attachments():
    bitable = FakeBitable(
        "rec-1", json_draft_row(attachment=attach() + attach(token="tok-2"))
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    bitable.attachments["tok-2"] = valid_file().encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    assert "恰好挂 1 个" in bitable.records["rec-1"][fields.ERROR_MSG]
    assert bitable.download_tokens == []


def test_backfill_broken_json_writes_error_and_no_partial_data():
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = b"{broken json"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())
    service.backfill_pending_json_rows(bitable.list_records())  # 同 token 不再重试

    row = bitable.records["rec-1"]
    assert row[fields.ERROR_MSG].startswith(JSON_PARSE_ERROR_PREFIX)
    assert "合法 JSON" in row[fields.ERROR_MSG]
    assert row.get(fields.NOZZLE_TEMP) is None  # 无部分数据
    assert bitable.download_tokens == ["tok-1"]


def test_backfill_replaced_attachment_retries():
    """同一 token 失败后不再重试;用户替换附件(新 token)后自然重试。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach(token="bad-1")))
    bitable.attachments["bad-1"] = b"{broken json"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())
    assert bitable.records["rec-1"][fields.ERROR_MSG].startswith(
        JSON_PARSE_ERROR_PREFIX
    )

    bitable.records["rec-1"][fields.PROFILE_JSON] = attach(token="good-1")
    bitable.attachments["good-1"] = valid_file().encode("utf-8")
    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.NOZZLE_TEMP] == 220
    assert row[fields.ERROR_MSG] == ""
    assert bitable.download_tokens == ["bad-1", "good-1"]


def test_backfill_skips_rows_already_in_lifecycle():
    """已进入提交生命周期的行(处理中/审核中/失败…)不碰。"""
    for status in (
        SubmissionStatus.PROCESSING,
        SubmissionStatus.REVIEWING,
        SubmissionStatus.FAILED,
        SubmissionStatus.APPROVED,
    ):
        bitable = FakeBitable(
            "rec-1",
            json_draft_row(attachment=attach(), status=status.value),
        )
        bitable.attachments["tok-1"] = valid_file().encode("utf-8")
        service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

        service.backfill_pending_json_rows(bitable.list_records())

        assert bitable.download_tokens == [], status
        assert bitable.records["rec-1"].get(fields.NOZZLE_TEMP) is None, status


def test_backfill_skips_rows_with_complete_fields():
    """canonical 列全满 -> 视为人工填写完成,不解析附件(内容非法也不读)。"""
    bitable = FakeBitable(
        "rec-1",
        json_draft_row(
            attachment=attach(),
            **row_fields(
                **{
                    fields.FILAMENT_DENSITY: 1.17,
                    fields.VITRIFICATION: 60,
                    fields.RETRACTION_LENGTH: 0.4,
                    fields.PRESSURE_ADVANCE: 0.02,
                }
            ),
        ),
    )
    bitable.attachments["tok-1"] = b"{broken json"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    assert bitable.download_tokens == []


def test_backfill_reads_attachment_when_only_optional_columns_blank():
    """必填已齐、可选列空 -> 附件仍值得读一次(可选值正是附件常带的内容)。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach(), **row_fields()))
    bitable.attachments["tok-1"] = valid_file(
        filament_density=1.17, temperature_vitrification=60
    ).encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    row = bitable.records["rec-1"]
    assert row[fields.FILAMENT_DENSITY] == 1.17
    assert row[fields.VITRIFICATION] == 60
    assert row[fields.NOZZLE_TEMP] == DEFAULTS["nozzle_temperature"]  # 不覆盖


def test_backfill_oversize_attachment_fails():
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = b"x" * (JSON_ATTACHMENT_MAX_BYTES + 1)
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows(bitable.list_records())

    assert "大小上限" in bitable.records["rec-1"][fields.ERROR_MSG]


def test_backfill_permanent_download_failure_no_retry():
    """非瞬时下载失败(如媒体已失效)-> 记 attempted,不每轮重试。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = b"x"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    bitable.download_error = BitableError("media gone")
    service.backfill_pending_json_rows(bitable.list_records())
    service.backfill_pending_json_rows(bitable.list_records())

    assert "media gone" in bitable.records["rec-1"][fields.ERROR_MSG]
    assert bitable.download_tokens == ["tok-1"]


def test_backfill_retryable_download_failure_retries_next_poll():
    """瞬时下载失败(网络/限流)-> 写错误信息但不记 attempted,下轮重试。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    bitable.download_error = RetryableError("网络抖动")
    service.backfill_pending_json_rows(bitable.list_records())
    assert "自动重试" in bitable.records["rec-1"][fields.ERROR_MSG]

    bitable.download_error = None
    service.backfill_pending_json_rows(bitable.list_records())

    assert bitable.records["rec-1"][fields.NOZZLE_TEMP] == 220
    assert bitable.download_tokens == ["tok-1", "tok-1"]


def test_backfill_isolates_per_record_errors():
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.records["rec-2"] = json_draft_row(attachment=attach(token="tok-2"))
    bitable.attachments["tok-2"] = valid_file().encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    boom = service._backfill_one
    calls: list[str] = []

    def wrapper(record_id, row_fields):
        calls.append(record_id)
        if record_id == "rec-1":
            raise RuntimeError("单行炸了")
        return boom(record_id, row_fields)

    service._backfill_one = wrapper  # type: ignore[assignment]
    service.backfill_pending_json_rows(bitable.list_records())

    assert calls == ["rec-1", "rec-2"]
    assert bitable.records["rec-2"][fields.NOZZLE_TEMP] == 220


# ======================================================================
# 提交路径的附件兜底(V3-P3:best-effort,附件问题绝不阻塞提交)
# ======================================================================
def manual_identity_row(**extra):
    """只手工选了身份四要素(单选列)的行 —— 参数全靠附件导入。"""
    return json_draft_row(
        attachment=attach(),
        requested=True,
        **{
            fields.PI_CODE: "L1002",
            fields.PRINTER_MODEL: "BBL P2S",
            fields.SLICER: "BambuStudio",
            fields.PM_METHOD_VERSION: "v1",
            **extra,
        },
    )


def test_requested_row_with_attachment_fills_and_submits_in_one_call():
    """选了身份 + 挂合法附件 -> 同一次 process_record 内反写 + 开 PR。"""
    bitable = FakeBitable("rec-1", manual_identity_row())
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.NOZZLE_TEMP] == 220  # 反写已落表
    assert row[fields.INHERITS] == "Panchroma PLA"
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False
    assert row[fields.ERROR_MSG] == ""
    assert len(repo.pull_requests) == 1
    assert bitable.download_tokens == ["tok-1"]


def test_empty_identity_is_not_backfillable_from_attachment():
    """V3-P3:身份四要素是单选列,附件一律不反写 —— 没手选就必须报缺。"""
    bitable = FakeBitable(
        "rec-1", json_draft_row(attachment=attach(), requested=True)
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert "数据校验失败" in row[fields.ERROR_MSG]
    for column in (
        fields.PI_CODE,
        fields.PRINTER_MODEL,
        fields.SLICER,
        fields.PM_METHOD_VERSION,
    ):
        assert column in row[fields.ERROR_MSG]
    assert repo.submit_calls == []


def test_requested_row_partial_manual_values_not_overwritten_at_claim():
    bitable = FakeBitable(
        "rec-1", manual_identity_row(**{fields.NOZZLE_TEMP: 205})
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.NOZZLE_TEMP] == 205  # 手填值优先
    assert row[fields.BED_TEMP] == 55  # 其余空白由附件填充
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value


def test_requested_row_invalid_attachment_does_not_block_manual_submission():
    """V3-P3:附件坏了不再是提交失败的理由 —— 表格数据齐全就照常提交。"""
    row = snapshot(requested=True)
    row[fields.PROFILE_JSON] = attach()
    bitable = FakeBitable("rec-1", row)
    bitable.attachments["tok-1"] = b"{broken json"
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert len(repo.pull_requests) == 1


def test_requested_row_broken_attachment_reports_missing_fields():
    """附件坏 + 表格有空位 -> 报「缺少必填字段」(用户看得懂,修正后可重试)。"""
    bitable = FakeBitable(
        "rec-1", json_draft_row(attachment=attach(), requested=True)
    )
    bitable.attachments["tok-1"] = b"{broken json"
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert "数据校验失败" in row[fields.ERROR_MSG]
    assert "缺少必填字段" in row[fields.ERROR_MSG]
    assert repo.submit_calls == []


def test_requested_row_blank_without_attachment_fails_with_guidance():
    bitable = FakeBitable("rec-1", json_draft_row(requested=True))
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert "数据校验失败" in row[fields.ERROR_MSG]
    assert "缺少必填字段" in row[fields.ERROR_MSG]
    assert repo.submit_calls == []


def test_requested_row_complete_fields_still_imports_missing_optional():
    """必填已齐但可选列空 + 挂附件 -> 附件仍会被读(补可选值),不阻塞提交。"""
    row = snapshot(requested=True)
    row[fields.PROFILE_JSON] = attach()
    bitable = FakeBitable("rec-1", row)
    bitable.attachments["tok-1"] = valid_file(
        filament_density=1.17, temperature_vitrification=60
    ).encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.FILAMENT_DENSITY] == 1.17  # 可选值由附件补齐
    assert row[fields.VITRIFICATION] == 60


def test_fully_filled_row_never_reads_attachment_at_claim():
    """canonical 列全满(含可选)-> 视为人工填写完成,附件连读都不读。"""
    row = snapshot(requested=True)
    row[fields.PROFILE_JSON] = attach()
    row.update(
        {
            fields.FILAMENT_DENSITY: 1.17,
            fields.VITRIFICATION: 60,
            fields.RETRACTION_LENGTH: 0.4,
            fields.PRESSURE_ADVANCE: 0.02,
        }
    )
    bitable = FakeBitable("rec-1", row)
    bitable.attachments["tok-1"] = b"{broken json"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert bitable.download_tokens == []
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value


def test_requested_row_transient_download_failure_still_retries_submission():
    """claim 时附件下载瞬时失败 -> 不阻塞提交:表格数据齐全就照常开 PR。"""
    row = snapshot(requested=True)
    row[fields.PROFILE_JSON] = attach()
    bitable = FakeBitable("rec-1", row)
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    bitable.download_error = RetryableError("网络抖动")
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert len(repo.pull_requests) == 1


def test_requested_row_blank_with_ini_attachment_submits():
    """整行只有附件(Prusa .ini)+ 手选身份 -> 一次点击直达 PR。"""
    row = json_draft_row(attachment=attach("profile.ini"), requested=True)
    row.update(
        {
            fields.PI_CODE: "L1002",
            fields.PRINTER_MODEL: "Prusa Core One",
            fields.SLICER: "PrusaSlicer",
            fields.PM_METHOD_VERSION: "v1",
        }
    )
    bitable = FakeBitable("rec-1", row)
    bitable.attachments["tok-1"] = prusa_ini().encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.INHERITS] == "Prusament PLA @COREONE"
    assert row[fields.NOZZLE_TEMP] == 200
    assert len(repo.pull_requests) == 1
    submission = repo.submissions[0]
    assert submission["profile"].repo_relative_path() == (
        "preset/L1002/Prusa/Core One/PrusaSlicer/L1002@Prusa Core One.json"
    )
