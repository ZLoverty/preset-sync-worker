class PresetSyncWorkerError(Exception):
    pass


class BitableError(PresetSyncWorkerError):
    pass


class GitRepositoryError(PresetSyncWorkerError):
    pass


class DriveError(PresetSyncWorkerError):
    """飞书云文档接口错误码失败(与 BitableError 平级,各自独立)。"""


class RetryableError(PresetSyncWorkerError):
    """Temporary error that should normally be retried."""


class PermanentError(PresetSyncWorkerError):
    """Invalid input or other error that should not be automatically retried."""
