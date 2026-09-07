"""MaterialProfile 测试:V2-P4 canonical schema(构造 / 校验 / 序列化 / 附件解析)。"""
import json

import pytest

from helpers import make_profile, profile_attachment_json, row_fields

from material_worker.domain.profile import (
    ALLOWED_SLICERS,
    REQUIRED_FIELDS,
    MaterialProfile,
    ProfileValidationError,
)


# ----------------------------------------------------------------------
# V2-P4:构造 + 派生视图
# ----------------------------------------------------------------------

def test_create_profile_defaults():
    profile = make_profile()
    profile.validate()

    assert profile.name == "Test PLA"
    assert profile.id is None  # 材料ID 可选,缺省不落


def test_repo_name_is_derived_composite():
    """name 键/文件名主干 = "<品名> @<品牌> <机型>";品名不单独成键(用户确认)。"""
    assert make_profile().repo_name() == "Test PLA @BBL H2C"


def test_repo_relative_path_follows_polymaker_layout():
    """preset/<品名>/<品牌>/<机型>/<切片器>/<复合名>.json(§12 照搬实况)。"""
    assert make_profile().repo_relative_path() == (
        "preset/Test PLA/BBL/H2C/BambuStudio/Test PLA @BBL H2C.json"
    )


def test_repo_relative_path_allows_spaces_and_chinese():
    profile = make_profile(name="PolyTerra PLA", brand="Polymaker", model="Core One")
    path = profile.repo_relative_path()
    assert "preset/PolyTerra PLA/Polymaker/Core One/" in path


def test_repo_relative_path_rejects_bad_segments():
    """未校验对象直接构造时也防御:分隔符/保留字符做仓库目录段 -> 报错。"""
    for key, bad in (("name", "a/b"), ("brand", "a\\b"), ("model", "a:b")):
        with pytest.raises(ProfileValidationError):
            make_profile(**{key: bad}).repo_relative_path()


def test_resolved_id_prefers_material_id():
    assert make_profile().resolved_id == "Test PLA"  # 缺省 = 品名
    assert make_profile(id="mat-42").resolved_id == "mat-42"


# ----------------------------------------------------------------------
# V2-P4:to_dict / to_repo_dict / to_json —— Git 产物是派生视图
# ----------------------------------------------------------------------

def test_to_dict_full_derived_view():
    profile = make_profile(nozzle_temperature=220.0, pressure_advance=0.05)
    data = profile.to_dict()

    assert data["id"] == "Test PLA"
    assert data["name"] == "Test PLA @BBL H2C"
    assert data["brand"] == "BBL"
    assert data["model"] == "H2C"
    assert data["slicer"] == "BambuStudio"
    assert data["filament_settings_id"] == "Test PLA @BBL H2C"  # = name(派生)
    # 10 项必填 A 参数用 BambuStudio 官方 key(§8),标量数值
    assert data["filament_density"] == 1.24
    assert data["temperature_vitrification"] == 59
    assert data["fan_cooling_layer_time"] == 100
    assert data["fan_max_speed"] == 100
    assert data["fan_min_speed"] == 100
    assert data["slow_down_layer_time"] == 8
    assert data["nozzle_temperature"] == 220  # 整数不写 220.0
    assert data["filament_flow_ratio"] == 0.98
    assert data["filament_max_volumetric_speed"] == 16
    assert data["filament_retraction_length"] == 0.4
    # 压力提前:值 + 派生开关(仅第三方机器,此处 brand 需非 BBL)
    assert data["pressure_advance"] == 0.05
    assert data["enable_pressure_advance"] == 1


def test_to_dict_uses_material_id_when_given():
    data = make_profile(id="mat-42").to_dict()
    assert data["id"] == "mat-42"


def test_to_dict_includes_truthy_optional_b_keys_only():
    data = make_profile(
        inherits="PolyTerra PLA @BBL H2C",
        version="01.08.02.50",
        pm_method_version="pm-v2.1",
        pi_code="PT-PLA-001",
    ).to_dict()
    assert data["inherits"] == "PolyTerra PLA @BBL H2C"
    assert data["version"] == "01.08.02.50"
    assert data["pm_method_version"] == "pm-v2.1"
    # V2-P4 seam:pi_code 仅建模存储,不进入 Git JSON(用户确认)
    assert "pi_code" not in data


def test_to_dict_omits_unset_optional_keys():
    data = make_profile().to_dict()
    for key in ("inherits", "version", "pm_method_version",
                "pressure_advance", "enable_pressure_advance"):
        assert key not in data


def test_to_repo_dict_adds_worker_generated_metadata():
    data = make_profile().to_repo_dict(submission_id="sub-abc")
    assert data["submission"] == "sub-abc"  # 与 P1 submission_id 同语义(§15)
    assert "generated_at" in data
    assert "submission_id" not in data  # 旧键不再出现
    assert "updated_at" not in data


def test_to_json_roundtrip():
    text = make_profile().to_json(submission_id="sub-abc")
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert parsed["name"] == "Test PLA @BBL H2C"
    assert parsed["submission"] == "sub-abc"


def test_to_json_without_submission_id_omits_metadata():
    parsed = json.loads(make_profile().to_json())
    assert "submission" not in parsed
    assert "generated_at" not in parsed


# ----------------------------------------------------------------------
# V2-P4:validate(所有输入路径共用:必填 / 目录段 / 切片器白名单 / BBL-PA / 范围)
# ----------------------------------------------------------------------

def test_invalid_temperature_below_range():
    with pytest.raises(ProfileValidationError):
        make_profile(nozzle_temperature=-10).validate()


def test_temperature_upper_bound():
    with pytest.raises(ProfileValidationError):
        make_profile(nozzle_temperature=700).validate()


def test_validate_missing_required():
    """对象直构造缺必填 -> 逐字段报错。"""
    profile = make_profile(name="")
    with pytest.raises(ProfileValidationError) as exc:
        profile.validate()
    assert "品名" in str(exc.value)


def test_validate_rejects_path_chars_in_name():
    with pytest.raises(ProfileValidationError) as exc:
        make_profile(name="PLA/../../evil").validate()
    assert "品名" in str(exc.value)


@pytest.mark.parametrize("slicer", [
    s for s in ("PrusaSlicer", "Cura", "", "BambuStudio ") if s != "BambuStudio"
])
def test_validate_rejects_unsupported_slicer(slicer):
    with pytest.raises(ProfileValidationError) as exc:
        make_profile(slicer=slicer).validate()
    message = str(exc.value)
    assert "切片器" in message
    if slicer == "PrusaSlicer":
        assert ".ini" in message  # 明确提示暂不支持 PrusaSlicer


def test_validate_accepts_all_allowed_slicers():
    for slicer in ALLOWED_SLICERS:
        make_profile(slicer=slicer).validate()


def test_validate_bbl_pressure_advance_rejected():
    with pytest.raises(ProfileValidationError) as exc:
        make_profile(brand="BBL", pressure_advance=0.05).validate()
    assert "官方机型" in str(exc.value)


def test_validate_third_party_pressure_advance_accepted():
    make_profile(brand="Elegoo", pressure_advance=0.05).validate()


def test_validate_numeric_bounds():
    """安全护栏:明显单位/小数位笔误逐字段报错(宽松,不误伤真实值)。"""
    cases = (
        dict(filament_density=0),
        dict(filament_density=99),
        dict(temperature_vitrification=900),
        dict(fan_cooling_layer_time=-1),
        dict(fan_max_speed=101),
        dict(fan_min_speed=-5),
        dict(slow_down_layer_time=9999),
        dict(filament_flow_ratio=0),
        dict(filament_flow_ratio=9),
        dict(filament_max_volumetric_speed=-2),
        dict(filament_max_volumetric_speed=500),
        dict(filament_retraction_length=-1),
        dict(filament_retraction_length=999),
        dict(pressure_advance=-1),
        dict(pressure_advance=7),
    )
    for overrides in cases:
        with pytest.raises(ProfileValidationError):
            make_profile(brand="Elegoo", **overrides).validate()


def test_validate_full_valid_profile_passes():
    make_profile().validate()


# ----------------------------------------------------------------------
# V2-P4:from_bitable_record(中文列名,B 列/数字字符串均可用)
# ----------------------------------------------------------------------

def test_from_bitable_record_full_row():
    profile = MaterialProfile.from_bitable_record(
        record_id="rec123",
        record_fields=row_fields(),
    )
    assert profile.name == "Test PLA"
    assert profile.brand == "BBL"
    assert profile.model == "H2C"
    assert profile.slicer == "BambuStudio"
    assert profile.nozzle_temperature == 220
    assert profile.source_record_id == "rec123"
    assert profile.submitted_at is not None


def test_from_bitable_record_accepts_numeric_strings():
    fields = row_fields(喷嘴温度="220", 流量比例="0.98")
    profile = MaterialProfile.from_bitable_record("rec123", fields)
    assert profile.nozzle_temperature == 220.0
    assert profile.filament_flow_ratio == 0.98


def test_from_bitable_record_missing_field_reports_all_missing():
    """缺必填 -> 一次性列出全部缺失列(逐字段报错),不逐个试错。"""
    fields = row_fields()
    fields.pop("品牌")
    fields.pop("线材密度")
    fields.pop("最大体积流速")
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.from_bitable_record("rec123", fields)
    message = str(exc.value)
    assert message.startswith("缺少必填字段: ")
    assert "品牌" in message and "线材密度" in message and "最大体积流速" in message
    assert "Profile JSON" in message  # 附导入指引


def test_from_bitable_record_invalid_number():
    fields = row_fields(喷嘴温度="hot")
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.from_bitable_record("rec123", fields)
    assert "喷嘴温度" in str(exc.value)


def test_from_bitable_record_uses_material_id_and_optional_columns():
    fields = row_fields(
        材料ID="mat-42",
        继承预设="PolyTerra PLA @BBL H2C",
        **{"PI Code": "PT-PLA-001"},
    )
    profile = MaterialProfile.from_bitable_record("rec123", fields)
    assert profile.id == "mat-42"
    assert profile.inherits == "PolyTerra PLA @BBL H2C"
    assert profile.pi_code == "PT-PLA-001"


def test_from_bitable_record_blank_optionals_stay_none():
    profile = MaterialProfile.from_bitable_record("rec123", row_fields())
    assert profile.pressure_advance is None
    assert profile.pi_code is None
    assert profile.id is None


# ----------------------------------------------------------------------
# V2-P4:parse_attachment_json(英文 key,与表格同一 schema,严格不许猜)
# ----------------------------------------------------------------------

def test_parse_attachment_json_full():
    profile = MaterialProfile.parse_attachment_json(profile_attachment_json())
    assert profile.name == "Test PLA"
    assert profile.brand == "BBL"
    assert profile.nozzle_temperature == 220
    assert profile.filament_flow_ratio == 0.98
    assert profile.pressure_advance is None


def test_parse_attachment_json_optional_values():
    profile = MaterialProfile.parse_attachment_json(
        profile_attachment_json(
            pressure_advance=0.05, pi_code="PT-PLA-001",
            inherits="PolyTerra PLA @BBL H2C",
        )
    )
    assert profile.pressure_advance == 0.05
    assert profile.pi_code == "PT-PLA-001"
    assert profile.inherits == "PolyTerra PLA @BBL H2C"


def test_parse_attachment_json_id_defaults_to_name():
    """附件没带 id 时与表格语义一致:缺省用品名(材料ID 不另落列)。"""
    profile = MaterialProfile.parse_attachment_json(profile_attachment_json())
    assert profile.id is None
    assert profile.resolved_id == "Test PLA"


def test_parse_attachment_json_accepts_numeric_strings():
    text = profile_attachment_json(nozzle_temperature="220")
    profile = MaterialProfile.parse_attachment_json(text)
    assert profile.nozzle_temperature == 220


def test_parse_attachment_json_trims_text_whitespace():
    text = profile_attachment_json(name="  Test PLA  ")
    profile = MaterialProfile.parse_attachment_json(text)
    assert profile.name == "Test PLA"


def test_parse_attachment_json_rejects_unknown_key():
    text = profile_attachment_json(cooling=5)
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json(text)
    message = str(exc.value)
    assert "cooling" in message
    assert "未建模键" in message


def test_parse_attachment_json_rejects_reserved_worker_keys():
    """worker 生成/派生键(submission/generated_at/filament_settings_id/
    enable_pressure_advance)附件不得携带覆盖。"""
    for reserved in ("submission", "generated_at", "filament_settings_id",
                     "enable_pressure_advance"):
        text = profile_attachment_json(**{reserved: "fake"})
        with pytest.raises(ProfileValidationError) as exc:
            MaterialProfile.parse_attachment_json(text)
        message = str(exc.value)
        assert reserved in message
        assert "保留键" in message


def test_parse_attachment_json_reports_all_missing_fields():
    text = '{"name": "Test PLA", "brand": "BBL"}'
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json(text)
    message = str(exc.value)
    assert "缺少必填字段" in message
    assert "nozzle_temperature(喷嘴温度)" in message  # key(中文列) 形态
    assert "slicer(切片器)" in message


def test_parse_attachment_json_rejects_empty_and_non_object():
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json("")
    assert "为空" in str(exc.value)
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json("[]")
    assert "顶层必须是对象" in str(exc.value)


def test_parse_attachment_json_rejects_malformed_json():
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json("{not json")
    assert "不是合法 JSON" in str(exc.value)


def test_parse_attachment_json_rejects_bad_number():
    text = profile_attachment_json(nozzle_temperature="hot")
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json(text)
    message = str(exc.value)
    assert "nozzle_temperature(喷嘴温度)" in message
    assert "不是有效数字" in message


def test_parse_attachment_json_numeric_blank_treated_as_unset():
    """可选数字键显式给空 -> None(validate 放行,不落 JSON)。"""
    text = profile_attachment_json(pressure_advance=None)
    profile = MaterialProfile.parse_attachment_json(text)
    assert profile.pressure_advance is None


# ----------------------------------------------------------------------
# 结构一致性:REQUIRED_FIELDS 必须 = 14 项(4 结构 + 10 耗材参数)
# ----------------------------------------------------------------------

def test_required_fields_are_14():
    assert len(REQUIRED_FIELDS) == 14
    keys = [f.key for f in REQUIRED_FIELDS]
    for key in ("name", "brand", "model", "slicer",
                "filament_density", "temperature_vitrification",
                "fan_cooling_layer_time", "fan_max_speed", "fan_min_speed",
                "slow_down_layer_time", "nozzle_temperature",
                "filament_flow_ratio", "filament_max_volumetric_speed",
                "filament_retraction_length"):
        assert key in keys
