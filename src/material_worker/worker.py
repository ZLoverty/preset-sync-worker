from __future__ import annotations

from material_worker.logging_setup import log

import time

from material_worker.adapters.bitable import BitableClient
from material_worker.services.submission_service import SubmissionService


class MaterialWorker:
    """轮询 daemon:找 已请求=true 的行,逐条交给 SubmissionService。"""

    def __init__(
        self,
        bitable: BitableClient,
        submission_service: SubmissionService,
    ):
        self.bitable = bitable
        self.submission_service = submission_service

    def boot(self) -> None:
        """启动前自检:补齐 Bitable 缺失列,配置错误在此快速失败。"""
        self.bitable.ensure_schema()
        log("[Schema] Bitable 列检查完成")

    def poll_once(self) -> None:
        """一轮轮询。单条记录出错不中断本轮其余记录(P6 #30)。

        V3:全表只拉一次,三个阶段(提交处理 / 附件反写 / 审查同步)
        共用同一份快照 —— 重复身份检测需要全表视角,顺带省掉两次全表拉取。
        """
        records = self.bitable.list_records()

        # 提交处理:已请求=true 的行;单行异常在 service 内隔离
        self.submission_service.process_pending_rows(records)

        # 附件导入:未进入提交生命周期的行挂附件时宽容解析反写(**不自动提交**,
        # 由用户点按钮决定;点按钮的行由 process_record 做同样的反写兜底)。
        self.submission_service.backfill_pending_json_rows(records)

        # 审查同步:推进「审核中」行(PR 合并 -> 已通过,关闭未合并 -> 已拒绝)。
        # service 内已按行隔离;整体异常由 run_forever 兜底,不终止 daemon。
        self.submission_service.sync_reviewing_rows(records)

    def run_forever(self, poll_interval: int) -> None:
        last_error: str | None = None
        consecutive_errors = 0
        while True:
            try:
                self.poll_once()
                if last_error is not None:
                    log(
                        f"[恢复] Bitable 轮询恢复正常，连续失败 {consecutive_errors} 次"
                    )
                    last_error = None
                    consecutive_errors = 0
            except Exception as exc:
                # worker 级错误(如 Bitable 整体不可达)不终止 daemon
                signature = f"{type(exc).__name__}: {exc}"
                consecutive_errors += 1
                if signature != last_error or consecutive_errors % 12 == 0:
                    log(
                        f"[Worker Error] Bitable 轮询失败 "
                        f"(连续第 {consecutive_errors} 次): {signature}"
                    )
                last_error = signature

            time.sleep(poll_interval)
