from __future__ import annotations

import uuid
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

from material_worker import fields
from material_worker.domain.status import SubmissionStatus
from material_worker.exceptions import (
    BitableError,
    PermanentError,
    RetryableError,
)

# Feishu Bitable 字段 type:1=文本 2=数字 3=单选 4=多选 7=复选框 17=附件
FIELD_TYPE_TEXT = 1
FIELD_TYPE_NUMBER = 2
FIELD_TYPE_SINGLE_SELECT = 3
FIELD_TYPE_MULTI_SELECT = 4
FIELD_TYPE_CHECKBOX = 7
FIELD_TYPE_ATTACHMENT = 17

# 字段 type -> 中文名(仅用于错误提示)
_FIELD_TYPE_LABELS: dict[int, str] = {
    FIELD_TYPE_TEXT: "文本",
    FIELD_TYPE_NUMBER: "数字",
    FIELD_TYPE_SINGLE_SELECT: "单选",
    FIELD_TYPE_MULTI_SELECT: "多选",
    FIELD_TYPE_CHECKBOX: "复选框",
    FIELD_TYPE_ATTACHMENT: "附件",
}

# worker 可安全自动创建的缺失列:列名 -> (字段类型)
AUTO_CREATE_FIELDS: dict[str, int] = {
    fields.SUBMISSION_ID: FIELD_TYPE_TEXT,
    fields.PR_URL: FIELD_TYPE_TEXT,
    fields.ERROR_MSG: FIELD_TYPE_TEXT,
    fields.RETRY_COUNT: FIELD_TYPE_NUMBER,
}

# 状态(单选)与触发信号(复选框)类型特殊,且选项需人工配置,
# 缺失时启动报错,由用户手工创建。
_MANUAL_FIELDS: dict[str, str] = {
    fields.STATUS: (
        "单选列,选项需包含: "
        + " / ".join(s.value for s in SubmissionStatus)
    ),
    fields.REQUESTED: "复选框列(按钮 -> Automation 置为已勾选)",
}

# V2-P1:单选列「状态」必须具备状态机全集选项
_STATUS_REQUIRED_OPTIONS: list[str] = [s.value for s in SubmissionStatus]


def _make_callable_error(exc: Exception) -> RetryableError:
    return RetryableError(f"Bitable 请求失败(网络/超时): {type(exc).__name__}: {exc}")


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
        """只取 已请求=true 的记录。

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

    def list_reviewing_records(self) -> list[tuple[str, dict[str, Any]]]:
        """取 状态=审核中 的行(审查同步用,本地过滤同 list_pending_records)。"""
        target = SubmissionStatus.REVIEWING.value
        return [
            (record_id, record_fields)
            for record_id, record_fields in self.list_records()
            if str(record_fields.get(fields.STATUS) or "").strip() == target
        ]

    def mark_approved(self, record_id: str) -> None:
        """审查通过(PR 已合并):状态=已通过。保留 提交 ID/PR URL 作历史。"""
        self._mark_terminal(record_id, SubmissionStatus.APPROVED)

    def mark_rejected(self, record_id: str) -> None:
        """审查未通过(PR 关闭未合并):状态=已拒绝。保留 提交 ID/PR URL。"""
        self._mark_terminal(record_id, SubmissionStatus.REJECTED)

    def _mark_terminal(self, record_id: str, status: SubmissionStatus) -> None:
        self.update_record(
            record_id,
            {
                fields.REQUESTED: False,
                fields.STATUS: status.value,
                fields.ERROR_MSG: "",
                fields.RETRY_COUNT: 0,
            },
        )

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

    def mark_processing(
        self,
        record_id: str,
        submission_id: str,
    ) -> None:
        """claim:占用该记录并固定提交 ID(已请求=false,防止重复轮询)。"""
        self.update_record(
            record_id,
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.PROCESSING.value,
                fields.SUBMISSION_ID: submission_id,
            },
        )

    def mark_reviewing(self, record_id: str, pull_request_url: str) -> None:
        """Git 提交成功:状态=审核中,记录 PR URL,清空错误与重试计数。"""
        self.update_record(
            record_id,
            {
                fields.REQUESTED: False,
                fields.STATUS: SubmissionStatus.REVIEWING.value,
                fields.PR_URL: pull_request_url,
                fields.ERROR_MSG: "",
                fields.RETRY_COUNT: 0,
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
                fields.RETRY_COUNT: 0,
            },
        )

    def mark_retryable(
        self,
        record_id: str,
        submission_id: str,
        error_message: str,
        retry_count: int,
    ) -> None:
        """瞬时失败:状态保持 处理中、已请求=true,下轮轮询自动重试。"""
        self.update_record(
            record_id,
            {
                fields.REQUESTED: True,
                fields.STATUS: SubmissionStatus.PROCESSING.value,
                fields.SUBMISSION_ID: submission_id,
                fields.ERROR_MSG: error_message,
                fields.RETRY_COUNT: retry_count,
            },
        )

    # ------------------------------------------------------------------
    # Schema 保障(启动时调用,P3 #15 / V2-P1)
    # ------------------------------------------------------------------
    def ensure_schema(self) -> None:
        """自动创建缺失的文本/数字列;校验 状态 列的存在、类型与选项(V2-P1)。

        - 「状态」必须是单选列且选项覆盖状态机全集;类型/选项不对一律启动失败,
          错误信息带列名、当前类型、期望类型与修复指引;
        - 缺失的文本/数字列自动创建,不阻塞启动。
        """
        by_name = {
            f.field_name: f for f in self._list_field_definitions() if f.field_name
        }

        for manual, hint in _MANUAL_FIELDS.items():
            if manual not in by_name:
                raise PermanentError(
                    f"Bitable 表格缺少列「{manual}」。请手工创建:{hint}"
                )

        self._ensure_status_single_select(by_name[fields.STATUS])

        for field_name, field_type in AUTO_CREATE_FIELDS.items():
            if field_name in by_name:
                continue
            self._create_text_field(field_name, field_type)

    def _ensure_status_single_select(self, field_def: Any) -> None:
        """V2-P1:校验「状态」列为单选且选项覆盖状态机全集,否则启动失败。

        修复建议说明:飞书不支持直接把已有字段改成另一种类型,
        只能人工删除后按单选重建并重新配置选项。
        """
        actual_type = field_def.type
        actual_label = _FIELD_TYPE_LABELS.get(
            actual_type, f"未知(type={actual_type})"
        )
        if actual_type != FIELD_TYPE_SINGLE_SELECT:
            raise PermanentError(
                f"Bitable 列「{fields.STATUS}」的类型不是单选:"
                f"当前类型={actual_label},期望类型=单选。\n"
                f"修复建议:飞书不支持直接将现有字段类型转换为目标类型。"
                f"请人工删除/重建「{fields.STATUS}」字段,"
                f"并重新配置状态选项("
                + " / ".join(_STATUS_REQUIRED_OPTIONS)
                + ")。"
            )

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
        resp = self._call(
            lambda: self.client.bitable.v1.app_table_field.create(request),
            f"create field {field_name}",
        )
        print(f"[Schema] 已创建 Bitable 列: {field_name}")

