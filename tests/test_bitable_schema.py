"""V2-P1:BitableClient.ensure_schema 对「状态」列的类型与选项校验。

用桩 lark client(不触网)驱动真实的 BitableClient.ensure_schema:
- 「状态」缺失/类型不是单选 -> PermanentError,错误信息含修复指引;
- 单选但缺状态机选项 -> PermanentError 明确指出缺失选项;
- 类型/选项正确 -> 正常返回(并可顺带自动补缺失的文本/数字列)。
"""
from types import SimpleNamespace

import pytest

from material_worker import fields
from material_worker.adapters.bitable import (
    AUTO_CREATE_FIELDS,
    BitableClient,
    FIELD_TYPE_ATTACHMENT,
    FIELD_TYPE_CHECKBOX,
    FIELD_TYPE_MULTI_SELECT,
    FIELD_TYPE_NUMBER,
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


def base_fields(status_field):
    """完整表格字段:两个人工列(状态/已请求)+ 四个自动补列。"""
    auto = [make_field(n, t) for n, t in AUTO_CREATE_FIELDS.items()]
    requested = make_field(fields.REQUESTED, FIELD_TYPE_CHECKBOX)
    return [status_field, requested] + auto


def test_status_field_missing_fails_startup():
    """状态 列不存在 -> 启动失败,提示手工创建单选列。"""
    fields_list = [make_field(fields.REQUESTED, FIELD_TYPE_CHECKBOX)]
    client = make_client(fields_list)

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    assert "缺少列" in msg and fields.STATUS in msg
    assert "单选" in msg  # 提示如何手工创建


def test_status_field_as_text_fails_startup():
    """V2-P1:状态 为文本列 -> 启动失败,错误含类型与修复指引。"""
    status_field = make_field(fields.STATUS, FIELD_TYPE_TEXT)
    client = make_client(base_fields(status_field))

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
    client = make_client(base_fields(status_field))

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
    client = make_client(base_fields(status_field))

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    for name in missing:
        assert name in msg  # 明确指出缺失选项
    assert "手动添加" in msg  # 补充选项无需重建列


def test_status_field_complete_passes():
    """V2-P1:单选且选项完整 -> 正常启动,不抛错。"""
    status_field = complete_status_field()
    client = make_client(base_fields(status_field))

    client.ensure_schema()  # 不应抛异常


def test_status_field_with_extra_options_passes():
    """单选选项是状态机全集超集(允许多余选项) -> 正常启动。"""
    status_field = complete_status_field(
        options=[s.value for s in SubmissionStatus] + ["预留选项"]
    )
    client = make_client(base_fields(status_field))

    client.ensure_schema()


def test_single_select_without_property_reports_all_missing():
    """单选但 API 未带回 property(异常响应)-> 视作缺全部选项,启动失败。"""
    status_field = make_field(fields.STATUS, FIELD_TYPE_SINGLE_SELECT)
    client = make_client(base_fields(status_field))

    with pytest.raises(PermanentError) as exc:
        client.ensure_schema()
    msg = str(exc.value)
    for name in (s.value for s in SubmissionStatus):
        assert name in msg


def test_auto_create_missing_text_fields_still_works():
    """类型校验通过后,自动补列机制保持(V2-P1 不回归)。"""
    status_field = complete_status_field()
    requested = make_field(fields.REQUESTED, FIELD_TYPE_CHECKBOX)
    # 只给 已请求/状态,四个自动列都缺
    fields_list = [status_field, requested]
    created: list[str] = []
    client = make_client(fields_list, created)

    client.ensure_schema()

    expected = set(AUTO_CREATE_FIELDS)
    assert expected.issubset(set(created))
    # 全表字段齐全,无任何数字/文本列被漏建


def test_auto_field_type_number_used_for_retry_count():
    """自动列类型与既有约定一致(文本列 1/数字列 2/附件列 17)。

    V2-P2:Profile JSON 为附件列(17);
    V2-P4:耗材参数列(喷嘴温度/线材密度/…/压力提前)与重试计数为数字列。
    """
    assert AUTO_CREATE_FIELDS[fields.RETRY_COUNT] == FIELD_TYPE_NUMBER
    assert AUTO_CREATE_FIELDS[fields.PROFILE_JSON] == FIELD_TYPE_ATTACHMENT
    number_columns = {
        fields.RETRY_COUNT,
        fields.NOZZLE_TEMP,
        fields.MAX_VOL_SPEED,
        fields.FILAMENT_DENSITY,
        fields.VITRIFICATION,
        fields.FAN_COOLING_LAYER_TIME,
        fields.FAN_MAX_SPEED,
        fields.FAN_MIN_SPEED,
        fields.SLOW_DOWN_LAYER_TIME,
        fields.FLOW_RATIO,
        fields.RETRACTION_LENGTH,
        fields.PRESSURE_ADVANCE,
    }
    assert number_columns <= set(AUTO_CREATE_FIELDS)  # 全部在自动建列清单内
    for name, ftype in AUTO_CREATE_FIELDS.items():
        if name in number_columns:
            assert ftype == FIELD_TYPE_NUMBER, name
        elif name == fields.PROFILE_JSON:
            assert ftype == FIELD_TYPE_ATTACHMENT
        else:
            assert ftype == FIELD_TYPE_TEXT, name
