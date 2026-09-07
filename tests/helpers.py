"""V2-P4:共享测试工厂 —— 构造全字段合法的 MaterialProfile/Bitable 行/附件 JSON。

默认值对应一个 BBL/H2C/BambuStudio 的完整档案(与 Polymaker-Preset
实况同构);各测试用 override 改个别字段,避免每个用例手写 14 个必填值。
"""

import json

from material_worker.domain.profile import FIELD_SCHEMA, MaterialProfile

DEFAULTS = {
    "name": "Test PLA",
    "brand": "BBL",
    "model": "H2C",
    "slicer": "BambuStudio",
    "filament_density": 1.24,
    "temperature_vitrification": 59,
    "fan_cooling_layer_time": 100,
    "fan_max_speed": 100,
    "fan_min_speed": 100,
    "slow_down_layer_time": 8,
    "nozzle_temperature": 220,
    "filament_flow_ratio": 0.98,
    "filament_max_volumetric_speed": 16,
    "filament_retraction_length": 0.4,
}


def make_profile(**overrides) -> MaterialProfile:
    kwargs = dict(DEFAULTS)
    kwargs.update(overrides)
    return MaterialProfile(**kwargs)


def row_fields(**overrides) -> dict:
    """Bitable 行字段快照:每个 canonical 列一个默认合法值。

    材料ID/继承预设等可选列默认留空;状态/提交元数据由调用方另补。
    """
    data = {}
    for f in FIELD_SCHEMA:
        if f.key == "id":
            continue  # 材料ID 默认空(材料身份缺省=品名)
        if f.key in DEFAULTS:
            data[f.column] = DEFAULTS[f.key]
    data.update(overrides)
    return data


def profile_attachment_json(**overrides) -> str:
    """合法附件 JSON(英文 key 与 Git Profile 同构):必填 14 项 + 覆盖项。"""
    payload: dict = {}
    for f in FIELD_SCHEMA:
        if f.key == "id":
            continue
        if f.key in DEFAULTS:
            payload[f.key] = DEFAULTS[f.key]
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return json.dumps(payload)
