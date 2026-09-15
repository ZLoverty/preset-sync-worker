from __future__ import annotations

import logging
import sys


def configure_logging() -> None:
    """Send timestamped, immediately flushed logs to stdout/journald."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def log(message: str) -> None:
    """Compatibility shim for existing human-readable events, with severity."""
    text = str(message)
    if text.startswith(("[严重错误]", "[Worker Error]")):
        level = logging.ERROR
    elif text.startswith("[附件解析失败]") and "(瞬时" in text:
        level = logging.WARNING
    elif text.startswith("[审查同步]") and any(
        marker in text for marker in ("失败", "找不到", "无 PR URL")
    ):
        level = logging.WARNING
    elif text.startswith(
        ("[数据/永久失败]", "[附件解析失败]", "[审查同步失败]", "[记录异常")
    ):
        level = logging.ERROR
    elif text.startswith(
        ("[重试]", "[跳过]", "[重复点击:无法确认 PR]", "[配置]")
    ):
        level = logging.WARNING
    else:
        level = logging.INFO
    include_traceback = text.startswith(
        ("[严重错误]", "[Worker Error]", "[记录异常", "[审查同步失败]")
    ) and sys.exc_info()[0] is not None
    logging.getLogger("preset-sync-worker").log(
        level, text, exc_info=include_traceback or None
    )
