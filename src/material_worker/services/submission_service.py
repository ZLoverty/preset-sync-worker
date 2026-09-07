from __future__ import annotations

from datetime import datetime
from typing import Any

from material_worker import fields
from material_worker.adapters.bitable import BitableClient
from material_worker.adapters.git import GitRepository
from material_worker.domain.profile import (
    MaterialProfile,
    ProfileValidationError,
)
from material_worker.domain.status import SubmissionStatus
from material_worker.domain.submission import (
    MaterialSubmission,
    resolve_submission_id,
)
from material_worker.exceptions import (
    PermanentError,
    RetryableError,
)

# V2-P3:PR 被关闭(未合并)但 Git 侧无任何评论可作关闭理由时的兜底文案。
CLOSE_REASON_FALLBACK = "PR 已关闭,未说明原因"


class SubmissionService:
    """业务编排层:输入校验 -> claim -> Git 幂等提交 -> 回写状态。

    不包含任何 Lark/Git SDK 细节(P2 #11 / P5 #24),只调用两个 adapter。
    """

    def __init__(
        self,
        bitable: BitableClient,
        repository: GitRepository,
        max_retries: int = 5,
    ):
        self.bitable = bitable
        self.repository = repository
        self.max_retries = max_retries
        # 无对应 PR 的「审核中」行只告警一次,避免每轮刷屏
        self._warned_no_pr: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    def sync_reviewing_rows(self) -> None:
        """审查同步:对照 Git 侧 PR 状态推进「审核中」行。

        - PR 已合并        -> 状态=已通过;
        - PR 关闭未合并    -> 状态=已拒绝,回写关闭理由(V2-P3);
        - PR 仍打开/不存在 -> 保持不变(行留 审核中,下一轮再看)。

        V2-P3:关闭理由 = 关闭前最后一条普通评论正文;无评论写兜底文案;
        理由读取失败记日志、理由留空 —— 两种情况都不阻塞状态推进。
        Git 查询失败的行使状态保持 审核中,由下一轮自然重试
        (瞬态自愈,不误标终态);单行异常不中断其余行。
        """
        for record_id, row_fields in self.bitable.list_reviewing_records():
            raw_sid = row_fields.get(fields.SUBMISSION_ID)
            submission_id = str(raw_sid).strip() if raw_sid else ""
            if not submission_id:
                continue  # 无提交 ID 的行无法对照 Git,交给人工

            # 行若已被改动(人工/并发)则跳过,避免越权推进
            current = SubmissionStatus.from_table(row_fields.get(fields.STATUS))
            if current != SubmissionStatus.REVIEWING:
                continue

            try:
                state = self.repository.submission_pr_state(submission_id)
            except Exception as exc:
                print(
                    f"[审查同步失败] record={record_id} "
                    f"submission={submission_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            warn_key = (record_id, submission_id)
            if state == "merged":
                self._mark_reviewed(record_id, current, SubmissionStatus.APPROVED)
                self._warned_no_pr.discard(warn_key)
                print(
                    f"[审查同步] record={record_id} "
                    f"submission={submission_id} -> 已通过 (PR 已合并)"
                )
            elif state == "closed":
                close_reason = self._fetch_close_reason(record_id, submission_id)
                self._mark_reviewed(
                    record_id,
                    current,
                    SubmissionStatus.REJECTED,
                    close_reason,
                )
                self._warned_no_pr.discard(warn_key)
                print(
                    f"[审查同步] record={record_id} "
                    f"submission={submission_id} -> 已拒绝 (PR 关闭未合并,"
                    f"关闭理由: {close_reason or '(留空,待人工补充)'})"
                )
            elif state is None:
                self._warn_pr_missing(record_id, submission_id)

    def _mark_reviewed(
        self,
        record_id: str,
        current: SubmissionStatus,
        to: SubmissionStatus,
        close_reason: str = "",
    ) -> None:
        """推进审核中行到终态;回写失败时行保持 审核中,下轮重试。

        V2-P3:落 已拒绝 时带上关闭理由(已通过 不写理由,传空)。
        """
        current.assert_can_transition(to)
        try:
            if to is SubmissionStatus.APPROVED:
                self.bitable.mark_approved(record_id)
            else:
                self.bitable.mark_rejected(record_id, close_reason)
        except Exception as exc:
            print(
                f"[严重错误] 无法回写 {to.value} record={record_id}: {exc}"
            )

    def _fetch_close_reason(self, record_id: str, submission_id: str) -> str:
        """V2-P3:取该提交被关闭 PR 的关闭理由文本(仅 closed 分支调用)。

        - 关闭前最后一条评论的正文可作理由 -> 原样返回;
        - Git 侧无评论 -> 返回兜底文案(CLOSE_REASON_FALLBACK);
        - 评论读取失败(瞬时/网络)   -> 记日志返回空串,状态推进不受阻塞,
          理由列留空,由人工在 Bitable 里补充。
        """
        try:
            reason = self.repository.pr_close_reason(submission_id)
        except Exception as exc:
            print(
                f"[审查同步] record={record_id} submission={submission_id} "
                f"关闭理由读取失败,理由留空待人工补充: "
                f"{type(exc).__name__}: {exc}"
            )
            return ""
        if not reason:
            return CLOSE_REASON_FALLBACK
        return reason

    def _warn_pr_missing(self, record_id: str, submission_id: str) -> None:
        """Git 上找不到该提交的 PR:一次性告警,行保持 审核中 等人工处理。

        审查同步与 V2-P0 重复点击两条路径共用同一去重集合,避免刷屏。
        """
        warn_key = (record_id, submission_id)
        if warn_key in self._warned_no_pr:
            return
        self._warned_no_pr.add(warn_key)
        print(
            f"[审查同步] record={record_id} "
            f"submission={submission_id} 在 Git 上找不到对应 PR,"
            f"保持 审核中(若 PR 被手动删除,请人工处理该行)"
        )

    # ------------------------------------------------------------------
    def process_record(
        self,
        record_id: str,
        fields_snapshot: dict[str, Any],
    ) -> None:
        """处理一条 已请求=true 的记录(P0 #3 触发链路不变)。

        - 数据不完整/非法:直接永久失败,绝不进入 Git(P0 #5);
        - 之后任何临时失败都会把 已请求 重新置 true,由下一轮轮询自动重试,
          重试次数超过上限或遇永久错误才置为 失败。
        - 审核中(在途提交)行的重复触发不进入本轮完整流程,按 PR 实况
          轻处理,绝不产生第二个 PR(V2-P0,见 _handle_reviewing_click)。
        """
        # 0) V2-P0:在途行(审核中)的重复点击短路 —— 只收敛、不开新一轮。
        status = SubmissionStatus.from_table(
            fields_snapshot.get(fields.STATUS)
        )
        if status is SubmissionStatus.REVIEWING:
            raw_sid = fields_snapshot.get(fields.SUBMISSION_ID)
            submission_id = str(raw_sid).strip() if raw_sid else ""
            if submission_id:
                self._handle_reviewing_click(record_id, submission_id)
                return
            # 审核中 但无提交 ID:没有在途提交可对照,落入常规流程开新一轮。

        # 1) 解析 + 校验(claim 之前完成,坏数据不占坑)
        try:
            profile = MaterialProfile.from_bitable_record(record_id, fields_snapshot)
            profile.validate()
        except ProfileValidationError as exc:
            self._fail_permanently(record_id, f"数据校验失败: {exc}")
            return

        # 2) 提交 ID:复用组(待处理/处理中/失败/审核中)复用既有 ID,
        #    否则(已通过/已拒绝 等终态)新开一轮(P3 #18 / V2-P0)
        submission_id = resolve_submission_id(fields_snapshot)
        submission = MaterialSubmission(
            submission_id=submission_id,
            record_id=record_id,
            profile=profile,
            submitted_at=datetime.now().astimezone(),
        )
        submission.mark_processing()

        retry_count = self._snapshot_retry_count(fields_snapshot)

        # 3) claim -> Git -> 回写(整体重试安全:branch/PR 由提交 ID 幂等)
        try:
            self._claim(record_id, submission_id)

            result = self.repository.submit_profile(
                submission_id=submission_id,
                profile=profile,
            )
            submission.mark_reviewing(result.pull_request_url)

            self.bitable.mark_reviewing(record_id, result.pull_request_url)
            print(
                f"[完成] record={record_id} "
                f"submission={submission_id} PR={result.pull_request_url}"
            )

        except ProfileValidationError as exc:  # 双保险,理论上走不到
            self._fail_permanently(record_id, f"数据校验失败: {exc}")

        except _ClaimLost:
            pass  # 已被另一 worker 占用,静默让行

        except PermanentError as exc:
            self._fail_permanently(record_id, str(exc))

        except Exception as exc:
            # RetryableError 之外未知异常也走有限自动重试:
            # 临时 Git/网络故障绝不直接永久失败(P6 #27/#28)
            self._handle_transient_failure(
                record_id,
                submission_id,
                retry_count,
                f"{type(exc).__name__}: {exc}",
            )

    def _handle_reviewing_click(
        self,
        record_id: str,
        submission_id: str,
    ) -> None:
        """V2-P0:审核中(在途)行再次点击的轻处理 —— 绝不产生第二个 PR。

        按该提交在 Git 上的实际 PR 状态收敛(不依赖"状态==审核中"这一个
        条件,先确认在途 PR 的实况):

        - PR 仍打开   -> 保持 审核中,清除 已请求(本轮重复触发就此打住);
        - PR 已合并/已关闭 -> 清除 已请求,交回本轮随后的审查同步
                             (同轮置 已通过/已拒绝;关闭理由见 V2-P3);
        - Git 上找不到该提交的 PR -> 清除 已请求 + 一次性告警(需人工处理);
        - PR 状态查询失败(瞬时)   -> 不动行,已请求 保持 true,下轮自然重试。
        """
        try:
            state = self.repository.submission_pr_state(submission_id)
        except Exception as exc:
            print(
                f"[重复点击:无法确认 PR] record={record_id} "
                f"submission={submission_id} {type(exc).__name__}: {exc} "
                f"(已请求 保持 true,下轮自动重试)"
            )
            return

        self.bitable.clear_request(record_id)
        if state == "open":
            print(
                f"[重复点击] record={record_id} submission={submission_id} "
                f"在途 PR 仍打开:保持 审核中,不创建新 PR"
            )
        elif state is None:
            self._warn_pr_missing(record_id, submission_id)
        else:  # merged / closed:审查同步同轮落终态(已通过/已拒绝)
            print(
                f"[重复点击] record={record_id} submission={submission_id} "
                f"PR 状态={state}:交回审查同步落终态,不创建新 PR"
            )

    # ------------------------------------------------------------------
    # claim
    # ------------------------------------------------------------------
    def _claim(self, record_id: str, submission_id: str) -> None:
        """占用记录:已请求=false、状态=处理中、固定提交 ID。

        Feishu 无 CAS 更新,claim 后立即回读校验提交 ID 仍为本方所写;
        若已被另一 worker 占用则放弃本行(残余竞态由 Git 幂等兜底)。
        """
        self.bitable.mark_processing(record_id, submission_id)

        _, current = self.bitable.get_record(record_id)
        current_id = current.get(fields.SUBMISSION_ID)
        if current_id is not None and str(current_id).strip() != submission_id:
            print(
                f"[跳过] record={record_id} 已被另一 worker 占用 "
                f"(submission={current_id})"
            )
            raise _ClaimLost(record_id)

    # ------------------------------------------------------------------
    # 失败/重试策略
    # ------------------------------------------------------------------
    def _fail_permanently(self, record_id: str, message: str) -> None:
        """永久失败:状态=失败、已请求=false,等用户修正后再点按钮。"""
        try:
            self.bitable.mark_failed(record_id, message)
        except Exception as update_exc:
            print(
                f"[严重错误] 无法回写失败状态 record={record_id}: {update_exc}"
            )
        print(f"[数据/永久失败] record={record_id}: {message}")

    def _handle_transient_failure(
        self,
        record_id: str,
        submission_id: str,
        previous_retry_count: int,
        message: str,
    ) -> None:
        """瞬时失败:已请求 置回 true 由下轮自动重试,超过上限才失败。"""
        retry_count = previous_retry_count + 1

        if retry_count > self.max_retries:
            self._fail_permanently(
                record_id,
                f"{message} (已自动重试 {self.max_retries} 次仍未成功)",
            )
            return

        try:
            self.bitable.mark_retryable(
                record_id,
                submission_id,
                message,
                retry_count,
            )
        except Exception as update_exc:
            print(
                f"[严重错误] 无法标记重试 record={record_id}: {update_exc}"
            )
            raise

        print(
            f"[重试] record={record_id} submission={submission_id} "
            f"({retry_count}/{self.max_retries}): {message}"
        )

    @staticmethod
    def _snapshot_retry_count(fields_snapshot: dict[str, Any]) -> int:
        raw = fields_snapshot.get(fields.RETRY_COUNT)
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0


class _ClaimLost(Exception):
    """内部信号:claim 校验发现记录已被他人占用,本次不处理。"""
