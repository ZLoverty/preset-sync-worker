from datetime import datetime

import pytest

from helpers import make_profile as make_valid_profile

from material_worker import fields
from material_worker.domain.status import InvalidTransitionError, SubmissionStatus
from material_worker.domain.submission import (
    MaterialSubmission,
    resolve_submission_id,
)


def make_submission():
    profile = make_valid_profile()  # V2-P4:全必填字段构造

    return MaterialSubmission(
        submission_id="submission-1",
        record_id="rec-1",
        profile=profile,
        submitted_at=datetime.now(),
    )


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


def _snapshot(status, submission_id=None, **extra):
    data = {
        fields.NAME: "Test PLA",
        fields.NOZZLE_TEMP: 220,
        fields.MAX_VOL_SPEED: 20,
        fields.STATUS: status.value if status else None,
    }
    if submission_id is not None:
        data[fields.SUBMISSION_ID] = submission_id
    data.update(extra)
    return data


def test_resolve_generates_id_on_blank_record():
    sid = resolve_submission_id(_snapshot(SubmissionStatus.PENDING))
    assert sid


def test_resolve_reuses_id_for_pending_processing_failed():
    for status in (
        SubmissionStatus.PENDING,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.FAILED,
    ):
        sid = resolve_submission_id(_snapshot(status, submission_id="sub-keep"))
        assert sid == "sub-keep"


def test_resolve_reuses_id_for_reviewing_with_existing_id():
    """V2-P0:审核中(在途提交)带提交 ID 时归入复用组(service 层会短路,
    resolve 层保持同一语义:不因重复触发换 ID)。"""
    sid = resolve_submission_id(
        _snapshot(SubmissionStatus.REVIEWING, submission_id="sub-inflight")
    )
    assert sid == "sub-inflight"


def test_resolve_generates_new_id_after_terminal_state():
    """V2-P0:终态(已通过/已拒绝)再次点击 = 新一轮,生成新 ID。"""
    for status in (
        SubmissionStatus.APPROVED,
        SubmissionStatus.REJECTED,
    ):
        sid = resolve_submission_id(_snapshot(status, submission_id="sub-old"))
        assert sid != "sub-old"


def test_resolve_generates_id_when_terminal_without_id():
    sid = resolve_submission_id(_snapshot(SubmissionStatus.APPROVED))
    assert sid
