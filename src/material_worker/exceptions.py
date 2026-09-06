class MaterialWorkerError(Exception):
    pass


class BitableError(MaterialWorkerError):
    pass


class GitRepositoryError(MaterialWorkerError):
    pass


class RetryableError(MaterialWorkerError):
    """Temporary error that should normally be retried."""


class PermanentError(MaterialWorkerError):
    """Invalid input or other error that should not be automatically retried."""
