from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableField,
    AppTableRecord,
    CreateAppTableFieldRequest,
    GetAppTableRecordRequest,
    ListAppTableFieldRequest,
    ListAppTableRecordRequest,
    UpdateAppTableRecordRequest,
)
from lark_oapi.api.drive.v1 import DownloadMediaRequest

from material_worker import fields
from material_worker.domain.status import SubmissionStatus
from material_worker.exceptions import (
    BitableError,
    PermanentError,
    RetryableError,
)

# Feishu Bitable 字段 type:1=文本 2=数字 3=单选 4=多选 5=日期 7=复选框
# 11=人员 17=附件 3001=按钮
FIELD_TYPE_TEXT = 1
FIELD_TYPE_NUMBER = 2
FIELD_TYPE_SINGLE_SELECT = 3
FIELD_TYPE_MULTI_SELECT = 4
FIELD_TYPE_DATE = 5
FIELD_TYPE_CHECKBOX = 7
FIELD_TYPE_PERSON = 11
FIELD_TYPE_ATTACHMENT = 17
FIELD_TYPE_BUTTON = 3001

# 字段 type -> 中文名(仅用于错误提示)
_FIELD_TYPE_LABELS: dict[int, str] = {
    FIELD_TYPE_TEXT: "文本",
    FIELD_TYPE_NUMBER: "数字",
    FIELD_TYPE_SINGLE_SELECT: "单选",
    FIELD_TYPE_MULTI_SELECT: "多选",
    FIELD_TYPE_DATE: "日期",
    FIELD_TYPE_CHECKBOX: "复选框",
    FIELD_TYPE_PERSON: "人员",
    FIELD_TYPE_ATTACHMENT: "附件",
    FIELD_TYPE_BUTTON: "按钮",
}

# ----------------------------------------------------------------------
# V3-P1:schema 保障分两类
#
# A. AUTO_CREATE —— 缺列时 worker 自动补齐(文本/数字/附件三类,
#    无选项、无业务语义,补出来必然是正确形态);
# B. VALIDATE_ONLY —— 单选/复选框/按钮/人员/日期列,**只校验不创建**:
#    这些列带选项或依赖表格其他配置(按钮还要挂 Automation),
#    自动创建一个空壳没有意义,缺列或类型不符一律启动失败 + 修复指引。
# ----------------------------------------------------------------------

# A. worker 可安全自动创建的缺失列:列名 -> 字段类型
AUTO_CREATE_FIELDS: dict[str, int] = {
    # 结构参数(文本)
    fields.INHERITS: FIELD_TYPE_TEXT,
    # 提交元数据(文本)
    fields.PR_URL: FIELD_TYPE_TEXT,
    fields.ERROR_MSG: FIELD_TYPE_TEXT,
    fields.CLOSE_REASON: FIELD_TYPE_TEXT,
    # 附件列
    fields.PROFILE_JSON: FIELD_TYPE_ATTACHMENT,
    fields.PROCESS_RECORD: FIELD_TYPE_ATTACHMENT,
    # 耗材参数(数字)
    fields.BED_TEMP: FIELD_TYPE_NUMBER,
    fields.NOZZLE_TEMP: FIELD_TYPE_NUMBER,
    fields.FAN_COOLING_LAYER_TIME: FIELD_TYPE_NUMBER,
    fields.FAN_MAX_SPEED: FIELD_TYPE_NUMBER,
    fields.FAN_MIN_SPEED: FIELD_TYPE_NUMBER,
    fields.SLOW_DOWN_LAYER_TIME: FIELD_TYPE_NUMBER,
    fields.FLOW_RATIO: FIELD_TYPE_NUMBER,
    fields.MAX_VOL_SPEED: FIELD_TYPE_NUMBER,
    fields.RETRACTION_LENGTH: FIELD_TYPE_NUMBER,
    fields.PRESSURE_ADVANCE: FIELD_TYPE_NUMBER,
    fields.FILAMENT_DENSITY: FIELD_TYPE_NUMBER,
    fields.VITRIFICATION: FIELD_TYPE_NUMBER,
}

# B. 只校验不创建:列名 -> (期望字段类型, 修复提示)
_VALIDATE_ONLY_FIELDS: dict[str, tuple[int, str]] = {
    fields.STATUS: (
        FIELD_TYPE_SINGLE_SELECT,
        "选项需包含: " + " / ".join(s.value for s in SubmissionStatus),
    ),
    fields.REQUESTED: (
        FIELD_TYPE_CHECKBOX,
        "按钮 -> Automation 置为已勾选(worker 清除)",
    ),
    fields.SUBMIT_BUTTON: (
        FIELD_TYPE_BUTTON,
        "按钮列,需在 Automation 中配置「点击后置 已请求/提交人/提交时间」",
    ),
    fields.PI_CODE: (FIELD_TYPE_SINGLE_SELECT, "选项 = 全部 PI Code(人工维护)"),
    fields.PRINTER_MODEL: (
        FIELD_TYPE_SINGLE_SELECT,
        "选项 = 「品牌 机型」形式(如 BBL P2S / Prusa Core One),人工维护",
    ),
    fields.SLICER: (FIELD_TYPE_SINGLE_SELECT, "选项 = 全部切片软件(人工维护)"),
    fields.PM_METHOD_VERSION: (
        FIELD_TYPE_SINGLE_SELECT,
        "选项 = 调参方法版本(人工维护)",
    ),
    fields.SUBMITTER: (FIELD_TYPE_PERSON, "人员列,由按钮 Automation 写入"),
    fields.SUBMIT_TIME: (FIELD_TYPE_DATE, "日期列,由按钮 Automation 写入"),
}

# V2-P1:单选列「状态」必须具备状态机全集选项
_STATUS_REQUIRED_OPTIONS: list[str] = [s.value for s in SubmissionStatus]


def _make_callable_error(exc: Exception) -> RetryableError:
    return RetryableError(f"Bitable 请求失败(网络/超时): {type(exc).__name__}: {exc}")


def _type_label(field_type: object) -> str:
    try:
        return _FIELD_TYPE_LABELS.get(int(field_type), f"未知(type={field_type})")
    except (TypeError, ValueError):
        return f"未知(type={field_type})"


@dataclass(frozen=True)
class AttachmentItem:
    """附件单元格里的一项(附件列原始值形态的适配层归一化)。"""

    file_token: str
    name: str


def person_names(cell: Any) -> list[str]:
    """人员列单元格 -> 姓名列表(空/形态异常返回空列表)。

    飞书返回 list[{id, name, en_name}];兼容单个 dict 与纯字符串形态。
    """
    entries = cell if isinstance(cell, list) else [cell]
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("en_name")
            if name:
                names.append(str(name).strip())
        elif isinstance(entry, str) and entry.strip():
            names.append(entry.strip())
    return names


def date_text(cell: Any) -> str:
    """日期列单元格 -> 本地时间文本(空/非法返回空串)。

    飞书日期字段返回**毫秒**时间戳;兼容已是字符串的形态。
    """
    if cell is None or cell == "":
        return ""
    if isinstance(cell, str):
        text = cell.strip()
        if not text:
            return ""
        try:
            cell = float(text)
        except ValueError:
            return text  # 已是可读文本,原样返回
    try:
        millis = float(cell)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(millis / 1000).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


class BitableClient:
    """Feishu Bitable 的唯一 SDK 交互入口(P2)。

    - 业务逻辑只调用这里的方法,不直接接触 lark_oapi;
    - 列名映射集中在本文件与 fields.py;
    - 传输层异常包装为 RetryableError;接口错误码失败抛 BitableError,
      由 service 按重试策略统一处理。
    """

    def __init__(
        self,
        client: lark.Client,
        app_token: str,
        table_id: str,
    ):
        self.client = client
        self.app_token = app_token
        self.table_id = table_id

    # ------------------------------------------------------------------
    # 通用调用包装
    # ------------------------------------------------------------------
    def _call(self, fn: Callable[[], Any], action: str) -> Any:
        try:
            resp = fn()
        except RetryableError:
            raise
        except Exception as exc:  # lark SDK 在传输层抛出的异常
            raise _make_callable_error(exc) from exc

        if not resp.success():
            code, msg = resp.code, resp.msg
            if code == 99991400:  # 访问频繁/系统繁忙
                raise RetryableError(
                    f"Bitable {action} 被限流: code={code}, msg={msg}"
                )
            raise BitableError(
                f"Bitable {action} failed: code={code}, msg={msg}"
            )
        return resp

    # ------------------------------------------------------------------
    # 读记录
    # ------------------------------------------------------------------
    def list_records(self) -> list[tuple[str, dict[str, Any]]]:
        """全表分页拉取 (record_id, fields)。"""
        records: list[tuple[str, dict[str, Any]]] = []
        page_token: str | None = None

        while True:
            builder = (
                ListAppTableRecordRequest.builder()
                .app_token(self.app_token)
                .table_id(self.table_id)
                .page_size(500)
            )
            if page_token:
                builder.page_token(page_token)

            resp = self._call(
                lambda: self.client.bitable.v1.app_table_record.list(
                    builder.build()
                ),
                "list records",
            )

            for item in resp.data.items or []:
                records.append((item.record_id, item.fields))
            if not resp.data.has_more:
                break
            page_token = resp.data.page_token

        return records

    def list_pending_records(self) -> list[tuple[str, dict[str, Any]]]:
        """只取 已请求=true 的记录(供单测/脚本使用;worker 走全表快照)。

        不做服务端过滤:实测飞书 list 的 CurrentValue 公式过滤与
        search 的结构化过滤对本表复选框字段都会**静默返回空**
        (code=0、语法合法,但匹配不到任何已勾选行;对部分文本
        字段同样失效),无法依赖。表格按行追加、量级小,采用
        全表拉取 + 本地过滤——本地按 API 真实返回的布尔值
        (True/False)判定,是唯一实测可靠的方式。
        """
        return [
            (record_id, record_fields)
            for record_id, record_fields in self.list_records()
            if record_fields.get(fields.REQUESTED) is True
        ]

    # ------------------------------------------------------------------
    # 附件(读取 + 媒体下载)—— V3 有两个附件列:导入用与过程记录
    # ------------------------------------------------------------------
    def attachment_items(
        self, row_fields: dict[str, Any], column: str = fields.PROFILE_JSON
    ) -> list[AttachmentItem]:
        """把一行快照里某个附件列的原始值归一化为 AttachmentItem 列表。

        附件列单元格在 API 返回里是 list[{file_token, name, …}];
        空/缺列/形态异常一律返回空列表,由调用方按业务规则判定。
        """
        cell = row_fields.get(column)
        if not isinstance(cell, list):
            return []
        items: list[AttachmentItem] = []
        for entry in cell:
            if not isinstance(entry, dict):
                continue
            token = entry.get("file_token")
            name = entry.get("name")
            if token is None or name is None:
                continue
            items.append(AttachmentItem(file_token=str(token), name=str(name)))
        return items

    def download_attachment(self, file_token: str) -> bytes:
        """下载附件(file_token)的原始字节(飞书媒体下载 API)。

        网络/限流等瞬时问题抛 RetryableError;其余失败抛 BitableError,
        由调用方按数据问题处理(不自动重试)。
        """
        request = (
            DownloadMediaRequest.builder()
            .file_token(file_token)
            .build()
        )
        resp = self._call(
            lambda: self.client.drive.v1.media.download(request),
            "download attachment",
        )
        if resp.file is None:
            raise BitableError("下载附件失败: 响应缺少文件内容")
        try:
            content = resp.file.read()
        except Exception as exc:
            raise BitableError(
                f"下载附件失败,无法读取响应流: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(content, bytes):
            raise BitableError("下载附件失败: 响应不是二进制内容")
        return content

    # ------------------------------------------------------------------
    # V3-P7:提交人 / 提交时间(只读,worker 不改写这两列)
    # ------------------------------------------------------------------
    @staticmethod
    def submitter_text(row_fields: dict[str, Any]) -> str:
        """「提交人」列 -> 展示文本;多人以「、」连接,空 -> "(未记录)"。"""
        names = person_names(row_fields.get(fields.SUBMITTER))
        return "、".join(names) if names else "(未记录)"

    @staticmethod
    def submit_time_text(row_fields: dict[str, Any]) -> str:
        """「提交时间」列 -> 本地时间文本;空 -> "(未记录)"。"""
        return date_text(row_fields.get(fields.SUBMIT_TIME)) or "(未记录)"

    # ------------------------------------------------------------------
    # V3-P8:过程记录(只取文件名,附件本体不进 Git)
    # ------------------------------------------------------------------
    def process_record_names(self, row_fields: dict[str, Any]) -> list[str]:
        return [
            item.name
            for item in self.attachment_items(row_fields, fields.PROCESS_RECORD)
        ]

    def get_record(self, record_id: str) -> tuple[str, dict[str, Any]]:
        request = (
            GetAppTableRecordRequest.builder()
            .app_token(self.app_token)
            .table_id(self.table_id)
            .record_id(record_id)
            .build()
        )
        resp = self._call(
            lambda: self.client.bitable.v1.app_table_record.get(request),
            "get record",
        )
        record: AppTableRecord | None = resp.data.record
        if record is None or record.fields is None:
            raise BitableError(
                f"Bitable get record returned empty: record={record_id}"
            )
        return record.record_id, record.fields

    # ------------------------------------------------------------------
    # 写记录(回写由这里集中构造,值/列名映射只有一份)
    # ------------------------------------------------------------------
    def update_record(self, record_id: str, values: dict[str, Any]) -> None:
        body = AppTableRecord.builder().fields(values).build()
        request = (
            UpdateAppTableRecordRequest.builder()
            .app_token(self.app_token)
            .table_id(self.table_id)
            .record_id(record_id)
            .request_body(body)
            .build()
        )
        self._call(
            lambda: self.client.bitable.v1.app_table_record.update(request),
            "update record",
        )

    def clear_request(self, record_id: str) -> None:
        """V2-P0:仅清除 已请求(在途行的重复触发就此打住),不动状态/内容。"""
        self.update_record(record_id, {fields.REQUESTED: False})

    def mark_processing(self, record_id: str) -> None:
        """claim:占用该记录(已请求=false,防止重复轮询)。

        V3-P5:claim 同时清空上一轮的 `PR URL` 与 `关闭理由` —— 表里没有
        提交 ID 列,`PR URL` 就是本轮 PR 的锚点,必须只指向本轮;新一轮
        提交时旧的(已合并/已关闭)PR URL 必须让位,否则审查同步会对着
        上一轮的 PR 判定。
        """
        self.update_record(
            record_id,
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.PROCESSING.value,
                fields.ERROR_MSG: "",
                fields.PR_URL: "",
                fields.CLOSE_REASON: "",
            },
        )

    def mark_reviewing(
        self,
        record_id: str,
        pull_request_url: str,
        *,
        clear_process_record: bool = False,
    ) -> None:
        """Git 提交成功:状态=审核中,记录 PR URL,清空错误信息。

        V3-P8:PR 建立成功后,与本次回写一并清空「过程记录」列 ——
        附件本体不进 Git,只在 commit message 里留下文件名,改动与记录
        的对应关系由 Git 历史承载(用户确认的语义)。
        """
        values: dict[str, Any] = {
            fields.REQUESTED: False,
            fields.STATUS: SubmissionStatus.REVIEWING.value,
            fields.PR_URL: pull_request_url,
            fields.ERROR_MSG: "",
        }
        if clear_process_record:
            values[fields.PROCESS_RECORD] = []
        self.update_record(record_id, values)

    def mark_approved(self, record_id: str) -> None:
        """审查通过(PR 已合并):状态=已通过。保留 PR URL 作历史。"""
        self._mark_terminal(record_id, SubmissionStatus.APPROVED)

    def mark_rejected(self, record_id: str, close_reason: str = "") -> None:
        """V2-P3:审查未通过(PR 关闭未合并):状态=已拒绝,回写关闭理由。

        保留 PR URL 作历史。close_reason 为空表示
        未取到理由(Git 查询失败或确实无评论),留空由人工补充。
        """
        self._mark_terminal(record_id, SubmissionStatus.REJECTED, close_reason)

    def _mark_terminal(
        self, record_id: str, status: SubmissionStatus, close_reason: str = ""
    ) -> None:
        self.update_record(
            record_id,
            {
                fields.REQUESTED: False,
                fields.STATUS: status.value,
                fields.ERROR_MSG: "",
                fields.CLOSE_REASON: close_reason,
            },
        )

    def mark_failed(self, record_id: str, error_message: str) -> None:
        """永久失败(校验错误/重试耗尽):状态=失败,不再自动重试。"""
        self.update_record(
            record_id,
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.FAILED.value,
                fields.ERROR_MSG: error_message,
            },
        )

    def mark_retryable(self, record_id: str, error_message: str) -> None:
        """瞬时失败:状态保持 处理中、已请求=true,下轮轮询自动重试。"""
        self.update_record(
            record_id,
            {
                fields.REQUESTED: True,
                fields.STATUS: SubmissionStatus.PROCESSING.value,
                fields.ERROR_MSG: error_message,
            },
        )

    # ------------------------------------------------------------------
    # Schema 保障(启动时调用,P3 #15 / V2-P1 / V3-P1)
    # ------------------------------------------------------------------
    def ensure_schema(self) -> None:
        """校验特殊列的存在与类型;自动创建缺失的文本/数字/附件列。

        - 单选/复选框/按钮/人员/日期列(VALIDATE_ONLY)缺失或类型不符
          一律启动失败,错误信息带列名、当前类型、期望类型与修复指引;
        - 「状态」另需校验选项覆盖状态机全集;
        - 缺失的文本/数字/附件列自动创建,不阻塞启动;
        - 表中多出的列一律忽略(不报错、不修改)。
        """
        by_name = {
            f.field_name: f for f in self._list_field_definitions() if f.field_name
        }

        for name, (expected_type, hint) in _VALIDATE_ONLY_FIELDS.items():
            self._ensure_field_type(by_name, name, expected_type, hint)

        self._ensure_status_options(by_name[fields.STATUS])

        for field_name, field_type in AUTO_CREATE_FIELDS.items():
            if field_name in by_name:
                continue
            self._create_text_field(field_name, field_type)

    def _ensure_field_type(
        self,
        by_name: dict[str, Any],
        field_name: str,
        expected_type: int,
        hint: str,
    ) -> None:
        """V3-P1:校验一个「只校验不创建」列的存在与字段类型。

        修复建议说明:飞书不支持直接把已有字段改成另一种类型,
        只能人工删除后按目标类型重建。
        """
        field_def = by_name.get(field_name)
        expected_label = _type_label(expected_type)
        if field_def is None:
            raise PermanentError(
                f"Bitable 表格缺少列「{field_name}」。\n"
                f"请手工创建:{expected_label} 列 —— {hint}"
            )
        actual_type = getattr(field_def, "type", None)
        if actual_type != expected_type:
            raise PermanentError(
                f"Bitable 列「{field_name}」的类型不是{expected_label}:"
                f"当前类型={_type_label(actual_type)},期望类型={expected_label}。\n"
                f"修复建议:飞书不支持直接将现有字段类型转换为目标类型。"
                f"请人工删除/重建「{field_name}」字段({hint})。"
            )

    def _ensure_status_options(self, field_def: Any) -> None:
        """V2-P1:校验「状态」列选项覆盖状态机全集,否则启动失败。"""
        property_def = getattr(field_def, "property", None)
        raw_options = getattr(property_def, "options", None) or []
        options = {
            opt.name
            for opt in raw_options
            if getattr(opt, "name", None)
        }
        missing = [o for o in _STATUS_REQUIRED_OPTIONS if o not in options]
        if missing:
            raise PermanentError(
                f"Bitable 单选列「{fields.STATUS}」缺少状态选项:"
                f"{'、'.join(missing)}。\n"
                f"修复建议:在字段设置中手动添加缺失选项"
                f"(选项应为: " + " / ".join(_STATUS_REQUIRED_OPTIONS) + ")。"
            )

    def _list_field_definitions(self) -> list[AppTableField]:
        fields_out: list[AppTableField] = []
        page_token: str | None = None
        while True:
            builder = (
                ListAppTableFieldRequest.builder()
                .app_token(self.app_token)
                .table_id(self.table_id)
                .page_size(100)
            )
            if page_token:
                builder.page_token(page_token)
            resp = self._call(
                lambda: self.client.bitable.v1.app_table_field.list(
                    builder.build()
                ),
                "list fields",
            )
            fields_out.extend(resp.data.items or [])
            if not resp.data.has_more:
                break
            page_token = resp.data.page_token
        return fields_out

    def _create_text_field(self, field_name: str, field_type: int) -> None:
        # client_token 用于幂等,飞书要求为标准 uuidv4(非 uuidv4 报 1254037)。
        # ensure_schema 先 list 查重、缺列才 create,列已存在不会走到这里,
        # 因此无需固定 token——每次随机生成即可,响应丢失后重试也只会在
        # 下次启动的查重阶段发现列已存在而跳过。
        request = (
            CreateAppTableFieldRequest.builder()
            .app_token(self.app_token)
            .table_id(self.table_id)
            .client_token(str(uuid.uuid4()))
            .request_body(
                AppTableField.builder()
                .field_name(field_name)
                .type(field_type)
                .build()
            )
            .build()
        )
        self._call(
            lambda: self.client.bitable.v1.app_table_field.create(request),
            f"create field {field_name}",
        )
        print(f"[Schema] 已创建 Bitable 列: {field_name}")
