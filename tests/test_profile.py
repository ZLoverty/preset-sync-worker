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
