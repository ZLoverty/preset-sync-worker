"""SubmissionService 测试:用 fake 的 BitableClient / GitRepository(P7 #35)。"""
import json

from helpers import (
    DEFAULTS,
    make_profile as make_valid_profile,
    profile_attachment_json,
    row_fields,
)

from material_worker import fields
from material_worker.adapters.bitable import AttachmentItem, BitableClient
from material_worker.adapters.git import GitRepository, PullRequestResult
from material_worker.domain.status import SubmissionStatus
from material_worker.exceptions import BitableError, PermanentError, RetryableError
from material_worker.services.submission_service import (
    CLOSE_REASON_FALLBACK,
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
        self.marked: list[tuple[str, str]] = []
        # V2-P2:附件导入(file_token -> 原始字节;下载调用记录/可注入失败)
        self.attachments: dict[str, bytes] = {}
        self.download_tokens: list[str] = []
        self.download_error: Exception | None = None

    def record(self, record_id):
        return self.records[record_id]

    def update_record(self, record_id, values):
        self.records[record_id].update(values)

    def clear_request(self, record_id):
        """V2-P0:仅清除 已请求(在途行重复触发打住)。"""
        self.records[record_id][fields.REQUESTED] = False

    def get_record(self, record_id):
        return record_id, dict(self.records[record_id])

    # V2-P2:镜像真实 BitableClient 的附件接口(供 service 调用)
    def attachment_items(self, row_fields):
        """与真实实现一致:附件列原始值 -> AttachmentItem 列表。"""
        cell = row_fields.get(fields.PROFILE_JSON)
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

    def list_records(self):
        """全表快照(V2-P2:附件导入扫描用,镜像真实 list_records)。"""
        return [(rid, dict(f)) for rid, f in self.records.items()]

    def list_reviewing_records(self):
        target = SubmissionStatus.REVIEWING.value
        return [
            (rid, dict(f))
            for rid, f in self.records.items()
            if str(f.get(fields.STATUS) or "").strip() == target
        ]

    def mark_processing(self, record_id, submission_id):
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.PROCESSING.value,
                fields.SUBMISSION_ID: submission_id,
                fields.CLOSE_REASON: "",  # V2-P3:新一轮 claim 清空旧理由
            }
        )

    def mark_reviewing(self, record_id, pull_request_url):
        self.mark_reviewing_calls += 1
        if self.fail_next_review:
            self.fail_next_review = False
            raise BitableError("mark_reviewing 模拟失败")
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.REVIEWING.value,
                fields.PR_URL: pull_request_url,
                fields.ERROR_MSG: "",
                fields.RETRY_COUNT: 0,
            }
        )

    def mark_failed(self, record_id, error_message):
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.FAILED.value,
                fields.ERROR_MSG: error_message,
                fields.RETRY_COUNT: 0,
            }
        )

    def mark_approved(self, record_id):
        self.marked.append((record_id, SubmissionStatus.APPROVED.value))
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.APPROVED.value,
                fields.ERROR_MSG: "",
                fields.RETRY_COUNT: 0,
                fields.CLOSE_REASON: "",  # V2-P3:已通过 不写理由
            }
        )

    def mark_rejected(self, record_id, close_reason=""):
        # V2-P3:close_reason 由 service 从 Git 侧同步(取不到则留空)
        self.marked.append((record_id, SubmissionStatus.REJECTED.value))
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.REJECTED.value,
                fields.ERROR_MSG: "",
                fields.RETRY_COUNT: 0,
                fields.CLOSE_REASON: close_reason,
            }
        )

    def mark_retryable(self, record_id, submission_id, error_message, retry_count):
        self.records[record_id].update(
            {
                fields.REQUESTED: True,
                fields.STATUS: SubmissionStatus.PROCESSING.value,
                fields.SUBMISSION_ID: submission_id,
                fields.ERROR_MSG: error_message,
                fields.RETRY_COUNT: retry_count,
            }
        )


class FakeGitRepository:
    """幂等 fake:同一 submission_id 只产生一个 PR。"""

    def __init__(self):
        self.pull_requests: dict[str, PullRequestResult] = {}
        self.submit_calls: list[str] = []
        self.next_error: Exception | None = None
        self.next_error_times = 0
        # 审查同步用:submission_id -> "merged"/"open"/"closed";查不到返回 None
        self.pr_states: dict[str, str] = {}
        self.pr_state_errors: dict[str, Exception] = {}
        # V2-P3:submission_id -> 关闭理由;缺省 None(无评论,service 写兜底)
        self.close_reasons: dict[str, str | None] = {}
        self.close_reason_errors: dict[str, Exception] = {}

    def pr_close_reason(self, submission_id):
        """V2-P3:取关闭理由(最后一条评论正文);查不到返回 None。"""
        if submission_id in self.close_reason_errors:
            raise self.close_reason_errors[submission_id]
        return self.close_reasons.get(submission_id)

    def submit_profile(self, submission_id, profile):
        self.submit_calls.append(submission_id)
        if self.next_error and self.next_error_times > 0:
            self.next_error_times -= 1
            raise self.next_error
        existing = self.pull_requests.get(submission_id)
        if existing is not None:
            return existing
        result = PullRequestResult(
            branch_name=f"material/{submission_id}",
            pull_request_url=f"https://example.invalid/pr/{submission_id}",
        )
        self.pull_requests[submission_id] = result
        return result

    def submission_pr_state(self, submission_id):
        if submission_id in self.pr_state_errors:
            raise self.pr_state_errors[submission_id]
        return self.pr_states.get(submission_id)


def snapshot(record_id="rec-1", requested=True, status=None, submission_id=None,
             retry_count=None, **extra):
    """V2-P4:全必填字段行(V2-P4 起必填 = 品名/品牌/机型/切片器 + 10 项耗材参数)。"""
    data = row_fields()
    data[fields.REQUESTED] = requested
    data[fields.STATUS] = (status or SubmissionStatus.PENDING).value
    if submission_id is not None:
        data[fields.SUBMISSION_ID] = submission_id
    if retry_count is not None:
        data[fields.RETRY_COUNT] = retry_count
    data.update(extra)
    return data


def test_happy_path():
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False
    assert row[fields.PR_URL].startswith("https://example.invalid/pr/")
    assert row[fields.RETRY_COUNT] == 0
    assert not row[fields.ERROR_MSG]
    assert len(repo.pull_requests) == 1


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
    assert row[fields.RETRY_COUNT] == 1
    assert "网络错误" in row[fields.ERROR_MSG]
    assert len(repo.pull_requests) == 0


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
    assert row[fields.RETRY_COUNT] == 0


def test_duplicate_processing_same_submission_creates_one_pr():
    """同一提交重复处理(P3 #17 / P4 #19):提交 ID 复用 -> 不产生第二个 PR。"""
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    # 第一轮:正常完成,生成提交 ID
    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    sid = bitable.records["rec-1"][fields.SUBMISSION_ID]
    assert len(repo.pull_requests) == 1

    # 第二轮:状态被人工/外部置回 待处理 + 已请求=true(重复请求),
    # 但 提交 ID 保留 -> 同一提交重试 -> 复用既有 PR,不重复建 PR
    retried = snapshot(
        status=SubmissionStatus.PENDING,
        submission_id=sid,
    )
    service.process_record("rec-1", retried)

    assert len(repo.pull_requests) == 1
    assert repo.submit_calls[-1] == sid
    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.PR_URL].endswith(sid)


def test_git_ok_but_bitable_update_failed_then_retried():
    """P4 #22:Git 成功但 Bitable 回写失败 -> 重试复用既有 PR,不重复建。"""
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    # 第一次:git 成功,但回写 mark_reviewing 失败(模拟网络/瞬时故障)
    bitable.fail_next_review = True
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.PROCESSING.value
    assert row[fields.REQUESTED] is True  # 自动重试信号
    assert row[fields.RETRY_COUNT] == 1
    sid = row[fields.SUBMISSION_ID]

    # 第二次:自动重试,同一提交 ID -> submit_profile 幂等返回既有 PR
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert len(repo.pull_requests) == 1
    assert repo.submit_calls == [sid, sid]
    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.ERROR_MSG] == ""


# ----------------------------------------------------------------------
# V2-P0:审核中(在途提交)重复点击 —— 按 PR 实况收敛,绝不产生第二个 PR
# ----------------------------------------------------------------------

def test_reviewing_click_with_open_pr_keeps_reviewing_no_second_pr():
    """V2-P0:审核中 + PR 仍打开时再次点击 -> 不创建第二个 PR/新提交。"""
    bitable = FakeBitable("rec-1", reviewing_row("rec-1", "sub-old"))
    repo = FakeGitRepository()
    repo.pr_states["sub-old"] = "open"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False  # 本轮重复触发就此打住
    assert row[fields.SUBMISSION_ID] == "sub-old"  # 提交 ID 不被替换
    assert repo.submit_calls == []  # 完全没走 Git 提交
    assert len(repo.pull_requests) == 0


def test_reviewing_click_merged_pr_then_sync_approves():
    """V2-P0:重复点击时 PR 已合并 -> 不开新一轮;同轮审查同步置 已通过。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "merged"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False
    assert repo.submit_calls == []

    service.sync_reviewing_rows()  # 同轮随后的审查同步落终态
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.APPROVED.value
    assert len(repo.pull_requests) == 0


def test_reviewing_click_closed_pr_then_sync_rejects():
    """V2-P0:重复点击时 PR 已关闭未合并 -> 同轮审查同步置 已拒绝,不新开轮。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "closed"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert repo.submit_calls == []
    assert bitable.records["rec-1"][fields.REQUESTED] is False

    service.sync_reviewing_rows()
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REJECTED.value
    assert bitable.marked == [("rec-1", SubmissionStatus.REJECTED.value)]


def test_reviewing_click_pr_query_failure_keeps_request_alive():
    """V2-P0:确认 PR 实况时 Git 查询失败(瞬时)-> 不动行,下轮自然重试。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_state_errors["sid-1"] = RetryableError("git down")
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is True  # 保持触发信号,由下一轮再确认
    assert repo.submit_calls == []


def test_reviewing_click_pr_missing_clears_request_and_warns_once():
    """V2-P0:Git 上找不到在途 PR -> 清 已请求 + 保持 审核中(人工处理)。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    # 第二次点击(已请求 重新置 true):告警只发一次,行保持 审核中
    bitable.records["rec-1"][fields.REQUESTED] = True
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False
    assert service._warned_no_pr == {("rec-1", "sid-1")}


def test_reviewing_click_without_submission_id_starts_new_round():
    """V2-P0:审核中 但无提交 ID(没有在途提交可对照)-> 按常规开新一轮。"""
    row = reviewing_row()
    row[fields.SUBMISSION_ID] = None  # type: ignore[assignment]
    bitable = FakeBitable("rec-1", row)
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False
    assert len(repo.pull_requests) == 1  # 只产生这一轮的一个 PR


def test_fast_double_click_produces_single_pr():
    """V2-P0/§3.5:快速双击(第二次点击落在下一轮询)只产生一个 PR。"""
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    # 第一次点击:正常处理 -> 审核中 + 一个 PR
    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    sid = bitable.records["rec-1"][fields.SUBMISSION_ID]
    assert len(repo.pull_requests) == 1

    # 第二次点击落在下一轮询(已请求=true,行处于 审核中,PR 打开)
    bitable.records["rec-1"][fields.REQUESTED] = True
    repo.pr_states[sid] = "open"
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    assert len(repo.pull_requests) == 1  # PR 数量不增加
    assert repo.submit_calls == [sid]  # 第二次点击没有再次提交
    assert bitable.records["rec-1"][fields.REQUESTED] is False
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value


# ----------------------------------------------------------------------
# V2-P0/Q1:已通过/已拒绝(终态)再次点击 = 新一轮提交(语义保持不变)
# ----------------------------------------------------------------------

def test_approved_click_opens_new_submission():
    """已通过 后再点按钮 = 新一轮提交(内容迭代),生成新提交 ID/PR。"""
    bitable = FakeBitable(
        "rec-1",
        snapshot(status=SubmissionStatus.APPROVED, submission_id="sub-old"),
    )
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    sid = row[fields.SUBMISSION_ID]
    assert sid != "sub-old"
    assert len(repo.pull_requests) == 1
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value


def test_rejected_click_opens_new_submission():
    """已拒绝 后再点按钮 = 新一轮提交(修正后重提),生成新提交 ID/PR。"""
    bitable = FakeBitable(
        "rec-1",
        snapshot(status=SubmissionStatus.REJECTED, submission_id="sub-old"),
    )
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    sid = row[fields.SUBMISSION_ID]
    assert sid != "sub-old"
    assert len(repo.pull_requests) == 1
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value


def reviewing_row(record_id="rec-1", submission_id="sid-1"):
    row = snapshot(
        record_id=record_id,
        status=SubmissionStatus.REVIEWING,
        submission_id=submission_id,
    )
    row[fields.PR_URL] = f"https://example.invalid/pr/{submission_id}"
    return row


def test_review_sync_merged_marks_approved():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "merged"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows()

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.APPROVED.value
    assert row[fields.REQUESTED] is False
    assert row[fields.ERROR_MSG] == ""
    assert row[fields.PR_URL].endswith("sid-1")  # 历史保留
    assert bitable.marked == [("rec-1", SubmissionStatus.APPROVED.value)]


def test_review_sync_closed_unmerged_marks_rejected():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "closed"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows()

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REJECTED.value
    assert bitable.marked == [("rec-1", SubmissionStatus.REJECTED.value)]


# ----------------------------------------------------------------------
# V2-P3:PR 关闭理由同步(已拒绝 行回写 Git 关闭前最后一条评论正文)
# ----------------------------------------------------------------------

def test_review_sync_closed_writes_close_reason_from_git():
    """关闭前最后一条评论正文作为关闭理由回写。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "closed"
    repo.close_reasons["sid-1"] = "缺材料 ID,请补充后重提"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows()

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REJECTED.value
    assert row[fields.CLOSE_REASON] == "缺材料 ID,请补充后重提"


def test_review_sync_closed_without_comments_writes_fallback():
    """Git 侧无评论可作理由 -> 写兜底文案,状态照常落 已拒绝。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "closed"  # close_reasons 缺省 None(无评论)
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows()

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REJECTED.value
    assert row[fields.CLOSE_REASON] == CLOSE_REASON_FALLBACK


def test_review_sync_close_reason_fetch_failure_still_rejects_empty():
    """理由读取失败(瞬时)不阻塞状态推进:落 已拒绝,理由留空人工补充。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "closed"
    repo.close_reason_errors["sid-1"] = RetryableError("git down")
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows()

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REJECTED.value
    assert row[fields.CLOSE_REASON] == ""


def test_review_sync_merged_writes_no_close_reason():
    """PR 合并 -> 已通过,不写关闭理由(即使 Git 侧有历史评论)。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "merged"
    repo.close_reasons["sid-1"] = "不应出现"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows()

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.APPROVED.value
    assert row[fields.CLOSE_REASON] == ""


def test_rejected_row_new_round_clears_close_reason():
    """V2-P3:终态行开启新一轮(claim)时清空上一轮的关闭理由。"""
    row = snapshot(
        status=SubmissionStatus.REJECTED,
        submission_id="sub-old",
    )
    row[fields.CLOSE_REASON] = "旧轮次的拒绝理由"
    bitable = FakeBitable("rec-1", row)
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.CLOSE_REASON] == ""


def test_review_sync_open_pr_keeps_reviewing():
    bitable = FakeBitable("rec-1", reviewing_row())
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "open"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows()

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert bitable.marked == []


def test_review_sync_missing_pr_keeps_reviewing():
    """Git 上找不到 PR(如被手动删除)-> 保持 审核中,不误标终态。"""
    bitable = FakeBitable("rec-1", reviewing_row())
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.sync_reviewing_rows()  # pr_states 为空 -> None
    service.sync_reviewing_rows()  # 第二次:告警只发一次,无异常

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert bitable.marked == []


def test_review_sync_skips_row_without_submission_id():
    row = reviewing_row()
    row[fields.SUBMISSION_ID] = None  # type: ignore[assignment]
    bitable = FakeBitable("rec-1", row)
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "merged"
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows()

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert repo.pr_state_errors == {}


def test_review_sync_isolates_per_record_errors():
    """单行 Git 查询失败不阻塞其余行;失败行保持 审核中 下轮自愈。"""
    bitable = FakeBitable("rec-1", reviewing_row("rec-1", "sid-1"))
    bitable.records["rec-2"] = reviewing_row("rec-2", "sid-2")
    repo = FakeGitRepository()
    repo.pr_states["sid-1"] = "merged"
    repo.pr_states["sid-2"] = "merged"
    repo.pr_state_errors["sid-2"] = RetryableError("git 查询失败")
    service = SubmissionService(bitable=bitable, repository=repo)

    service.sync_reviewing_rows()

    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.APPROVED.value
    assert bitable.records["rec-2"][fields.STATUS] == SubmissionStatus.REVIEWING.value


def test_exception_does_not_leak_from_service():
    """service 内 Git/Bitable 异常不会外泄到 daemon(worker 兜底打印)。"""
    bitable = FakeBitable("rec-1", snapshot())
    repo = FakeGitRepository()
    repo.next_error = PermanentError("boom")
    repo.next_error_times = 1
    service = SubmissionService(bitable=bitable, repository=repo)

    # 永久失败路径内部已吞掉异常并回写
    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.FAILED.value

    # 瞬态路径的 mark_retryable 异常(bitable 也挂了)会冒泡:
    # worker 的 per-record 兜底负责不让 daemon 终止
    class DownBitable:
        def mark_retryable(self, *a, **k):
            raise BitableError("bitable down")

        def mark_failed(self, *a, **k):
            raise BitableError("bitable down")

    repo2 = FakeGitRepository()
    repo2.next_error = RetryableError("git down")
    repo2.next_error_times = 1
    down = DownBitable()  # type: ignore[assignment]
    service2 = SubmissionService(bitable=down, repository=repo2)  # type: ignore[arg-type]

    try:
        service2.process_record("rec-1", snapshot())
        raise AssertionError("应抛出异常交由 worker 兜底")
    except BitableError:
        pass


# ----------------------------------------------------------------------
# JSON 附件导入(上传 = 新建材料,V2-P4 语义):
# 草稿行 -> 下载解析(须含 14 个必填键,其余键忽略)-> 只填空 -> 反写 +
# 置「已请求」-> 下一轮轮询自动 claim -> PR。失败只写错误信息,不提交。
# ----------------------------------------------------------------------

def valid_file(**overrides):
    """V2-P4:全必填键的合法附件 JSON(默认值同 helpers.DEFAULTS)。"""
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


def attach(name="profile.json", token="tok-1"):
    return [{"file_token": token, "name": name}]


def test_json_backfill_happy_path_fills_blanks_and_auto_submits():
    """合法附件 JSON -> 只填空 + 置「已请求」;下一轮轮询自动开 PR。"""
    bitable = FakeBitable(
        "rec-1",
        json_draft_row(
            attachment=attach(),
            **{fields.ERROR_MSG: "旧的解析错误"},
        ),
    )
    bitable.attachments["tok-1"] = valid_file(id="mat-1").encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.backfill_pending_json_rows()  # 本轮轮询:只反写,不开 PR

    row = bitable.records["rec-1"]
    assert row[fields.NAME] == "Test PLA"
    assert row[fields.BRAND] == "BBL"  # V2-P4:结构列一并由附件填入
    assert row[fields.MODEL] == "H2C"
    assert row[fields.SLICER] == "BambuStudio"
    assert row[fields.MATERIAL_ID] == "mat-1"  # 附件显式带 id 且 != 品名才落列
    assert row[fields.NOZZLE_TEMP] == 220
    assert row[fields.MAX_VOL_SPEED] == DEFAULTS["filament_max_volumetric_speed"]
    assert row[fields.ERROR_MSG] == ""
    assert row[fields.STATUS] in ("", SubmissionStatus.DRAFT.value)  # 尚未 claim
    assert row[fields.REQUESTED] is True  # V2-P4:自动进入提交流程
    assert repo.submit_calls == []  # 本轮内还没走到 Git(下一轮才提交)

    # 下一轮轮询:已请求 行走与按钮完全相同的常规流程 -> claim -> PR
    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False
    assert row[fields.ERROR_MSG] == ""
    assert len(repo.pull_requests) == 1
    assert row[fields.PR_URL].endswith(row[fields.SUBMISSION_ID])
    assert service._json_parse_attempted == {("rec-1", "tok-1")}


def test_json_backfill_fills_optional_columns_and_keeps_id_blank_by_default():
    """V2-P4:可选 B 列(继承预设/切片器版本/调参方法版本/PI Code)在附件给出
    且单元格空白时也反写;附件不带 id -> 材料ID 保持空(缺省=品名不落列)。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = valid_file(
        inherits="PolyTerra PLA @BBL H2C",
        version="01.08.02.50",
        pm_method_version="pm-v2.1",
        pi_code="PT-PLA-001",
    ).encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()

    row = bitable.records["rec-1"]
    assert row[fields.INHERITS] == "PolyTerra PLA @BBL H2C"
    assert row[fields.SLICER_VERSION] == "01.08.02.50"
    assert row[fields.PM_METHOD_VERSION] == "pm-v2.1"
    assert row[fields.PI_CODE] == "PT-PLA-001"
    assert row.get(fields.MATERIAL_ID) in (None, "")  # 无 id 键:材料ID 不落


def test_json_backfill_only_fills_blank_cells_keeps_manual_values():
    """用户已填的单元格绝不被附件覆盖(附件只填空位)。"""
    bitable = FakeBitable(
        "rec-1",
        json_draft_row(
            attachment=attach(),
            **{fields.NAME: "已手填的品名"},  # 品名已填,其余留空
        ),
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()

    row = bitable.records["rec-1"]
    assert row[fields.NAME] == "已手填的品名"
    assert row[fields.NOZZLE_TEMP] == 220
    assert row[fields.MAX_VOL_SPEED] == DEFAULTS["filament_max_volumetric_speed"]
    assert row[fields.REQUESTED] is True  # V2-P4:填齐后同样自动进入提交流程


def test_json_backfill_skips_non_candidates():
    """无附件/字段已齐/已在提交生命周期(审核中/失败…)的行一律不动。"""
    bitable = FakeBitable("rec-1", json_draft_row())  # 无附件
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")

    # 字段已齐 + 挂着附件:视为人工填写完成,不解析
    bitable.records["rec-2"] = dict(
        row_fields(喷嘴温度=200, 最大体积流速=10),
        **json_draft_row(attachment=attach(token="tok-1")),
    )
    # 审核中 行(在途提交)挂附件 + 空字段:不碰
    reviewing = json_draft_row(
        attachment=attach(token="tok-1"), status=SubmissionStatus.REVIEWING.value
    )
    reviewing[fields.SUBMISSION_ID] = "sid-1"
    bitable.records["rec-3"] = reviewing
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()

    for rid in ("rec-1", "rec-2", "rec-3"):
        row = bitable.records[rid]
        assert row.get(fields.NOZZLE_TEMP) in (None, 200, 10)
        assert row.get(fields.REQUESTED) is False  # 无附件/已齐/在途:不自动提交
    assert bitable.download_tokens == []  # 一次下载都没发生


def test_json_backfill_accepts_realistic_preset_style_json():
    """V2-P4:真实 BambuStudio 系 JSON(14 必填键 + 一堆附加键/数组值)
    -> 附加键忽略,反写 + 自动提交;不再因未建模键报错(用户 E2E 反馈)。"""
    payload = valid_file(
        cool_plate_temp=["35"],
        cool_plate_temp_initial_layer=["35"],
        eng_plate_temp=["55"],
        filament_type="PLA",
        filament_id="PMPL27",
        filament_settings_id=["Test PLA @BBL H2C"],
        **{"from": "User", "type": "filament", "instantiation": "true"},
    )
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = payload.encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.backfill_pending_json_rows()

    row = bitable.records["rec-1"]
    assert row[fields.ERROR_MSG] == ""
    assert row[fields.NAME] == "Test PLA"
    assert row[fields.NOZZLE_TEMP] == 220
    assert row[fields.REQUESTED] is True  # 自动进入提交流程
    assert row.get(fields.MATERIAL_ID) in (None, "")  # filament_id 不落材料ID

    # 下一轮轮询自动开 PR(证明上传即可达提交,无需按钮)
    service.process_record("rec-1", dict(bitable.records["rec-1"]))
    assert bitable.records["rec-1"][fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert len(repo.pull_requests) == 1


def test_json_backfill_invalid_value_writes_error_and_no_partial_data():
    """数值数组值不一致 -> 写错误信息,不写任何部分数据,不热循环。"""
    bad = valid_file(nozzle_temperature=["220", "230"])  # 多喷头取值不一致
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = bad.encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()
    service.backfill_pending_json_rows()  # 第二次:同 token 不再下载

    row = bitable.records["rec-1"]
    assert row[fields.ERROR_MSG].startswith(JSON_PARSE_ERROR_PREFIX)
    assert "各值不一致" in row[fields.ERROR_MSG]
    assert row.get(fields.NAME) is None  # 无部分数据
    assert row.get(fields.NOZZLE_TEMP) is None
    assert row[fields.REQUESTED] is False  # 失败不自动提交
    assert bitable.download_tokens == ["tok-1"]


def test_json_backfill_missing_required_key_reports_all_missing():
    """V2-P4:附件缺必填键 -> 一次性列出缺失(与表格同一 schema),不反写。"""
    payload = json.loads(valid_file())
    del payload["slicer"]
    del payload["filament_flow_ratio"]
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = json.dumps(payload).encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()

    row = bitable.records["rec-1"]
    assert row[fields.ERROR_MSG].startswith(JSON_PARSE_ERROR_PREFIX)
    assert "缺少必填字段" in row[fields.ERROR_MSG]
    assert "slicer(切片器)" in row[fields.ERROR_MSG]
    assert "filament_flow_ratio(流量比例)" in row[fields.ERROR_MSG]
    assert row.get(fields.NAME) is None  # 无部分数据


def test_json_backfill_attachment_goes_through_shared_validation():
    """V2-P4:附件路径与手工填表共用同一 validate —— BBL+压力提前 一律拦。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = valid_file(
        brand="BBL", pressure_advance=0.05
    ).encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()

    row = bitable.records["rec-1"]
    assert "官方机型" in row[fields.ERROR_MSG]
    assert row.get(fields.PRESSURE_ADVANCE) is None  # 无部分数据


def test_json_backfill_replaced_attachment_retries():
    """同一 token 失败后不再重试;用户替换附件(新 token)后自然重试。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach(token="bad-1")))
    bitable.attachments["bad-1"] = b"{broken json"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()
    assert bitable.records["rec-1"][fields.ERROR_MSG].startswith(
        JSON_PARSE_ERROR_PREFIX
    )

    bitable.records["rec-1"][fields.PROFILE_JSON] = attach(token="good-1")
    bitable.attachments["good-1"] = valid_file().encode("utf-8")
    service.backfill_pending_json_rows()

    row = bitable.records["rec-1"]
    assert row[fields.NAME] == "Test PLA"
    assert row[fields.ERROR_MSG] == ""
    assert row[fields.REQUESTED] is True  # 替换后成功 -> 自动提交
    assert bitable.download_tokens == ["bad-1", "good-1"]


def test_json_backfill_multiple_or_non_json_attachments_fail():
    """严格单 JSON(R2):多个附件 / 混入非 .json 一律失败,不静默选择。"""
    # 两个 JSON
    bitable = FakeBitable(
        "rec-1",
        json_draft_row(attachment=attach() + attach(token="tok-2")),
    )
    # 混入一个 .png
    bitable.records["rec-2"] = json_draft_row(attachment=attach(name="photo.png"))
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    bitable.attachments["tok-2"] = valid_file().encode("utf-8")
    bitable.attachments["photo.png"] = b"png"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()

    assert "恰好挂 1 个" in bitable.records["rec-1"][fields.ERROR_MSG]
    assert ".json" in bitable.records["rec-2"][fields.ERROR_MSG]
    assert bitable.records["rec-1"].get(fields.NAME) is None
    assert bitable.download_tokens == []  # 规则不符,连下载都不发生


def test_json_backfill_oversize_attachment_fails():
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = b"x" * (JSON_ATTACHMENT_MAX_BYTES + 1)
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()

    row = bitable.records["rec-1"]
    assert "大小上限" in row[fields.ERROR_MSG]
    assert row.get(fields.NAME) is None


def test_json_backfill_permanent_download_failure_no_retry():
    """非瞬时下载失败(如媒体已失效)-> 记 attempted,不每轮重试。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = b"x"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    bitable.download_error = BitableError("media gone")
    service.backfill_pending_json_rows()
    service.backfill_pending_json_rows()  # 第二次不再下载

    assert "media gone" in bitable.records["rec-1"][fields.ERROR_MSG]
    assert bitable.download_tokens == ["tok-1"]


def test_json_backfill_retryable_download_failure_retries_next_poll():
    """瞬时下载失败(网络/限流)-> 写错误信息但不记 attempted,下轮重试。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    bitable.download_error = RetryableError("网络抖动")
    service.backfill_pending_json_rows()
    assert "自动重试" in bitable.records["rec-1"][fields.ERROR_MSG]

    bitable.download_error = None  # 故障恢复
    service.backfill_pending_json_rows()

    assert bitable.records["rec-1"][fields.NAME] == "Test PLA"
    assert bitable.records["rec-1"][fields.REQUESTED] is True  # 恢复后自动提交
    assert bitable.download_tokens == ["tok-1", "tok-1"]


def test_json_backfill_value_range_violation_writes_error():
    """附件数值越界 -> 共享 validate() 拦住,写错误信息,不反写。"""
    bitable = FakeBitable("rec-1", json_draft_row(attachment=attach()))
    bitable.attachments["tok-1"] = valid_file(
        nozzle_temperature=9000
    ).encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.backfill_pending_json_rows()

    row = bitable.records["rec-1"]
    assert "喷嘴温度" in row[fields.ERROR_MSG]
    assert row.get(fields.NOZZLE_TEMP) is None
    assert row[fields.REQUESTED] is False  # 校验失败不自动提交


# ----------------------------------------------------------------------
# V2-P4:claim 路径的附件导入 —— 点「请求」时必填仍空 + 挂 JSON 附件,
# process_record 先按附件反写再提交(上传=新建,用户 E2E 反馈:点按钮先行
# 时不再落入「缺少必填字段」永久失败)。附件问题 -> 永久失败带前缀。
# ----------------------------------------------------------------------

def test_requested_row_with_attachment_fills_and_submits_in_one_call():
    """空行点「请求」+ 挂合法 JSON:同一次 process_record 内反写 + 开 PR。"""
    bitable = FakeBitable(
        "rec-1", json_draft_row(attachment=attach(), requested=True)
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.NAME] == "Test PLA"  # 反写已落表
    assert row[fields.BRAND] == "BBL"
    assert row[fields.NOZZLE_TEMP] == 220
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.REQUESTED] is False
    assert row[fields.ERROR_MSG] == ""
    assert len(repo.pull_requests) == 1  # 一次点击直达 PR
    assert bitable.download_tokens == ["tok-1"]


def test_requested_row_partial_manual_values_not_overwritten_at_claim():
    """claim 反写同样只填空:用户已填的单元格不被附件覆盖。"""
    bitable = FakeBitable(
        "rec-1",
        json_draft_row(
            attachment=attach(),
            requested=True,
            **{fields.NAME: "手工品名"},
        ),
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.NAME] == "手工品名"  # 附件里的 name 未覆盖
    assert row[fields.NOZZLE_TEMP] == 220  # 其余空白由附件填充
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert len(repo.pull_requests) == 1


def test_requested_row_invalid_attachment_fails_with_parse_error():
    """附件本身非法(数组值不一致)-> 永久失败带「附件解析失败:」,无部分数据。"""
    bitable = FakeBitable(
        "rec-1", json_draft_row(attachment=attach(), requested=True)
    )
    bitable.attachments["tok-1"] = valid_file(
        nozzle_temperature=["220", "230"]
    ).encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert row[fields.REQUESTED] is False
    assert row[fields.ERROR_MSG].startswith(JSON_PARSE_ERROR_PREFIX)
    assert "各值不一致" in row[fields.ERROR_MSG]
    assert row.get(fields.NAME) is None  # 反写未落任何部分数据
    assert repo.submit_calls == []


def test_requested_row_multi_attachment_fails_at_claim():
    """claim 路径同样执行严格单 JSON(R2),不静默选一个。"""
    bitable = FakeBitable(
        "rec-1",
        json_draft_row(
            attachment=attach() + attach(token="tok-2"), requested=True
        ),
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    bitable.attachments["tok-2"] = valid_file().encode("utf-8")
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert "恰好挂 1 个" in row[fields.ERROR_MSG]
    assert bitable.download_tokens == []  # 规则不符,连下载都不发生


def test_requested_row_blank_without_attachment_fails_with_guidance():
    """必填空 + 无附件可导(真·空行点按钮)-> 保持原语义:校验失败给指引。"""
    bitable = FakeBitable("rec-1", json_draft_row(requested=True))
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.FAILED.value
    assert "数据校验失败" in row[fields.ERROR_MSG]
    assert "缺少必填字段" in row[fields.ERROR_MSG]
    assert repo.submit_calls == []


def test_requested_row_transient_download_failure_retries_then_succeeds():
    """claim 时附件下载瞬时失败 -> 有限自动重试(不终态),恢复后一次成功。"""
    bitable = FakeBitable(
        "rec-1", json_draft_row(attachment=attach(), requested=True)
    )
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    repo = FakeGitRepository()
    service = SubmissionService(bitable=bitable, repository=repo)

    bitable.download_error = RetryableError("网络抖动")
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.PROCESSING.value  # 非终态
    assert row[fields.REQUESTED] is True  # 下轮自动重试
    assert row[fields.RETRY_COUNT] == 1
    assert "自动重试" in row[fields.ERROR_MSG]
    assert repo.submit_calls == []

    bitable.download_error = None  # 故障恢复
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.NAME] == "Test PLA"
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert row[fields.ERROR_MSG] == ""
    assert len(repo.pull_requests) == 1
    assert bitable.download_tokens == ["tok-1", "tok-1"]


def test_requested_row_complete_fields_never_touches_attachment():
    """必填字段已齐的行点「请求」:不解析附件(内容非法也不读),直接提交。"""
    row = snapshot()
    row[fields.PROFILE_JSON] = attach()
    bitable = FakeBitable("rec-1", row)
    bitable.attachments["tok-1"] = b"{broken json"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert bitable.download_tokens == []  # 附件从未被读取


def test_claim_path_ignores_backfill_attempted_and_retries_attachment():
    """反写轮已对同一 (record, token) 记过 attempted(坏文件)后,用户修正
    附件内容并点「请求」:claim 路径仍重新解析 —— attempted 只约束无人
    值守的反写轮,不阻塞显式提交(修复 stuck 行)。"""
    bitable = FakeBitable(
        "rec-1", json_draft_row(attachment=attach(), requested=False)
    )
    bitable.attachments["tok-1"] = b"{broken json"
    service = SubmissionService(bitable=bitable, repository=FakeGitRepository())

    # 第一轮:反写轮遇到坏附件 -> 记 attempted + 写错误信息(不置请求)
    service.backfill_pending_json_rows()
    assert bitable.records["rec-1"][fields.ERROR_MSG].startswith(
        JSON_PARSE_ERROR_PREFIX
    )
    assert service._json_parse_attempted == {("rec-1", "tok-1")}

    # 用户覆盖同一附件内容为合法 JSON(同 token)并点「请求」
    bitable.attachments["tok-1"] = valid_file().encode("utf-8")
    bitable.records["rec-1"][fields.REQUESTED] = True
    service.process_record("rec-1", dict(bitable.records["rec-1"]))

    row = bitable.records["rec-1"]
    assert row[fields.NAME] == "Test PLA"
    assert row[fields.ERROR_MSG] == ""
    assert row[fields.STATUS] == SubmissionStatus.REVIEWING.value
    assert service._json_parse_attempted == {("rec-1", "tok-1")}  # 不再追加
    assert bitable.download_tokens == ["tok-1", "tok-1"]
