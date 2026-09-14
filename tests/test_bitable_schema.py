"""V3-P1:BitableClient.ensure_schema 的列形态保障。

用桩 lark client(不触网)驱动真实的 BitableClient.ensure_schema:
- 单选/复选框/按钮/人员/日期列(人工维护、带选项或依赖 Automation)
  **只校验不创建**:缺失或类型不符 -> PermanentError,信息含当前类型、
  期望类型与修复指引;
- 「状态」另需校验选项覆盖状态机全集;
- 文本/数字/附件列缺失时自动补齐;
- V3:表里不再有「提交 ID」「重试次数」列。
"""
from types import SimpleNamespace

import pytest

from material_worker import fields
from material_worker.adapters.bitable import (
    AUTO_CREATE_FIELDS,
    _VALIDATE_ONLY_FIELDS,
    BitableClient,
    FIELD_TYPE_ATTACHMENT,
    FIELD_TYPE_BUTTON,
    FIELD_TYPE_CHECKBOX,
    FIELD_TYPE_DATE,
    FIELD_TYPE_MULTI_SELECT,
    FIELD_TYPE_NUMBER,
    FIELD_TYPE_PERSON,
    FIELD_TYPE_SINGLE_SELECT,
    FIELD_TYPE_TEXT,
)
from material_worker.domain.status import SubmissionStatus
from material_worker.exceptions import PermanentError


class FakeFieldListResponse:
    """模拟 ListAppTableField 响应:单页返回全部字段。"""

    def __init__(self, field_defs):
        self._field_defs = field_defs
        self.code = 0
        self.msg = "success"

    def success(self):
        return True

    @property
    def data(self):
        return SimpleNamespace(
            items=self._field_defs, has_more=False, page_token=None
        )


class FakeCreateFieldResponse:
    """模拟 CreateAppTableField 响应。"""

    def __init__(self):
        self.code = 0
        self.msg = "success"

    def success(self):
        return True

    @property
    def data(self):
        return SimpleNamespace(field=None)


def make_field(name, ftype, options=None):
    if options is None:
        prop = None
    else:
        prop = SimpleNamespace(
            options=[SimpleNamespace(name=o, id=f"opt-{i}", color=0)
                     for i, o in enumerate(options)]
        )
    return SimpleNamespace(
        field_name=name, field_id=f"fld-{name}", type=ftype, property=prop
    )


def complete_status_field(options=None):
    if options is None:
        options = [s.value for s in SubmissionStatus]
    return make_field(fields.STATUS, FIELD_TYPE_SINGLE_SELECT, options)


def make_client(field_defs, created=None):
    """构造带桩 app_table_field.list / create 的 BitableClient。"""

    class FakeClient:
        def __init__(self):
            self.bitable = SimpleNamespace(
                v1=SimpleNamespace(
                    app_table_field=SimpleNamespace(
                        list=lambda req: FakeFieldListResponse(field_defs),
                        create=lambda req: (
                            created.append(req.request_body.field_name)
                            if created is not None else None,
                            FakeCreateFieldResponse(),
                        )[1],
                    )
                )
            )

    return BitableClient(client=FakeClient(), app_token="t", table_id="tb")


def validate_only_fields(**overrides):
    """全部「只校验不创建」列的正确形态(可按需覆盖某一列)。"""
    defs = []
    for name, (ftype, _hint) in _VALIDATE_ONLY_FIELDS.items():
        if name in overrides:
            defs.append(overrides[name])
            continue
        options = (
            [s.value for s in SubmissionStatus]
            if name == fields.STATUS
            else None
        )
        defs.append(make_field(name, ftype, options))
    return defs


def base_fields(**overrides):
    """完整表格字段:只校验列 + 自动补列。"""
    auto = [make_field(n, t) for n, t in AUTO_CREATE_FIELDS.items()]
    return validate_only_fields(**overrides) + auto


# ----------------------------------------------------------------------
# 只校验不创建
# ----------------------------------------------------------------------
def test_missing_button_column_fails_startup():
    """V3:提交审核 按钮列不存在 -> 启动失败(按钮要挂 Automation)。"""
    defs = [d for d in base_fields() if d.field_name != fields.SUBMIT_BUTTON]
    client = make_client(defs)

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    assert "缺少列" in msg and fields.SUBMIT_BUTTON in msg
    assert "按钮" in msg


@pytest.mark.parametrize(
    "column,label",
    [
        (fields.PI_CODE, "单选"),
        (fields.PRINTER_MODEL, "单选"),
        (fields.SLICER, "单选"),
        (fields.PM_METHOD_VERSION, "单选"),
        (fields.SUBMITTER, "人员"),
        (fields.SUBMIT_TIME, "日期"),
        (fields.REQUESTED, "复选框"),
    ],
)
def test_validate_only_columns_are_not_auto_created(column, label):
    """这些列缺失时必须报错,且提示期望类型 —— 绝不静默建空壳。"""
    defs = [d for d in base_fields() if d.field_name != column]
    client = make_client(defs)

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    assert column in msg
    assert label in msg


def test_single_select_wrong_type_reports_current_and_expected():
    """V3:PI Code 是文本列 -> 启动失败,含当前类型/期望类型/修复指引。"""
    pi_code = make_field(fields.PI_CODE, FIELD_TYPE_TEXT)
    client = make_client(base_fields(**{fields.PI_CODE: pi_code}))

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    assert fields.PI_CODE in msg
    assert "文本" in msg and "单选" in msg
    assert "删除/重建" in msg


def test_submitter_as_text_fails_startup():
    """"提交人"必须是人员列(Automation 写入的是人,不是字符串)。"""
    submitter = make_field(fields.SUBMITTER, FIELD_TYPE_TEXT)
    client = make_client(base_fields(**{fields.SUBMITTER: submitter}))

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    assert fields.SUBMITTER in msg
    assert "人员" in msg


def test_status_field_as_text_fails_startup():
    """V2-P1:状态 为文本列 -> 启动失败,错误含类型与修复指引。"""
    status_field = make_field(fields.STATUS, FIELD_TYPE_TEXT)
    client = make_client(base_fields(**{fields.STATUS: status_field}))

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    assert fields.STATUS in msg
    assert "文本" in msg and "单选" in msg  # 当前类型 / 期望类型
    assert "删除/重建" in msg  # 修复指引


def test_status_field_as_multi_select_fails_startup():
    """V2-P1:状态 为多选列 -> 启动失败(含当前类型)。"""
    status_field = make_field(
        fields.STATUS, FIELD_TYPE_MULTI_SELECT, options=["待处理", "审核中"]
    )
    client = make_client(base_fields(**{fields.STATUS: status_field}))

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    assert fields.STATUS in msg
    assert "多选" in msg and "单选" in msg
    assert "删除/重建" in msg


def test_status_field_missing_options_fails_startup():
    """V2-P1:单选列但缺少状态机选项 -> 启动失败并列出缺失项。"""
    missing = [SubmissionStatus.APPROVED.value, SubmissionStatus.REJECTED.value]
    present = [s.value for s in SubmissionStatus if s.value not in missing]
    status_field = make_field(fields.STATUS, FIELD_TYPE_SINGLE_SELECT, present)
    client = make_client(base_fields(**{fields.STATUS: status_field}))

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    for name in missing:
        assert name in msg  # 明确指出缺失选项
    assert "手动添加" in msg  # 补充选项无需重建列


def test_status_field_complete_passes():
    """V2-P1:单选且选项完整 -> 正常启动,不抛错。"""
    client = make_client(base_fields())

    client.ensure_schema()  # 不应抛异常


def test_status_field_with_extra_options_passes():
    """单选选项是状态机全集超集(允许多余选项) -> 正常启动。"""
    status_field = complete_status_field(
        options=[s.value for s in SubmissionStatus] + ["预留选项"]
    )
    client = make_client(base_fields(**{fields.STATUS: status_field}))

    client.ensure_schema()


def test_single_select_without_property_reports_all_missing():
    """单选但 API 未带回 property(异常响应)-> 视作缺全部选项,启动失败。"""
    status_field = make_field(fields.STATUS, FIELD_TYPE_SINGLE_SELECT)
    client = make_client(base_fields(**{fields.STATUS: status_field}))

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    for name in (s.value for s in SubmissionStatus):
        assert name in msg


def test_first_failure_mentions_only_that_column():
    """多列同时缺失时快速失败(报第一处),信息足够定位即可。"""
    defs = [
        d
        for d in base_fields()
        if d.field_name not in (fields.SUBMIT_BUTTON, fields.PI_CODE)
    ]
    client = make_client(defs)

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    assert "缺少列" in str(exc.value)


# ----------------------------------------------------------------------
# 自动补列
# ----------------------------------------------------------------------
def test_auto_create_missing_text_fields_still_works():
    """类型校验通过后,自动补列机制保持(V2-P1 不回归)。"""
    requested = make_field(fields.REQUESTED, FIELD_TYPE_CHECKBOX)
    # 只给只校验列,自动列全缺
    fields_list = validate_only_fields()
    created: list[str] = []
    client = make_client(fields_list, created)

    client.ensure_schema()

    assert set(created) == set(AUTO_CREATE_FIELDS)
    assert requested.field_name in {
        d.field_name for d in fields_list
    }  # 只校验列不会被创建


def test_existing_auto_columns_are_not_recreated():
    created: list[str] = []
    client = make_client(base_fields(), created)

    client.ensure_schema()

    assert created == []


def test_auto_field_types_match_their_content():
    """自动列类型:附件列(17)/数字列(2)/其余文本(1)。"""
    assert AUTO_CREATE_FIELDS[fields.PROFILE_JSON] == FIELD_TYPE_ATTACHMENT
    assert AUTO_CREATE_FIELDS[fields.PROCESS_RECORD] == FIELD_TYPE_ATTACHMENT
    assert AUTO_CREATE_FIELDS[fields.INHERITS] == FIELD_TYPE_TEXT

    number_columns = {
        fields.BED_TEMP,
        fields.NOZZLE_TEMP,
        fields.FAN_COOLING_LAYER_TIME,
        fields.FAN_MAX_SPEED,
        fields.FAN_MIN_SPEED,
        fields.SLOW_DOWN_LAYER_TIME,
        fields.FLOW_RATIO,
        fields.MAX_VOL_SPEED,
        fields.RETRACTION_LENGTH,
        fields.PRESSURE_ADVANCE,
        fields.FILAMENT_DENSITY,
        fields.VITRIFICATION,
    }
    assert number_columns <= set(AUTO_CREATE_FIELDS)  # 全部在自动建列清单内
    for name, ftype in AUTO_CREATE_FIELDS.items():
        if name in number_columns:
            assert ftype == FIELD_TYPE_NUMBER, name
        elif name in (fields.PROFILE_JSON, fields.PROCESS_RECORD):
            assert ftype == FIELD_TYPE_ATTACHMENT, name
        else:
            assert ftype == FIELD_TYPE_TEXT, name


def test_removed_columns_are_gone_from_schema():
    """V3:提交 ID / 重试次数 两列已废弃,worker 不再建也不再读。"""
    assert not hasattr(fields, "SUBMISSION_ID")
    assert not hasattr(fields, "RETRY_COUNT")
    assert not hasattr(fields, "ALLOWED_SLICERS")
    for column in ("提交 ID", "重试次数"):
        assert column not in AUTO_CREATE_FIELDS
        assert column not in _VALIDATE_ONLY_FIELDS


def test_unknown_extra_columns_are_ignored():
    """表里多出来的列一律忽略(不报错、不修改)。"""
    extra = make_field("备注", FIELD_TYPE_TEXT)
    client = make_client(base_fields() + [extra, make_field("序号", FIELD_TYPE_NUMBER)])

    client.ensure_schema()


def test_button_type_constant_is_3001():
    assert FIELD_TYPE_BUTTON == 3001
