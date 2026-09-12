class MaterialWorkerError(Exception):
    pass


class BitableError(MaterialWorkerError):
    pass


class GitRepositoryError(MaterialWorkerError):
    pass


class DriveError(MaterialWorkerError):
    """飞书云文档接口错误码失败(与 BitableError 平级,各自独立)。"""


class RetryableError(MaterialWorkerError):
    """Temporary error that should normally be retried."""


class PermanentError(MaterialWorkerError):
    """Invalid input or other error that should not be automatically retried."""
