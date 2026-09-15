from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable, Sequence

from material_worker import fields
from material_worker.adapters.bitable import AttachmentItem, BitableClient
from material_worker.adapters.git import GitRepository
from material_worker.adapters.lark_drive import DriveClient, DriveUpload
from material_worker.domain.attachment_import import (
    SUPPORTED_EXTENSIONS,
    AttachmentFormatError,
    extract_attachment_values,
)
from material_worker.domain.profile import (
    FIELD_SCHEMA,
    MaterialProfile,
    ProfileValidationError,
)
from material_worker.domain.status import SubmissionStatus
from material_worker.domain.submission import MaterialSubmission
from material_worker.exceptions import (
    PermanentError,
    RetryableError,
)
from material_worker.logging_setup import log

# V2-P3:PR 被关闭(未合并)但 Git 侧无任何评论可作关闭理由时的兜底文案。
CLOSE_REASON_FALLBACK = "PR 已关闭,未说明原因"

# V3:关闭理由**读取失败**(权限/网络/响应格式异常)时写入的占位文案 ——
# 与 CLOSE_REASON_FALLBACK 明确区分。原先这种情况写空串,表格上跟
# 「确实没人写理由」长得一样,而失败的成因往往是**永久性**的
# (Gitea token 缺 `read:issue` -> `/issues/{n}/comments` 一直 403),
# 行落终态后 worker 不再回看,理由就永久丢失、且无人察觉。
CLOSE_REASON_READ_ERROR = "关闭理由读取失败,请查看 worker 日志"
# 占位文案附带的异常摘要上限(单元格不宜过长)。
CLOSE_REASON_ERROR_DETAIL_MAX = 200

# V2-P2:附件大小上限(超出即判定数据问题,报错不解析)。
JSON_ATTACHMENT_MAX_BYTES = 1_000_000
# V2-P2:反写失败/解析失败写入「错误信息」的前缀,便于人工识别来源。
JSON_PARSE_ERROR_PREFIX = "附件解析失败: "

# V3-P10:「提交时间」取不出来时的月份层兜底目录名 —— 固定且显眼,
# 一眼能看出这批记录的时间信息有问题。
UNCLASSIFIED_MONTH = "未分类"


def _compact_timestamp(value: object) -> str:
    """「提交时间」原始值 -> 目录名可用的紧凑时间串;拿不到返回 ""。

    表格里可能是毫秒时间戳(int),也可能是已经格式化好的文本(`date_text`
    对文本原样返回,所以两种形态都得吃)。取不出来就交给调用方回退到内容
    摘要 —— 这里绝不返回常量,常量会导致两轮提交共用一个目录。
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            moment = datetime.fromtimestamp(value / 1000).astimezone()
        except (OverflowError, OSError, ValueError):
            return ""
        millis = moment.microsecond // 1000
        return moment.strftime("%Y%m%d-%H%M%S") + f"-{millis:03d}"

    digits = "".join(ch for ch in str(value) if ch.isdigit())[:14]
    if len(digits) < 6:  # 连年月都凑不出来,当缺失
        return ""
    if len(digits) > 8:
        return f"{digits[:8]}-{digits[8:]}"
    return digits


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB"


class SubmissionService:
    """业务编排层:输入校验 -> claim -> Git 幂等提交 -> 回写状态。

    不包含任何 Lark/Git SDK 细节(P2 #11 / P5 #24),只调用两个 adapter。
    """

    def __init__(
        self,
        bitable: BitableClient,
        repository: GitRepository,
        max_retries: int = 5,
        drive: DriveClient | None = None,
    ):
        self.bitable = bitable
        self.repository = repository
        self.max_retries = max_retries
        # V3-P10:过程记录存档(未配置 -> None,退回「只列文件名」)
        self.drive = drive
        # 无对应 PR 的「审核中」行只告警一次,避免每轮刷屏
        self._warned_no_pr: set[tuple[str, str]] = set()
        # V2-P2:同一 (record_id, file_token) 只解析一次 —— 确定性失败
        # (非法 JSON/超限/规则不符)不每轮重试刷屏;换附件(新 token)
        # 或重启进程后自然重试。瞬时失败不入该集合,下轮自动重试。
        self._json_parse_attempted: set[tuple[str, str]] = set()
        # V3-P5:表里没有「重试次数」列 —— 自动重试计数只存在于本进程内存
        # (record_id -> 连续失败次数),重启清零。成功/永久失败时清除。
        self._retry_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # V3-P9:重复身份检测
    # ------------------------------------------------------------------
    @staticmethod
    def identity_key(row_fields: dict[str, Any]) -> tuple[str, str, str] | None:
        """行的身份三元组(PI Code × 打印机型号 × 切片软件)。

        任一格为空则返回 None(身份不全的行由必填校验路径拦截,
        不参与重复判定)。
        """
        parts = [
            str(row_fields.get(col) or "").strip()
            for col in (
                fields.PI_CODE,
                fields.PRINTER_MODEL,
                fields.SLICER,
            )
        ]
        if not all(parts):
            return None
        return parts[0], parts[1], parts[2]

    @classmethod
    def duplicate_index(
        cls, records: Iterable[tuple[str, dict[str, Any]]]
    ) -> dict[tuple[str, str, str], list[str]]:
        """全表身份索引:{身份: [record_id, ...]}(含只有一个持有者的项)。"""
        index: dict[tuple[str, str, str], list[str]] = {}
        for record_id, row_fields in records:
            key = cls.identity_key(row_fields)
            if key is None:
                continue
            index.setdefault(key, []).append(record_id)
        return index

    def _conflicting_rows(
        self,
        record_id: str,
        row_fields: dict[str, Any],
        index: dict[tuple[str, str, str], list[str]],
    ) -> list[str]:
        key = self.identity_key(row_fields)
        if key is None:
            return []
        return [rid for rid in index.get(key, []) if rid != record_id]

    # ------------------------------------------------------------------
    # 轮询入口
    # ------------------------------------------------------------------
    def process_pending_rows(
        self, records: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """处理本轮 已请求=true 的行(worker 每轮把全表快照传进来)。

        全表快照只拉一次,重复身份检测与逐行处理共用同一份数据;
        单行异常不中断其余行(P6 #30)。
        """
        index = self.duplicate_index(records)
        for record_id, row_fields in records:
            if row_fields.get(fields.REQUESTED) is not True:
                continue
            identity = self.identity_key(row_fields)
            log(
                f"[提交开始] record={record_id} identity={identity or '(身份字段不全)'} "
                f"status={row_fields.get(fields.STATUS)!r}"
            )
            try:
                self.process_record(record_id, row_fields, index)
            except Exception as exc:
                log(
                    f"[记录异常] record={record_id} "
                    f"{type(exc).__name__}: {exc}"
                )

    # ------------------------------------------------------------------
    def sync_reviewing_rows(
        self, records: list[tuple[str, dict[str, Any]]] | None = None
    ) -> None:
        """审查同步:对照 Git 侧 PR 状态推进「审核中」行。

        V3-P5:`PR URL` 列是本轮 PR 的唯一锚点(同一 branch 上会累积多轮
        历史 PR,按 branch 取"第一个 PR"会误判)。

        - PR 已合并        -> 状态=已通过;
        - PR 关闭未合并    -> 状态=已拒绝,回写关闭理由(V2-P3);
        - PR 仍打开/查不到 -> 保持不变(行留 审核中,下一轮再看)。

        关闭理由 = 关闭前最后一条普通评论正文;无评论写兜底文案;
        理由读取失败记日志、理由留空 —— 两种情况都不阻塞状态推进。
        Git 查询失败的行使状态保持 审核中,由下一轮自然重试;
        单行异常不中断其余行。
        """
        if records is None:
            records = self.bitable.list_records()
        target = SubmissionStatus.REVIEWING.value
        for record_id, row_fields in records:
            if str(row_fields.get(fields.STATUS) or "").strip() != target:
                continue

            pr_url = str(row_fields.get(fields.PR_URL) or "").strip()
            if not pr_url:
                # 审核中但无 PR URL:没有可对照的 PR,交给人工(见 V2-P0 语义)
                self._warn_pr_missing(record_id, "(无 PR URL)")
                continue

            # 行若已被改动(人工/并发)则跳过,避免越权推进
            current = SubmissionStatus.from_table(row_fields.get(fields.STATUS))
            if current != SubmissionStatus.REVIEWING:
                continue

            try:
                state = self.repository.pr_state(pr_url)
            except Exception as exc:
                log(
                    f"[审查同步失败] record={record_id} pr={pr_url}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            warn_key = (record_id, pr_url)
            if state == "merged":
                self._mark_reviewed(record_id, current, SubmissionStatus.APPROVED)
                self._warned_no_pr.discard(warn_key)
                log(
                    f"[审查同步] record={record_id} {pr_url} -> 已通过 (PR 已合并)"
                )
            elif state == "closed":
                close_reason = self._fetch_close_reason(record_id, pr_url)
                self._mark_reviewed(
                    record_id,
                    current,
                    SubmissionStatus.REJECTED,
                    close_reason,
                )
                self._warned_no_pr.discard(warn_key)
                log(
                    f"[审查同步] record={record_id} {pr_url} -> 已拒绝 "
                    f"(PR 关闭未合并,关闭理由: {close_reason or '(留空,待人工补充)'})"
                )
            elif state is None:
                self._warn_pr_missing(record_id, pr_url)

    def _mark_reviewed(
        self,
        record_id: str,
        current: SubmissionStatus,
        to: SubmissionStatus,
        close_reason: str = "",
    ) -> None:
        """推进审核中行到终态;回写失败时行保持 审核中,下轮重试。"""
        current.assert_can_transition(to)
        try:
            if to is SubmissionStatus.APPROVED:
                self.bitable.mark_approved(record_id)
            else:
                self.bitable.mark_rejected(record_id, close_reason)
        except Exception as exc:
            log(
                f"[严重错误] 无法回写 {to.value} record={record_id}: {exc}"
            )

    def _fetch_close_reason(self, record_id: str, pr_url: str) -> str:
        """V2-P3:取该被关闭 PR 的关闭理由文本(仅 closed 分支调用)。

        - 关闭前最后一条评论的正文可作理由 -> 原样返回;
        - Git 侧无评论 -> 返回兜底文案(CLOSE_REASON_FALLBACK);
        - 读取失败(权限/网络/响应异常)   -> 记日志、格子里写**可区分的
          占位文案**(CLOSE_REASON_READ_ERROR + 异常摘要),状态照常推进:
          行一旦落终态 worker 就不再回看,这一格不能留成一片空白。
        """
        try:
            reason = self.repository.pr_close_reason(pr_url)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
            if len(detail) > CLOSE_REASON_ERROR_DETAIL_MAX:
                detail = detail[:CLOSE_REASON_ERROR_DETAIL_MAX] + "…"
            log(
                f"[审查同步] record={record_id} pr={pr_url} "
                f"关闭理由读取失败,格子写占位文案待人工补充: {detail}"
            )
            log(
                "          若上面是 403 鉴权失败:该 Git Token 缺少 issue "
                "读取权限(Gitea 需 read:issue,/issues/{n}/comments "
                "无此 scope 一律 403)"
            )
            return f"({CLOSE_REASON_READ_ERROR}: {detail})"
        if not reason:
            return CLOSE_REASON_FALLBACK
        return reason

    def _warn_pr_missing(self, record_id: str, marker: str) -> None:
        """Git 上找不到该行的 PR:一次性告警,行保持 审核中 等人工处理。"""
        warn_key = (record_id, marker)
        if warn_key in self._warned_no_pr:
            return
        self._warned_no_pr.add(warn_key)
        log(
            f"[审查同步] record={record_id} {marker} 在 Git 上找不到对应 PR,"
            f"保持 审核中(若 PR 被手动删除,请人工处理该行)"
        )

    # ------------------------------------------------------------------
    # V3-P3:附件导入(下载 -> 宽容解析 -> 只填空字段 -> 反写,不自动提交)
    # ------------------------------------------------------------------
    def backfill_pending_json_rows(
        self, records: list[tuple[str, dict[str, Any]]] | None = None
    ) -> None:
        """扫描附件待导入行:反写找到的字段。

        候选条件:
        - 行尚未进入提交生命周期(状态列为空或 草稿);
        - 附件列非空;
        - 标准字段至少有一个为空(字段已齐的行视为人工填写完成,不解析)。

        V3-P3 的两处关键变化:
        - **宽容取值** —— 只反写认得的键,缺键不再报错;
        - **不再自动提交** —— 反写后不置「已请求」,是否提交由用户点按钮
          决定(点按钮时还会做一次同样的反写兜底,规则一致)。

        确定性失败按 (record_id, file_token) 只尝试一次;瞬时失败
        (网络/限流)不记录,下轮自动重试。单行异常不中断扫描。
        """
        if records is None:
            records = self.bitable.list_records()
        for record_id, row_fields in records:
            try:
                self._backfill_one(record_id, row_fields)
            except Exception as exc:
                log(
                    f"[记录异常-附件解析] record={record_id} "
                    f"{type(exc).__name__}: {exc}"
                )

    @staticmethod
    def _single_attachment(items: list[Any]) -> Any:
        """严格单附件:恰好 1 个且为受支持的格式,违规抛 AttachmentFormatError。"""
        if len(items) != 1:
            raise AttachmentFormatError(
                f"「{fields.PROFILE_JSON}」列须恰好挂 1 个附件,"
                f"当前有 {len(items)} 个(多附件/混入其它文件都按失败处理)"
            )
        item = items[0]
        if not item.name.lower().endswith(SUPPORTED_EXTENSIONS):
            raise AttachmentFormatError(
                f"附件格式不支持: {item.name!r}。"
                f"仅支持 {' / '.join(SUPPORTED_EXTENSIONS)}"
            )
        return item

    def _download_attachment_text(self, item: Any) -> str:
        """下载 -> 大小/UTF-8 校验 -> 文本。

        确定性失败(超限/非 UTF-8/下载永久失败)抛 AttachmentFormatError;
        仅瞬时下载失败抛 RetryableError,由调用方按瞬态处理。
        """
        try:
            raw = self.bitable.download_attachment(item.file_token)
        except RetryableError:
            raise
        except Exception as exc:
            raise AttachmentFormatError(
                f"附件下载失败: {type(exc).__name__}: {exc}"
            ) from exc

        if len(raw) > JSON_ATTACHMENT_MAX_BYTES:
            raise AttachmentFormatError(
                f"附件超过大小上限 {JSON_ATTACHMENT_MAX_BYTES} 字节"
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AttachmentFormatError(f"附件不是 UTF-8 文本: {exc}") from exc

    def _extract_from_attachment(self, item: Any) -> dict[str, Any]:
        """下载 + 宽容解析 -> {列: 值}(可能为空字典)。"""
        text = self._download_attachment_text(item)
        return extract_attachment_values(text, item.name)

    @staticmethod
    def _fill_values(
        row_fields: dict[str, Any], parsed: dict[str, Any]
    ) -> dict[str, Any]:
        """只填空单元格的反写值(已填内容绝不被附件覆盖)。"""
        return {
            column: value
            for column, value in parsed.items()
            if str(row_fields.get(column) or "").strip() == ""
        }

    @staticmethod
    def _import_payload(values: dict[str, Any]) -> dict[str, Any]:
        """导入成功的一次原子写:填上的值 + 清历史错误 + **清空附件格**。

        V3-P3(用户确认):附件列是**一次性导入入口**,不是长期挂在行上的
        数据源 —— 值既已落进表格,原件就该让位。留着它会反噬:用户随后
        清空/改动某个格子(想改值、想让可选参数留空走继承),只要该格变空,
        附件就会被再读一遍并把它填回旧值(worker 重启后 `_json_parse_attempted`
        清零,必然再发生一次),手改反复被覆盖。

        「过程记录」是同一套语义:用完即清,原件不进 Git、不留表。
        """
        return {**values, fields.ERROR_MSG: "", fields.PROFILE_JSON: []}

    def _backfill_one(
        self, record_id: str, row_fields: dict[str, Any]
    ) -> None:
        status_text = str(row_fields.get(fields.STATUS) or "").strip()
        if status_text not in ("", SubmissionStatus.DRAFT.value):
            return  # 已进入提交生命周期(处理中/审核中/终态…)不动
        if not self._has_blank_fields(row_fields):
            return  # 必填已齐,视为人工填写,不解析

        items = self.bitable.attachment_items(row_fields)
        if not items:
            return
        attempt_key = (record_id, items[0].file_token)
        if attempt_key in self._json_parse_attempted:
            return
        try:
            item = self._single_attachment(items)
            parsed = self._extract_from_attachment(item)
        except RetryableError as exc:
            self._attachment_failure(
                record_id,
                None,
                f"附件下载失败(瞬时,下轮自动重试): {exc}",
                mark_attempted=False,
            )
            return
        except AttachmentFormatError as exc:
            self._attachment_failure(record_id, attempt_key, str(exc))
            return

        self._json_parse_attempted.add(attempt_key)
        values = self._fill_values(row_fields, parsed)
        if not values:
            # 一个关心的键都没认出来 -> 静默跳过(不写错误、不反写、
            # **不清附件格**:没提取到任何东西就删掉用户的文件太粗暴)
            log(
                f"[附件导入] record={record_id} 附件 {item.name!r} "
                f"未包含任何关心的键,跳过(附件保留)"
            )
            return

        try:
            # 一次原子反写:不产生部分脏数据
            self.bitable.update_record(
                record_id, self._import_payload(values)
            )
        except Exception as exc:
            log(f"[严重错误] 附件反写失败 record={record_id}: {exc}")
            self._json_parse_attempted.discard(attempt_key)  # 下轮自愈重试
            return
        # V3-P3:不再置「已请求」—— 是否提交由用户点按钮决定
        log(
            f"[附件导入] record={record_id} 由附件 {item.name!r} 反写 "
            f"{len(values)} 个字段(只填空,不自动提交),"
            f"并清空「{fields.PROFILE_JSON}」列"
        )

    @staticmethod
    def _has_blank_fields(row_fields: dict[str, Any]) -> bool:
        """canonical 字段(必填 + 可选)是否还有空位。

        V3-P3:可选列(线材密度/玻璃化温度/回抽距离/压力提前)正是附件最常
        携带的内容,因此「已填满」的判定必须覆盖全部 canonical 列 ——
        只要有任一列为空,附件就值得解析一次;全满则视为人工填写完成,
        不对附件做任何读取。
        """
        for f in FIELD_SCHEMA:
            value = row_fields.get(f.column)
            if value is None or str(value).strip() == "":
                return True
        return False

    def _attachment_failure(
        self,
        record_id: str,
        attempt_key: tuple[str, str] | None,
        message: str,
        *,
        mark_attempted: bool = True,
    ) -> None:
        """附件文件级问题统一收敛:写「错误信息」列 + 按需记 attempted。

        成功路径绝不写入任何部分数据(一次性反写)。
        """
        if mark_attempted and attempt_key is not None:
            self._json_parse_attempted.add(attempt_key)
        full = f"{JSON_PARSE_ERROR_PREFIX}{message}"
        try:
            self.bitable.update_record(record_id, {fields.ERROR_MSG: full})
        except Exception as update_exc:
            log(
                f"[严重错误] 无法回写附件解析错误 record={record_id}: "
                f"{update_exc}"
            )
        log(f"[附件解析失败] record={record_id}: {message}")

    # ------------------------------------------------------------------
    # 提交路径的附件兜底(V3-P3:best-effort,绝不阻塞提交)
    # ------------------------------------------------------------------
    def _import_at_claim(
        self,
        record_id: str,
        row_fields: dict[str, Any],
    ) -> dict[str, Any]:
        """必填有空位且挂了附件时,先按附件反写(只填空)再继续校验。

        V3-P3:附件问题**不再是提交失败的理由** —— 附件只是省手输的
        便捷入口,提交本身由用户点按钮决定。因此:

        - 解析成功 -> 反写落表(与轮询路径一样用完即清空附件格,见
          `_import_payload`)并把值合并进本次校验的快照;
        - 文件级问题/瞬时失败 -> 记日志后按原行数据继续,该报缺字段
          就报缺字段(用户看得懂,且修正后重新点击即可)。

        返回合并后的行快照(无附件/未解析出内容时原样返回)。
        """
        items = self.bitable.attachment_items(row_fields)
        if not items:
            return row_fields
        try:
            item = self._single_attachment(items)
            parsed = self._extract_from_attachment(item)
        except Exception as exc:
            log(
                f"[附件导入(提交路径)] record={record_id} 附件未能导入,"
                f"按表格现有数据继续: {type(exc).__name__}: {exc}"
            )
            return row_fields

        values = self._fill_values(row_fields, parsed)
        if not values:
            return row_fields
        payload = self._import_payload(values)
        try:
            self.bitable.update_record(record_id, payload)
        except Exception as exc:
            log(
                f"[严重错误] 提交前附件反写失败 record={record_id},"
                f"按表格现有数据继续: {exc}"
            )
            return {**row_fields, **values}
        log(
            f"[附件导入(提交路径)] record={record_id} 由附件 {item.name!r} "
            f"反写 {len(values)} 个字段,并清空「{fields.PROFILE_JSON}」列"
            f" -> 继续常规提交"
        )
        return {**row_fields, **payload}

    # ------------------------------------------------------------------
    # V3-P10:过程记录 -> 云文档
    # ------------------------------------------------------------------
    @staticmethod
    def round_folder_path(
        record_id: str,
        submit_time_raw: object,
        tokens: Sequence[str],
    ) -> tuple[str, str]:
        """本次提交的目标目录 (月份层, 叶子层) —— **整轮重试稳定**。

        叶子层 = `{提交时间}_{record_id}`:时间戳给出「哪一次提交」,
        `record_id` 给出「哪一行」。两者在整轮重试期间都不变,所以重试
        必然落回同一个目录;用户再点一次按钮时 Automation 会重写提交
        时间 -> 新目录 = 新的一次提交,正是要的语义。

        月份层不是审美:飞书**单层节点上限 1500**,几千次提交平铺在根
        目录下会直接超。

        缺 `提交时间` 时(理论上走不到)回退成**内容摘要**,绝不用常量 ——
        常量会让同一行两轮提交共用一个目录,第二轮的新照片被「按名跳过」
        当成已存在,**静默丢失**,PR 反而链到上一轮的照片。
        """
        stamp = _compact_timestamp(submit_time_raw)
        if stamp:
            return f"{stamp[:4]}-{stamp[4:6]}", f"{stamp}_{record_id}"

        digest = hashlib.sha1(
            "|".join([record_id, *sorted(tokens)]).encode("utf-8")
        ).hexdigest()[:12]
        return UNCLASSIFIED_MONTH, f"{digest}_{record_id}"

    def _publish_process_records(
        self,
        record_id: str,
        submit_time_raw: object,
        items: Sequence[AttachmentItem],
    ) -> tuple[list[str], str]:
        """把过程记录上传到云文档 -> (PR 正文用的文件名, 文件夹链接)。

        - 未配置云文档 / 无附件 -> 只返回文件名,行为与 V3-P8 完全一致;
        - 任一环节失败 -> 异常上抛,**本轮不建 PR**(证据优先于 PR)。

        展示给 PR 正文的永远是**原始文件名**,不受落盘改名(重名后缀、
        非法字符清洗)影响。
        """
        if self.drive is None or not items:
            return [item.name for item in items], ""

        uploads = [
            DriveUpload(
                name=item.name,
                content=self._download_process_record(item, self.drive.max_file_bytes),
            )
            for item in items
        ]
        month, leaf = self.round_folder_path(
            record_id, submit_time_raw, [item.file_token for item in items]
        )
        result = self.drive.publish((month, leaf), uploads)
        return [item.name for item in items], result.folder_url

    def _download_process_record(self, item: AttachmentItem, limit: int) -> bytes:
        """下载一个过程记录附件,并在下载前后各做一次体积预检。

        下载**前**用单元格里的 `size` 拦一道:media.download 会把整个
        响应体缓冲进内存,等拿到 bytes 再判断,内存已经吃进去了 ——
        所以预检必须在下载之前,这是唯一有效的防护。
        """
        if item.size is not None and item.size > limit:
            raise PermanentError(
                f"过程记录「{item.name}」{_mb(item.size)} 超过单文件上限 "
                f"{_mb(limit)},请压缩后重新上传该附件"
            )

        content = self.bitable.download_attachment(item.file_token)
        if len(content) > limit:
            # 单元格没给 size(或给得不准)时的兜底
            raise PermanentError(
                f"过程记录「{item.name}」{_mb(len(content))} 超过单文件上限 "
                f"{_mb(limit)},请压缩后重新上传该附件"
            )
        return content

    # ------------------------------------------------------------------
    def process_record(
        self,
        record_id: str,
        fields_snapshot: dict[str, Any],
        duplicate_index: dict[tuple[str, str, str], list[str]] | None = None,
    ) -> None:
        """处理一条 已请求=true 的记录(P0 #3 触发链路不变)。

        - V3-P9:身份与另一行重复 -> 永久失败,不产生 PR;
        - 数据不完整/非法:直接永久失败,绝不进入 Git(P0 #5);必填有空位
          但挂了附件时先按附件反写(只填空)再校验;
        - 之后任何临时失败都会把 已请求 重新置 true,由下一轮轮询自动重试;
        - 审核中(在途提交)行的重复触发不进入完整流程,按 PR 实况轻处理,
          绝不产生第二个 PR(V2-P0,见 _handle_reviewing_click)。
        """
        # 0) V3-P9:重复身份防护(先进入处理的行正常提交)
        if duplicate_index is not None:
            conflicts = self._conflicting_rows(
                record_id, fields_snapshot, duplicate_index
            )
            if conflicts:
                self._fail_permanently(
                    record_id,
                    f"该行身份(PI Code × 打印机型号 × 切片软件)与另一行重复:"
                    f"record={conflicts[0]}。一行 = 一个身份,请删除或修改"
                    f"其中一行后重新提交(未产生 PR)",
                )
                return

        # 1) V2-P0:在途行(审核中)的重复点击短路 —— 只收敛、不开新一轮。
        status = SubmissionStatus.from_table(
            fields_snapshot.get(fields.STATUS)
        )
        if status is SubmissionStatus.REVIEWING:
            pr_url = str(fields_snapshot.get(fields.PR_URL) or "").strip()
            if pr_url:
                self._handle_reviewing_click(record_id, pr_url)
            else:
                # 审核中但没有 PR 锚点:无从确认在途 PR,绝不贸然开第二个 PR
                self.bitable.clear_request(record_id)
                self._warn_pr_missing(record_id, "(无 PR URL)")
            return

        # 2) 解析 + 校验(claim 之前完成,坏数据不占坑)
        try:
            if self._has_blank_fields(fields_snapshot):
                fields_snapshot = self._import_at_claim(record_id, fields_snapshot)
            profile = MaterialProfile.from_bitable_record(record_id, fields_snapshot)
            profile.validate()
        except ProfileValidationError as exc:
            self._fail_permanently(record_id, f"数据校验失败: {exc}")
            return

        # 3) claim -> Git -> 回写(branch 由身份派生,整体重试安全)
        submission = MaterialSubmission(record_id=record_id, profile=profile)
        submission.mark_processing()

        try:
            fresh_fields = self._claim(record_id)
            if fresh_fields is not None:
                # V3-P7/P8:提交人 / 提交时间 / 过程记录取 claim 时的最新快照
                # (Automation 与「已请求」在同一次更新写入,这里读得最全)
                fields_snapshot = {**fields_snapshot, **fresh_fields}

            submitter = self.bitable.submitter_text(fields_snapshot)
            submit_time = self.bitable.submit_time_text(fields_snapshot)
            record_items = self.bitable.attachment_items(
                fields_snapshot, fields.PROCESS_RECORD
            )

            # V3-P10:先把记录存档到云文档,**再**建 PR —— 上传失败就抛出去,
            # 本轮不产生 PR(用户确认的语义:证据优先于 PR)。
            process_records, folder_url = self._publish_process_records(
                record_id,
                fields_snapshot.get(fields.SUBMIT_TIME),
                record_items,
            )

            result = self.repository.submit_profile(
                profile=profile,
                pull_request_url=str(
                    fields_snapshot.get(fields.PR_URL) or ""
                ).strip(),
                submitter=submitter,
                submit_time=submit_time,
                process_records=process_records,
                process_records_folder_url=folder_url,
            )
            submission.mark_reviewing(result.pull_request_url)

            # V3-P8/P10:记录已在云文档留档 -> PR 建立成功后清空该列
            # (判据是「这行有没有附件」,与上传有没有启用无关)
            self.bitable.mark_reviewing(
                record_id,
                result.pull_request_url,
                clear_process_record=bool(record_items),
            )
            self._retry_counts.pop(record_id, None)
            log(
                f"[完成] record={record_id} "
                f"identity={profile.identity} ({profile.slicer}) "
                f"PR={result.pull_request_url}"
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
                f"{type(exc).__name__}: {exc}",
            )

    def _handle_reviewing_click(self, record_id: str, pr_url: str) -> None:
        """V2-P0:审核中(在途)行再次点击的轻处理 —— 绝不产生第二个 PR。

        按行内 `PR URL` 指向的 PR 实况收敛:

        - PR 仍打开   -> 保持 审核中,清除 已请求(本轮重复触发就此打住);
        - PR 已合并/已关闭 -> 清除 已请求,交回本轮随后的审查同步
                             (同轮置 已通过/已拒绝;关闭理由见 V2-P3);
        - 查不到该 PR -> 清除 已请求 + 一次性告警(需人工处理);
        - 查询失败(瞬时)   -> 不动行,已请求 保持 true,下轮自然重试。
        """
        try:
            state = self.repository.pr_state(pr_url)
        except Exception as exc:
            log(
                f"[重复点击:无法确认 PR] record={record_id} pr={pr_url} "
                f"{type(exc).__name__}: {exc} "
                f"(已请求 保持 true,下轮自动重试)"
            )
            return

        self.bitable.clear_request(record_id)
        if state == "open":
            log(
                f"[重复点击] record={record_id} {pr_url} "
                f"在途 PR 仍打开:保持 审核中,不创建新 PR"
            )
        elif state is None:
            self._warn_pr_missing(record_id, pr_url)
        else:  # merged / closed:审查同步同轮落终态(已通过/已拒绝)
            log(
                f"[重复点击] record={record_id} {pr_url} "
                f"PR 状态={state}:交回审查同步落终态,不创建新 PR"
            )

    # ------------------------------------------------------------------
    # claim
    # ------------------------------------------------------------------
    def _claim(self, record_id: str) -> dict[str, Any] | None:
        """占用记录:已请求=false、状态=处理中、清空 PR URL/关闭理由。

        V3-P5:表里没有「提交 ID」列,无法再用它做 CAS 回读校验。
        这里回读一次状态确认无人抢占;真正的并发安全由
        **branch 由身份派生 + Git 幂等**兜底(两个 worker 同时处理同一行
        也只会收敛到同一个 branch/PR),生产仍建议单 worker 部署。

        返回回读到的最新行快照(调用方用于读取提交人/提交时间/过程记录)。
        """
        self.bitable.mark_processing(record_id)
        _, current = self.bitable.get_record(record_id)
        status = str(current.get(fields.STATUS) or "").strip()
        if status != SubmissionStatus.PROCESSING.value:
            log(
                f"[跳过] record={record_id} claim 后状态为 {status!r},"
                f"已被他人改动,本行让行"
            )
            raise _ClaimLost(record_id)
        return current

    # ------------------------------------------------------------------
    # 失败/重试策略
    # ------------------------------------------------------------------
    def _fail_permanently(self, record_id: str, message: str) -> None:
        """永久失败:状态=失败、已请求=false,等用户修正后再点按钮。"""
        self._retry_counts.pop(record_id, None)
        try:
            self.bitable.mark_failed(record_id, message)
        except Exception as update_exc:
            log(
                f"[严重错误] 无法回写失败状态 record={record_id}: {update_exc}"
            )
        log(f"[数据/永久失败] record={record_id}: {message}")

    def _handle_transient_failure(self, record_id: str, message: str) -> None:
        """瞬时失败:已请求 置回 true 由下轮自动重试,超过上限才失败。

        V3-P5:计数只在内存(self._retry_counts),重启清零。
        """
        retry_count = self._retry_counts.get(record_id, 0) + 1

        if retry_count > self.max_retries:
            self._fail_permanently(
                record_id,
                f"{message} (已自动重试 {self.max_retries} 次仍未成功)",
            )
            return

        self._retry_counts[record_id] = retry_count
        try:
            self.bitable.mark_retryable(record_id, message)
        except Exception as update_exc:
            log(
                f"[严重错误] 无法标记重试 record={record_id}: {update_exc}"
            )
            raise

        log(
            f"[重试] record={record_id} "
            f"({retry_count}/{self.max_retries}): {message}"
        )


class _ClaimLost(Exception):
    """内部信号:claim 校验发现记录已被他人改动,本次不处理。"""
