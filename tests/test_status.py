import pytest

from material_worker.domain.status import (
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
    for status in (
        SubmissionStatus.REVIEWING,
        SubmissionStatus.APPROVED,
        SubmissionStatus.REJECTED,
    ):
        assert status.is_terminal(), status

    for status in (
        SubmissionStatus.DRAFT,
        SubmissionStatus.PENDING,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.FAILED,
    ):
        assert not status.is_terminal(), status


def test_retry_reuse_semantics():
    for status in (
        SubmissionStatus.PENDING,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.FAILED,
    ):
        assert status.retry_reuses_same_submission(), status
    assert not SubmissionStatus.REVIEWING.retry_reuses_same_submission()
    assert not SubmissionStatus.APPROVED.retry_reuses_same_submission()


def test_from_table_values():
    assert SubmissionStatus.from_table("审核中") is SubmissionStatus.REVIEWING
    assert SubmissionStatus.from_table(None) is SubmissionStatus.PENDING
    assert SubmissionStatus.from_table("") is SubmissionStatus.PENDING
    assert SubmissionStatus.from_table("未知文本") is SubmissionStatus.PENDING
