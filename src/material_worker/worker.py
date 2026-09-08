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

        # V2-P2/V2-P4:JSON 附件导入 —— 未进入提交生命周期(状态空/草稿)的
        # 行挂 Profile JSON 附件时解析反写 + 置「已请求」,由下一轮轮询自动
        # claim;已点「请求」的行由 process_record 在处理时先按附件反写再提交,
        # 两个入口共用同一解析/只填空规则(见 submission_service)。
        self.submission_service.backfill_pending_json_rows()

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
