import pytest

from material_worker.domain.profile import (
    MaterialProfile,
    ProfileValidationError,
)


def test_create_profile():
    profile = MaterialProfile(
        id="test-pla",
        name="Test PLA",
        nozzle_temperature=220,
        max_volumetric_speed=20,
    )

    profile.validate()

    assert profile.id == "test-pla"


def test_profile_to_dict():
    profile = MaterialProfile(
        id="test-pla",
        name="Test PLA",
        nozzle_temperature=220,
        max_volumetric_speed=20,
    )

    data = profile.to_dict()

    assert data["id"] == "test-pla"
    assert data["name"] == "Test PLA"


def test_invalid_temperature():
    profile = MaterialProfile(
        id="test-pla",
        name="Test PLA",
        nozzle_temperature=-10,
        max_volumetric_speed=20,
    )

    with pytest.raises(ProfileValidationError):
        profile.validate()


def test_from_bitable_record():
    profile = MaterialProfile.from_bitable_record(
        record_id="rec123",
        fields={
            "品名": "Test PLA",
            "喷嘴温度": 220,
            "最大体积流速": 20,
        },
    )

    assert profile.name == "Test PLA"
    assert profile.source_record_id == "rec123"


def test_from_bitable_record_missing_field():
    with pytest.raises(ProfileValidationError):
        MaterialProfile.from_bitable_record(
            record_id="rec123",
            fields={"品名": "Test PLA", "喷嘴温度": 220},  # 缺 最大体积流速
        )


def test_from_bitable_record_invalid_number():
    with pytest.raises(ProfileValidationError):
        MaterialProfile.from_bitable_record(
            record_id="rec123",
            fields={
                "品名": "Test PLA",
                "喷嘴温度": "hot",
                "最大体积流速": 20,
            },
        )


def test_from_bitable_record_uses_material_id():
    profile = MaterialProfile.from_bitable_record(
        record_id="rec123",
        fields={
            "品名": "Test PLA",
            "材料ID": "mat-42",
            "喷嘴温度": 220,
            "最大体积流速": 20,
        },
    )

    assert profile.id == "mat-42"


def test_temperature_upper_bound():
    profile = MaterialProfile(
        id="t",
        name="Test PLA",
        nozzle_temperature=700,
        max_volumetric_speed=20,
    )

    with pytest.raises(ProfileValidationError):
        profile.validate()


def test_to_repo_dict_contains_submission_metadata():
    profile = MaterialProfile(
        id="test-pla",
        name="Test PLA",
        nozzle_temperature=220,
        max_volumetric_speed=20,
    )

    data = profile.to_repo_dict(submission_id="sub-abc")

    assert data["id"] == "test-pla"
    assert data["submission_id"] == "sub-abc"
    assert "updated_at" in data


def test_to_json_roundtrip():
    profile = MaterialProfile(
        id="test-pla",
        name="Test PLA",
        nozzle_temperature=220,
        max_volumetric_speed=20,
    )

    import json

    parsed = json.loads(profile.to_json(submission_id="sub-abc"))
    assert parsed["name"] == "Test PLA"
    assert parsed["submission_id"] == "sub-abc"
    assert profile.to_json(submission_id="sub-abc").endswith("\n")


# ----------------------------------------------------------------------
# V2-P2:parse_attachment_json —— 附件 JSON 解析(英文 key,严格,不许猜)
# ----------------------------------------------------------------------

def _valid_json():
    return """{
      "id": "mat-1",
      "name": "Test PLA",
      "nozzle_temperature": 220,
      "max_volumetric_speed": 20
    }"""


def test_parse_attachment_json_full():
    profile = MaterialProfile.parse_attachment_json(_valid_json())
    assert profile.id == "mat-1"
    assert profile.name == "Test PLA"
    assert profile.nozzle_temperature == 220
    assert profile.max_volumetric_speed == 20


def test_parse_attachment_json_id_defaults_to_name():
    """附件没带 id 时与表格语义一致:缺省用品名,不另行落「材料ID」。"""
    text = """{"name": "Test PLA", "nozzle_temperature": 220,
               "max_volumetric_speed": "20"}"""
    profile = MaterialProfile.parse_attachment_json(text)
    assert profile.id == "Test PLA"
    assert profile.max_volumetric_speed == 20  # 数字字符串可解析


def test_parse_attachment_json_accepts_nozzle_only_extra_whitespace():
    text = '{"name": "  Test PLA  ", "nozzle_temperature": 220, "max_volumetric_speed": 20}'
    profile = MaterialProfile.parse_attachment_json(text)
    assert profile.name == "Test PLA"  # 首尾空白修剪


def test_parse_attachment_json_rejects_unknown_key():
    text = '{"name": "x", "nozzle_temperature": 220, "max_volumetric_speed": 20, "cooling": 1}'
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json(text)
    assert "cooling" in str(exc.value)
    assert "未建模键" in str(exc.value)


def test_parse_attachment_json_rejects_reserved_worker_keys():
    """submission_id/updated_at 是 worker 生成元数据,附件不得携带覆盖。"""
    text = ('{"name": "x", "nozzle_temperature": 220, '
            '"max_volumetric_speed": 20, "submission_id": "fake"}')
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json(text)
    assert "submission_id" in str(exc.value)
    assert "保留键" in str(exc.value)


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


def test_parse_attachment_json_reports_missing_field():
    text = '{"name": "x", "nozzle_temperature": 220}'
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json(text)
    assert "max_volumetric_speed" in str(exc.value)
    assert "缺少必填字段" in str(exc.value)


def test_parse_attachment_json_rejects_bad_number():
    text = '{"name": "x", "nozzle_temperature": "hot", "max_volumetric_speed": 20}'
    with pytest.raises(ProfileValidationError) as exc:
        MaterialProfile.parse_attachment_json(text)
    assert "nozzle_temperature" in str(exc.value)
    assert "不是有效数字" in str(exc.value)
