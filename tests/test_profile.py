"""MaterialProfile:身份派生、路径布局、必填/可选、输出形态、数值护栏。"""
import json

import pytest

from helpers import make_profile, row_fields

from material_worker import fields
from material_worker.domain.profile import (
    FIELD_SCHEMA,
    REQUIRED_FIELDS,
    MaterialProfile,
    ProfileValidationError,
    coerce_attachment_number,
    describe_changes,
    parse_profile_json,
)


# ----------------------------------------------------------------------
# V3-P2:必填 / 可选
# ----------------------------------------------------------------------
def test_required_columns_match_v3_decision():
    """必填 = 身份三要素 + 调参方法版本 + 继承预设 + 8 项核心参数。"""
    assert [f.column for f in REQUIRED_FIELDS] == [
        fields.PI_CODE,
        fields.PRINTER_MODEL,
        fields.SLICER,
        fields.PM_METHOD_VERSION,
        fields.INHERITS,
        fields.BED_TEMP,
        fields.NOZZLE_TEMP,
        fields.FAN_COOLING_LAYER_TIME,
        fields.FAN_MAX_SPEED,
        fields.FAN_MIN_SPEED,
        fields.SLOW_DOWN_LAYER_TIME,
        fields.FLOW_RATIO,
        fields.MAX_VOL_SPEED,
    ]


def test_optional_columns_are_the_four():
    optional = [f.column for f in FIELD_SCHEMA if not f.required]
    assert optional == [
        fields.FILAMENT_DENSITY,
        fields.VITRIFICATION,
        fields.RETRACTION_LENGTH,
        fields.PRESSURE_ADVANCE,
    ]


def test_single_select_columns_are_not_backfillable():
    """V3-P3:单选列必须人工在下拉中选择,附件一律不反写。"""
    not_backfillable = {f.column for f in FIELD_SCHEMA if not f.backfill}
    assert not_backfillable == {
        fields.PI_CODE,
        fields.PRINTER_MODEL,
        fields.SLICER,
        fields.PM_METHOD_VERSION,
    }


def test_missing_required_lists_all_columns():
    data = row_fields()
    for column in (fields.INHERITS, fields.NOZZLE_TEMP, fields.PI_CODE):
        data.pop(column)
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.from_bitable_record("rec1", data)
    message = str(exc.value)
    for column in (fields.INHERITS, fields.NOZZLE_TEMP, fields.PI_CODE):
        assert column in message


def test_inherits_is_now_required():
    """V3:继承预设为空 -> 报错、不生成 PR。"""
    data = row_fields(**{fields.INHERITS: ""})
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.from_bitable_record("rec1", data)
    assert fields.INHERITS in str(exc.value)


def test_optional_blank_is_allowed():
    profile = MaterialProfile.from_bitable_record("rec1", row_fields())
    profile.validate()
    assert profile.filament_density is None
    assert profile.pressure_advance is None


# ----------------------------------------------------------------------
# V3-P4:身份与路径
# ----------------------------------------------------------------------
def test_identity_and_path():
    profile = make_profile()
    assert profile.identity == "L1002@BBL P2S"
    assert profile.brand == "BBL"
    assert profile.model == "P2S"
    assert profile.repo_relative_path() == (
        "preset/L1002/BBL/P2S/BambuStudio/L1002@BBL P2S.json"
    )


def test_model_with_space_keeps_rest_intact():
    """`Creality K2 Pro` -> 品牌 Creality / 机型 `K2 Pro`(只切第一个空格)。"""
    profile = make_profile(printer_model="Creality K2 Pro")
    assert profile.brand == "Creality"
    assert profile.model == "K2 Pro"
    assert profile.identity == "L1002@Creality K2 Pro"
    assert profile.repo_relative_path() == (
        "preset/L1002/Creality/K2 Pro/BambuStudio/L1002@Creality K2 Pro.json"
    )


def test_slicer_is_part_of_identity_path():
    """同一身份不同切片软件 -> 两个文件、互不覆盖。"""
    bambu = make_profile()
    prusa = make_profile(slicer="PrusaSlicer")
    assert bambu.repo_relative_path() != prusa.repo_relative_path()
    assert prusa.repo_relative_path() == (
        "preset/L1002/BBL/P2S/PrusaSlicer/L1002@BBL P2S.json"
    )
    # 所有切片器统一 .json(V3:worker 只产基础数据)
    assert prusa.repo_relative_path().endswith(".json")


def test_printer_model_without_space_is_rejected():
    profile = make_profile(printer_model="P2S")
    with pytest.raises(ProfileValidationError) as exc:
        profile.validate()
    assert fields.PRINTER_MODEL in str(exc.value)


def test_path_separator_in_segment_is_rejected():
    profile = make_profile(pi_code="L/1002")
    with pytest.raises(ProfileValidationError):
        profile.validate()


# ----------------------------------------------------------------------
# V3-P4:输出形态
# ----------------------------------------------------------------------
def test_to_dict_shape():
    profile = make_profile(
        filament_density=1.17,
        temperature_vitrification=60,
        filament_retraction_length=0.4,
    )
    data = profile.to_dict()

    assert data["name"] == "L1002@BBL P2S"
    assert data["pi_code"] == "L1002"
    assert data["printer"] == "BBL P2S"
    assert data["slicer"] == "BambuStudio"
    assert data["inherits"] == "Panchroma PLA"
    assert data["pm_method_version"] == "v1"
    assert data["nozzle_temperature"] == 220
    assert data["textured_plate_temp"] == 55

    # V3:不写 id / filament_settings_id / 伴随键
    assert "id" not in data
    assert "filament_settings_id" not in data
    assert not [k for k in data if k.endswith("_initial_layer")]
    assert not [k for k in data if k.startswith("first_layer")]
    # 全部为标量(数组化留给构建脚本)
    assert all(not isinstance(v, list) for v in data.values())


def test_to_dict_omits_blank_optionals():
    data = make_profile().to_dict()
    for key in (
        "filament_density",
        "temperature_vitrification",
        "filament_retraction_length",
        "pressure_advance",
        "enable_pressure_advance",
    ):
        assert key not in data


def test_integers_are_not_written_as_floats():
    text = make_profile().to_json()
    assert "220.0" not in text
    assert '"nozzle_temperature": 220' in text


def test_pressure_advance_ignored_on_bbl():
    """V3:BBL 官方机型忽略压力提前 —— 不报错,也不输出该键。"""
    profile = make_profile(pressure_advance=0.02)
    profile.validate()  # 不报错
    data = profile.to_dict()
    assert "pressure_advance" not in data
    assert "enable_pressure_advance" not in data


def test_pressure_advance_written_on_third_party():
    profile = make_profile(
        printer_model="Prusa Core One", pressure_advance=0.02
    )
    profile.validate()
    data = profile.to_dict()
    assert data["pressure_advance"] == 0.02
    assert data["enable_pressure_advance"] == 1


def test_file_carries_no_provenance_keys():
    """文件里只有材料数据 —— submission/generated_at 已按用户确认删除。"""
    profile = make_profile()
    data = profile.to_dict()
    for key in ("submission", "submission_id", "generated_at", "submitted_at"):
        assert key not in data
    assert profile.to_json() == json.dumps(
        data, ensure_ascii=False, indent=2
    ) + "\n"


def test_to_json_is_stable_and_ends_with_newline():
    text = make_profile().to_json()
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert list(parsed)[:6] == [
        "name",
        "pi_code",
        "printer",
        "slicer",
        "inherits",
        "pm_method_version",
    ]


# ----------------------------------------------------------------------
# 差异描述(PR 正文的主体):把两版基础数据讲成人话
# ----------------------------------------------------------------------
def test_describe_changes_first_submission_lists_values():
    lines = describe_changes(None, make_profile().to_dict())
    assert f"{fields.INHERITS}: Panchroma PLA" in lines
    assert f"{fields.NOZZLE_TEMP}: 220 °C" in lines
    assert f"{fields.MAX_VOL_SPEED}: 18 mm³/s" in lines
    # 首次提交没有旧值可言 -> 不出现箭头
    assert not [line for line in lines if "→" in line]


def test_describe_changes_order_and_labels_follow_the_table():
    before = make_profile().to_dict()
    after = make_profile(
        inherits="Panchroma PLA HF", filament_flow_ratio=0.92
    ).to_dict()
    assert describe_changes(before, after) == [
        f"{fields.INHERITS}: Panchroma PLA → Panchroma PLA HF",
        f"{fields.FLOW_RATIO}: 0.95 → 0.92",
    ]


def test_describe_changes_renders_blank_as_unset():
    """可选键从无到有 -> (未设置) → 值;派生键 enable_pressure_advance 不列。"""
    before = make_profile().to_dict()  # BBL 忽略压力提前 -> 无该键
    after = make_profile(
        printer_model="Prusa Core One", pressure_advance=0.02
    ).to_dict()
    assert describe_changes(before, after) == [
        f"{fields.PRESSURE_ADVANCE}: (未设置) → 0.02"
    ]


def test_describe_changes_marks_dropped_key_as_unset():
    before = make_profile(filament_density=1.17).to_dict()
    assert describe_changes(before, make_profile().to_dict()) == [
        f"{fields.FILAMENT_DENSITY}: 1.17 g/cm³ → (未设置)"
    ]


def test_describe_changes_skips_identity_and_unchanged_keys():
    """身份四要素在每个路径下恒定(正文首行已交代),不重复列出。"""
    profile = make_profile(pressure_advance=0.02)  # BBL:该键被忽略
    assert describe_changes(profile.to_dict(), profile.to_dict()) == []


def test_describe_changes_never_leaks_legacy_provenance_keys():
    """仓库里遗留的旧文件带 submission/generated_at —— 它们的消失也不该
    借差异描述把内部 uuid 漏回 PR 正文。"""
    legacy = make_profile().to_dict()
    legacy["submission"] = "032d5ac951eb4501b631cd920c2fe6c5"
    legacy["generated_at"] = "2026-09-11T10:23:00+08:00"
    assert describe_changes(legacy, make_profile().to_dict()) == []


def test_describe_changes_survives_hand_edited_value():
    """仓库文件被手改成非数字时,正文照常生成(只影响这一行的写法)。"""
    before = make_profile().to_dict()
    before["nozzle_temperature"] = "很高"
    assert describe_changes(before, make_profile().to_dict()) == [
        f"{fields.NOZZLE_TEMP}: 很高 → 220 °C"
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        (None, None),
        ("", None),
        ("不是 JSON", None),
        ("[1, 2]", None),          # 合法 JSON 但不是对象
        ('{"a": 1}', {"a": 1}),
    ],
)
def test_parse_profile_json_tolerates_garbage(text, expected):
    assert parse_profile_json(text) == expected


# ----------------------------------------------------------------------
# 数值护栏
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "key,value,column",
    [
        ("nozzle_temperature", 900, fields.NOZZLE_TEMP),
        ("fan_max_speed", 120, fields.FAN_MAX_SPEED),
        ("filament_flow_ratio", 0, fields.FLOW_RATIO),
        ("textured_plate_temp", -5, fields.BED_TEMP),
    ],
)
def test_numeric_bounds(key, value, column):
    profile = make_profile(**{key: value})
    with pytest.raises(ProfileValidationError) as exc:
        profile.validate()
    assert column in str(exc.value)


def test_bounds_skip_blank_optionals():
    make_profile(filament_density=None, pressure_advance=None).validate()


def test_bitable_numeric_strings_are_coerced():
    data = row_fields(**{fields.NOZZLE_TEMP: "220", fields.FLOW_RATIO: "0.95"})
    profile = MaterialProfile.from_bitable_record("rec1", data)
    assert profile.nozzle_temperature == 220.0
    assert profile.filament_flow_ratio == 0.95


def test_bitable_malformed_number_raises():
    data = row_fields(**{fields.NOZZLE_TEMP: "很高"})
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.from_bitable_record("rec1", data)
    assert fields.NOZZLE_TEMP in str(exc.value)


# ----------------------------------------------------------------------
# V3-P3:附件数值取值 —— 数组先剔 nil 槽位,再要求剩余值一致
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        # 实测 nozzle_temperature 常是 ["300", "nil"]:未启用的喷头槽位写 nil,
        # 剔除后剩单个值 -> 可唯一确定
        (["300", "nil"], 300),
        (["nil", "300"], 300),
        (["300", None], 300),      # JSON 真 null
        (["300", ""], 300),        # 空字符串槽位
        ([300, "nil"], 300),       # 数字与字符串混排
        (["300", "nil", "300"], 300),
        # 无 nil 的既有形态
        (["220"], 220),
        (["220", "220"], 220),
        ([0, "0"], 0),
        # 标量形态
        ("220", 220),
        (220, 220),
        (0.95, 0.95),
        # —— 无法唯一确定 -> None(调用方跳过该键,绝不静默挑一个喷头)——
        (["220", "240"], None),          # 多喷头真的调得不同
        (["220", "nil", "240"], None),   # 剔除 nil 后仍不一致
        (["nil", "nil"], None),          # 全是空槽位
        (["nil"], None),
        ([], None),
        ("nil", None),
        (None, None),
        ("很高", None),
        (["220", "很高"], None),
    ],
)
def test_coerce_attachment_number_slots(raw, expected):
    assert coerce_attachment_number(fields.NOZZLE_TEMP, raw) == expected
