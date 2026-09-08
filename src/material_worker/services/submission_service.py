from __future__ import annotations

from datetime import datetime
from typing import Any

from material_worker import fields
from material_worker.adapters.bitable import BitableClient
from material_worker.adapters.git import GitRepository
from material_worker.domain.profile import (
    FIELD_SCHEMA,
    REQUIRED_FIELDS,
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

# V2-P2:附件 JSON 大小上限(超出即判定数据问题,报错不解析)。
JSON_ATTACHMENT_MAX_BYTES = 1_000_000
# V2-P2:反写失败/解析失败写入「错误信息」的前缀,便于人工识别来源。
JSON_PARSE_ERROR_PREFIX = "附件解析失败: "


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
        # V2-P2:同一 (record_id, file_token) 只解析一次 —— 确定性失败
        # (非法 JSON/超限/规则不符)不每轮重试刷屏;换附件(新 token)
        # 或重启进程后自然重试。瞬时失败不入该集合,下轮自动重试。
        self._json_parse_attempted: set[tuple[str, str]] = set()

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
    # V2-P2:JSON 附件导入(上传 -> 下载 -> 解析 -> 只填空字段 -> 反写)
    # ------------------------------------------------------------------
    def backfill_pending_json_rows(self) -> None:
        """扫描 JSON 附件待导入行:反写字段并自动进入提交流程。

        候选条件(V2-P4,§5.2 上传语义 = 新建材料):
        - 行尚未进入提交生命周期(状态列为空或 草稿);
        - 「Profile JSON」附件列非空;
        - 必填字段(品名/品牌/机型/切片器 + 10 项耗材参数)至少有一个为空
          (字段已齐的行视为人工填写完成,不解析、不自动提交)。

        解析与校验通过后(附件须含全部 14 个必填键,其余键忽略):
        只填空单元格,用户已填内容绝不被附件覆盖;反写 + 置「已请求」
        在一次原子 update 完成(V2-P4:上传 JSON = 新建材料意图,由下一轮
        轮询自动 claim -> Git 提交 -> PR,与按钮同一路径),失败不产生
        部分脏数据。解析/校验失败只写「错误信息」,不置请求、不提交。

        确定性失败按 (record_id, file_token) 只尝试一次;瞬时失败
        (网络/限流)不记录,下轮自动重试。单行异常不中断扫描。
        """
        for record_id, row_fields in self.bitable.list_records():
            try:
                self._backfill_one_json(record_id, row_fields)
            except Exception as exc:
                print(
                    f"[记录异常-附件解析] record={record_id} "
                    f"{type(exc).__name__}: {exc}"
                )

    # ------------------------------------------------------------------
    # V2-P4:附件解析核心(反写轮与 claim 路径共用同一规则/解析/只填空)
    # ------------------------------------------------------------------
    @staticmethod
    def _single_json_item(items: list[Any]) -> Any:
        """严格单 JSON(R2):恰好 1 个附件且为 .json 文件,违规抛异常。"""
        if len(items) != 1:
            raise _AttachmentImportError(
                f"「{fields.PROFILE_JSON}」列须恰好挂 1 个 JSON 附件,"
                f"当前有 {len(items)} 个(多附件/混入其它文件都按失败处理)"
            )
        item = items[0]
        if not item.name.lower().endswith(".json"):
            raise _AttachmentImportError(f"附件不是 .json 文件: {item.name!r}")
        return item

    def _download_and_parse_json(self, item: Any) -> MaterialProfile:
        """下载 -> 大小/UTF-8 -> 解析 + 校验(与手工填表共用 schema)。

        确定性失败(规则/超限/编码/下载永久失败/解析/校验)抛
        _AttachmentImportError,消息即完整提示(无前缀);
        仅瞬时下载失败抛 RetryableError,由调用方按瞬态处理。
        """
        try:
            raw = self.bitable.download_attachment(item.file_token)
        except RetryableError:
            raise
        except Exception as exc:
            raise _AttachmentImportError(
                f"附件下载失败: {type(exc).__name__}: {exc}"
            ) from exc

        if len(raw) > JSON_ATTACHMENT_MAX_BYTES:
            raise _AttachmentImportError(
                f"附件超过大小上限 {JSON_ATTACHMENT_MAX_BYTES} 字节"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _AttachmentImportError(
                f"附件不是 UTF-8 文本: {exc}"
            ) from exc

        try:
            profile = MaterialProfile.parse_attachment_json(text)
            profile.validate()
        except ProfileValidationError as exc:
            raise _AttachmentImportError(str(exc)) from exc
        return profile

    @staticmethod
    def _attachment_fill_values(
        row_fields: dict[str, Any], profile: MaterialProfile
    ) -> dict[str, Any]:
        """附件 -> 只填空单元格的全字段反写值(canonical FIELD_SCHEMA 驱动)。

        已填内容绝不被覆盖;「材料ID」只有附件显式给出且与品名不同才落列
        (缺省=品名不落)。反写值不含 REQUESTED/ERROR_MSG,由调用方决定。
        """
        values: dict[str, Any] = {}
        for f in FIELD_SCHEMA:
            col = f.column
            row_value = row_fields.get(col)
            if row_value is not None and str(row_value).strip() != "":
                continue  # 用户已填,不覆盖
            value = getattr(profile, f.key)
            if f.key == "id":
                if profile.id is not None and profile.id != profile.name:
                    values[col] = profile.id
            elif value is not None:
                values[col] = value
        return values

    def _backfill_one_json(
        self, record_id: str, row_fields: dict[str, Any]
    ) -> None:
        status_text = str(row_fields.get(fields.STATUS) or "").strip()
        if status_text not in ("", SubmissionStatus.DRAFT.value):
            return  # 已进入提交生命周期(处理中/审核中/终态…)不动
        if not self._has_blank_standard_fields(row_fields):
            return  # 标准字段已齐,视为人工填写,不解析

        items = self.bitable.attachment_items(row_fields)
        if not items:
            return
        # 严格单 JSON(R2):恰好 1 个附件且为 .json 文件
        attempt_key = (record_id, items[0].file_token)
        if attempt_key in self._json_parse_attempted:
            return
        try:
            item = self._single_json_item(items)
            profile = self._download_and_parse_json(item)
        except RetryableError as exc:
            self._json_parse_failure(
                record_id,
                None,
                f"附件下载失败(瞬时,下轮自动重试): {exc}",
                mark_attempted=False,
            )
            return
        except _AttachmentImportError as exc:
            self._json_parse_failure(record_id, attempt_key, str(exc))
            return

        values = self._attachment_fill_values(row_fields, profile)
        values[fields.ERROR_MSG] = ""  # 成功清掉历史解析错误

        # V2-P4:上传 JSON = 新建材料意图(用户确认)—— 解析+校验通过说明
        # 必填字段齐(validate 已把关),反写与置「已请求」在同一次原子
        # update 完成:下一轮轮询走与按钮完全相同的常规提交流程
        # (claim -> Git 幂等提交 -> 审核中 + PR)。失败路径不置请求,
        # 修正附件后由新 token 自然重试。
        values[fields.REQUESTED] = True
        try:
            # 一次原子反写:不产生部分脏数据
            self.bitable.update_record(record_id, values)
        except Exception as exc:
            print(f"[严重错误] 附件反写失败 record={record_id}: {exc}")
            return  # 不标记 attempted,下轮自愈重试
        self._json_parse_attempted.add((record_id, item.file_token))
        print(
            f"[附件导入] record={record_id} 由附件 {item.name!r} "
            f"反写完成,字段已齐 -> 已置「已请求」,下轮自动进入提交流程"
        )

    @staticmethod
    def _has_blank_standard_fields(row_fields: dict[str, Any]) -> bool:
        """必填 canonical 字段是否还有空位(V2-P4:14 项全量必填)。"""
        for f in REQUIRED_FIELDS:
            value = row_fields.get(f.column)
            if value is None or str(value).strip() == "":
                return True
        return False

    def _json_parse_failure(
        self,
        record_id: str,
        attempt_key: tuple[str, str] | None,
        message: str,
        *,
        mark_attempted: bool = True,
    ) -> None:
        """解析失败统一收敛:写「错误信息」列 + 按需记 attempted + 打印。

        成功路径绝不写入任何部分数据(§5.8:一次性反写)。
        """
        if mark_attempted and attempt_key is not None:
            self._json_parse_attempted.add(attempt_key)
        full = f"{JSON_PARSE_ERROR_PREFIX}{message}"
        try:
            self.bitable.update_record(record_id, {fields.ERROR_MSG: full})
        except Exception as update_exc:
            print(
                f"[严重错误] 无法回写附件解析错误 record={record_id}: "
                f"{update_exc}"
            )
        print(f"[附件解析失败] record={record_id}: {message}")

    # ------------------------------------------------------------------
    # V2-P4:claim 路径的附件导入 —— 点「请求」时必填仍空 + 挂 JSON 附件,
    # 当场先按附件反写再提交(上传=新建材料),不依赖反写轮先跑。
    # ------------------------------------------------------------------
    def _import_json_at_claim(
        self,
        record_id: str,
        fields_snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        """按附件反写空白必填字段并把反写内容一次落表(不置「已请求」)。

        与反写轮(_backfill_one_json)共用 _single_json_item /
        _download_and_parse_json / _attachment_fill_values,「只填空、不覆盖」
        一致。返回反写值(供调用方合并出 profile);无附件返回 None。

        - 附件确定性失败(规则/解析/校验/下载永久失败)-> 抛 _AttachmentFailure
          (消息带「附件解析失败:」前缀),由调用方永久失败处理;
        - 瞬时下载失败 -> 抛 RetryableError(已带前缀),由调用方自动重试;
        - 反写落表失败 -> 原样上抛,调用方按未知异常走有限自动重试。
        """
        items = self.bitable.attachment_items(fields_snapshot)
        if not items:
            return None
        try:
            item = self._single_json_item(items)
            profile = self._download_and_parse_json(item)
        except RetryableError as exc:
            raise RetryableError(
                f"{JSON_PARSE_ERROR_PREFIX}附件下载失败(瞬时,下轮自动重试): "
                f"{exc}"
            ) from exc
        except _AttachmentImportError as exc:
            raise _AttachmentFailure(f"{JSON_PARSE_ERROR_PREFIX}{exc}") from exc

        values = self._attachment_fill_values(fields_snapshot, profile)
        values[fields.ERROR_MSG] = ""  # 反写成功清掉历史错误
        try:
            # 先落表再继续提交:成功后行内数据与提交内容一致,失败路径
            # (如 Git 永久失败)也不丢反写结果;该 update 失败交由调用方重试。
            self.bitable.update_record(record_id, values)
        except Exception:
            print(f"[严重错误] 提交前附件反写失败 record={record_id}: 下轮自动重试")
            raise
        print(
            f"[附件导入(提交路径)] record={record_id} 由附件 {item.name!r} "
            f"反写完成 -> 继续常规提交"
        )
        return values

    # ------------------------------------------------------------------
    def process_record(
        self,
        record_id: str,
        fields_snapshot: dict[str, Any],
    ) -> None:
        """处理一条 已请求=true 的记录(P0 #3 触发链路不变)。

        - 数据不完整/非法:直接永久失败,绝不进入 Git(P0 #5);其中必填
          字段有空位但挂有「Profile JSON」附件时,先按附件反写再校验
          (V2-P4 上传=新建,点按钮与纯上传收敛到同一提交流程);
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
        #    V2-P4:必填有空位且附件在场 -> 先反写(只填空)合并进快照,再走
        #    常规校验;附件问题 -> 永久失败(前缀「附件解析失败:」),瞬时
        #    下载/反写失败 -> 有限自动重试,绝不占坑或静默跳过。
        try:
            if self._has_blank_standard_fields(fields_snapshot):
                fills = self._import_json_at_claim(record_id, fields_snapshot)
                if fills:
                    fields_snapshot = {**fields_snapshot, **fills}
            profile = MaterialProfile.from_bitable_record(record_id, fields_snapshot)
            profile.validate()
        except _AttachmentFailure as exc:
            self._fail_permanently(record_id, str(exc))
            return
        except RetryableError as exc:
            self._handle_transient_failure(
                record_id,
                resolve_submission_id(fields_snapshot),
                self._snapshot_retry_count(fields_snapshot),
                str(exc),
            )
            return
        except ProfileValidationError as exc:
            self._fail_permanently(record_id, f"数据校验失败: {exc}")
            return
        except Exception as exc:
            # 附件反写落表失败等未知异常:走有限自动重试(P6 #27 同策略)
            self._handle_transient_failure(
                record_id,
                resolve_submission_id(fields_snapshot),
                self._snapshot_retry_count(fields_snapshot),
                f"{type(exc).__name__}: {exc}",
            )
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


class _AttachmentImportError(Exception):
    """附件确定性问题的内部信号(规则/超限/编码/下载永久失败/解析/校验)。

    消息即完整提示文案(无前缀);由调用方(反写轮写「错误信息」列 /
    claim 路径转终态失败)各自收敛。
    """


class _AttachmentFailure(Exception):
    """claim 路径附件确定性失败的终态信号(消息已带「附件解析失败:」前缀)。"""
