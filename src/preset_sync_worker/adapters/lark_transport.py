"""lark SDK 调用的统一错误映射(Bitable 与云文档共用)。

这里只负责「一次调用 + 把失败翻译成本项目的异常类型」,不含任何业务语义:

- 传输层异常(网络/超时)-> RetryableError;
- 限流码 99991400 -> RetryableError(账号级,不是某个接口专有);
- 其余错误码 -> ``error_cls``(Bitable 传 BitableError,云文档传 DriveError)。

**这里刻意不猜「永久 vs 瞬时」**:凭错误码猜不可靠,而猜错的代价是永久失败
或者无限重试。明确永久的问题(文件超限、文件名为空、配置缺失)一律由调用方在
**发请求之前**抛 PermanentError;未知错误码交给 service 的有界重试,API 的 msg
会原样写进「错误信息」列,人工一眼能看到真正原因。
"""
from __future__ import annotations

from typing import Any, Callable

from preset_sync_worker.exceptions import PresetSyncWorkerError, RetryableError

#: 飞书全局限流/系统繁忙码
RATE_LIMIT_CODE = 99991400


def call_lark(
    fn: Callable[[], Any],
    action: str,
    *,
    surface: str,
    error_cls: type[PresetSyncWorkerError],
) -> Any:
    """执行一次 lark SDK 调用并归一化失败。

    ``action`` 与 ``surface`` 只用于拼错误文案(如 "Bitable list records failed: …")。
    """
    try:
        resp = fn()
    except RetryableError:
        raise
    except Exception as exc:  # lark SDK 在传输层抛出的异常
        raise _make_callable_error(exc, surface) from exc

    if not resp.success():
        code, msg = resp.code, resp.msg
        if code == RATE_LIMIT_CODE:  # 访问频繁/系统繁忙
            raise RetryableError(
                f"{surface} {action} 被限流: code={code}, msg={msg}"
            )
        raise error_cls(f"{surface} {action} failed: code={code}, msg={msg}")
    return resp


def _make_callable_error(
    exc: Exception, surface: str
) -> RetryableError:
    return RetryableError(
        f"{surface} 请求失败(网络/超时): {type(exc).__name__}: {exc}"
    )
