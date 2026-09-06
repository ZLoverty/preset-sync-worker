from __future__ import annotations

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
        print("[Schema] Bitable 列检查完成")

    def poll_once(self) -> None:
        """一轮轮询。单条记录出错不中断本轮其余记录(P6 #30)。"""
        records = self.bitable.list_pending_records()

        for record_id, fields_map in records:
            try:
                self.submission_service.process_record(record_id, fields_map)
            except Exception as exc:
                # service 内已覆盖预期错误;兜底防止单行异常中断循环
                print(
                    f"[记录异常] record={record_id} "
                    f"{type(exc).__name__}: {exc}"
                )

        # 审查同步:推进「审核中」行(PR 合并 -> 已通过,关闭未合并 -> 已拒绝)。
        # service 内已按行隔离;整体异常由 run_forever 兜底,不终止 daemon。
        self.submission_service.sync_reviewing_rows()

    def run_forever(self, poll_interval: int) -> None:
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                # worker 级错误(如 Bitable 整体不可达)不终止 daemon
                print(
                    f"[Worker Error] {type(exc).__name__}: {exc}"
                )

            time.sleep(poll_interval)
