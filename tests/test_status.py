import pytest

from preset_sync_worker.domain.status import (
    InvalidTransitionError,
    SubmissionStatus,
)


def test_valid_worker_transitions():
    # claim / 成功 / 永久失败 / 失败后修正重试
    assert SubmissionStatus.PENDING.can_transition(SubmissionStatus.PROCESSING)
    assert SubmissionStatus.PENDING.can_transition(SubmissionStatus.FAILED)
    assert SubmissionStatus.PROCESSING.can_transition(SubmissionStatus.REVIEWING)
    assert SubmissionStatus.PROCESSING.can_transition(SubmissionStatus.FAILED)
    assert SubmissionStatus.FAILED.can_transition(SubmissionStatus.PROCESSING)


def test_invalid_transitions():
    assert not SubmissionStatus.REVIEWING.can_transition(SubmissionStatus.PENDING)
    assert not SubmissionStatus.PROCESSING.can_transition(SubmissionStatus.PROCESSING)
    assert not SubmissionStatus.APPROVED.can_transition(SubmissionStatus.FAILED)
    assert not SubmissionStatus.PENDING.can_transition(SubmissionStatus.REVIEWING)


def test_assert_transition_raises():
    with pytest.raises(InvalidTransitionError):
        SubmissionStatus.APPROVED.assert_can_transition(
            SubmissionStatus.PROCESSING
        )


def test_terminal_states():
    """V2-P0:终态只含 已通过/已拒绝;审核中 是在途状态,由审查同步推进。"""
    for status in (
        SubmissionStatus.APPROVED,
        SubmissionStatus.REJECTED,
    ):
        assert status.is_terminal(), status

    for status in (
        SubmissionStatus.DRAFT,
        SubmissionStatus.PENDING,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.REVIEWING,  # V2-P0:审核中 不再视为已收尾
        SubmissionStatus.FAILED,
    ):
        assert not status.is_terminal(), status


def test_retry_reuse_semantics():
    """V2-P0:重复点击复用同一提交的状态组 = 待处理/处理中/失败/审核中。"""
    for status in (
        SubmissionStatus.PENDING,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.FAILED,
        SubmissionStatus.REVIEWING,  # V2-P0:在途提交的重复触发复用
    ):
        assert status.retry_reuses_same_submission(), status
    assert not SubmissionStatus.APPROVED.retry_reuses_same_submission()
    assert not SubmissionStatus.REJECTED.retry_reuses_same_submission()


def test_from_table_values():
    assert SubmissionStatus.from_table("审核中") is SubmissionStatus.REVIEWING
    assert SubmissionStatus.from_table(None) is SubmissionStatus.PENDING
    assert SubmissionStatus.from_table("") is SubmissionStatus.PENDING
    assert SubmissionStatus.from_table("未知文本") is SubmissionStatus.PENDING
