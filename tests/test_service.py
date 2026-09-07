"""SubmissionService 测试:用 fake 的 BitableClient / GitRepository(P7 #35)。"""
from material_worker import fields
from material_worker.adapters.bitable import BitableClient
from material_worker.adapters.git import GitRepository, PullRequestResult
from material_worker.domain.profile import MaterialProfile
from material_worker.domain.status import SubmissionStatus
from material_worker.exceptions import BitableError, PermanentError, RetryableError
from material_worker.services.submission_service import SubmissionService


def make_profile(record_id="rec-1"):
    return MaterialProfile(
        id="test-pla",
        name="Test PLA",
        nozzle_temperature=220,
        max_volumetric_speed=20,
        source_record_id=record_id,
    )


class FakeBitable:
    """内存版 BitableClient:记录变更可见,可注入单次回写失败。"""

    def __init__(self, record_id, initial_fields):
        self.records = {record_id: dict(initial_fields)}
        self.fail_next_review = False
        self.mark_reviewing_calls = 0
        self.marked: list[tuple[str, str]] = []

    def record(self, record_id):
        return self.records[record_id]

    def update_record(self, record_id, values):
        self.records[record_id].update(values)

    def clear_request(self, record_id):
        """V2-P0:仅清除 已请求(在途行重复触发打住)。"""
        self.records[record_id][fields.REQUESTED] = False

    def get_record(self, record_id):
        return record_id, dict(self.records[record_id])

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
            }
        )

    def mark_rejected(self, record_id):
        self.marked.append((record_id, SubmissionStatus.REJECTED.value))
        self.records[record_id].update(
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.REJECTED.value,
                fields.ERROR_MSG: "",
                fields.RETRY_COUNT: 0,
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
    data = {
        fields.NAME: "Test PLA",
        fields.NOZZLE_TEMP: 220,
        fields.MAX_VOL_SPEED: 20,
        fields.REQUESTED: requested,
        fields.STATUS: (status or SubmissionStatus.PENDING).value,
    }
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
