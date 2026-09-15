"""MaterialSubmission 领域对象 + V3-P5 的 branch 派生规则。

V3:表里不再有「提交 ID」列(resolve_submission_id 已删除)——
一行 = 一个身份,每次提交都提交这一行,提交 ID 会被覆盖而失去意义,
幂等改由「branch 由身份派生」承担。本轮提交也不再生成任何 id 或
时间戳(用户确认:提交的文件里 submission/generated_at 没用)。
"""
import pytest

from helpers import make_profile as make_valid_profile

from preset_sync_worker.adapters.git import GitRepository
from preset_sync_worker.domain.status import InvalidTransitionError, SubmissionStatus
from preset_sync_worker.domain.submission import MaterialSubmission


def make_submission():
    return MaterialSubmission(record_id="rec-1", profile=make_valid_profile())


def test_submission_state():
    submission = make_submission()

    assert submission.status == SubmissionStatus.PENDING

    submission.mark_processing()
    assert submission.status == SubmissionStatus.PROCESSING

    submission.mark_reviewing("https://example.invalid/pr/1")
    assert submission.status == SubmissionStatus.REVIEWING
    assert submission.pull_request_url.endswith("/1")


def test_mark_failed_clears_error_and_keeps_url():
    submission = make_submission()
    submission.mark_processing()
    submission.mark_reviewing("https://example.invalid/pr/1")

    submission.mark_failed("boom")
    assert submission.status == SubmissionStatus.FAILED
    assert submission.error_message == "boom"


def test_illegal_transition_raises():
    submission = make_submission()
    submission.mark_processing()
    submission.mark_reviewing("https://example.invalid/pr/1")

    with pytest.raises(InvalidTransitionError):
        submission.mark_processing()  # 审核中 -> 处理中 非法


def test_submission_carries_no_traceability_fields():
    """没有 submission_id / submitted_at —— 溯源交给 Git 历史。"""
    submission = make_submission()
    assert not hasattr(submission, "submission_id")
    assert not hasattr(submission, "submitted_at")


# ----------------------------------------------------------------------
# V3-P5:branch 由行身份派生
# ----------------------------------------------------------------------
def test_branch_name_derived_from_identity_and_slicer():
    assert GitRepository.branch_name_for("L1002@BBL P2S", "BambuStudio") == (
        "material/L1002@BBL_P2S_BambuStudio"
    )


def test_branch_name_is_stable_for_same_identity():
    """同一行永远同一 branch —— 重复点击/重试天然幂等的基础。"""
    first = GitRepository.branch_name_for("L1002@BBL P2S", "BambuStudio")
    second = GitRepository.branch_name_for("L1002@BBL P2S", "BambuStudio")
    assert first == second


def test_branch_differs_across_slicers():
    bambu = GitRepository.branch_name_for("L1002@BBL P2S", "BambuStudio")
    prusa = GitRepository.branch_name_for("L1002@BBL P2S", "PrusaSlicer")
    assert bambu != prusa


def test_branch_has_no_spaces():
    """git branch 名不能含空格 -> 空白折叠为下划线。"""
    branch = GitRepository.branch_name_for(
        "L1002@Creality K2 Pro", "CrealityPrint"
    )
    assert " " not in branch
    assert branch == "material/L1002@Creality_K2_Pro_CrealityPrint"
