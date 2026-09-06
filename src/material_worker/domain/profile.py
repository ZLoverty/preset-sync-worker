from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from material_worker import fields as mw_fields


class ProfileValidationError(ValueError):
    pass


def _as_number(field_name: str, value: object) -> float:
    """把表格值(数字或数字字符串)转为 float,非法即抛校验错误。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProfileValidationError(f"字段「{field_name}」不是有效数字: {value!r}") from exc


@dataclass
class MaterialProfile:
    """一种材料的规范档案(domain model,不依赖任何外部 SDK)。

    Git 仓库中的 JSON 即由此序列化;Bitable 字段解析见
    ``from_bitable_record``(列名集中定义在 fields.py)。
    """

    id: str
    name: str
    nozzle_temperature: float
    max_volumetric_speed: float

    # 元数据:不写入材料档案文件本身(档案里只有 id/数值/提交时间)。
    source_record_id: str | None = None
    submitted_at: datetime | None = None

    @classmethod
    def from_bitable_record(
        cls,
        record_id: str,
        fields: dict[str, Any],
    ) -> "MaterialProfile":
        """Bitable 行 -> domain 对象。

        任一核心字段缺失/非法都会抛 ProfileValidationError,
        保证不完整输入永远不会进入 Git 提交(P0 #5)。
        """

        def missing(name: str) -> bool:
            value = fields.get(name)
            return value is None or str(value).strip() == ""

        if missing(mw_fields.NAME):
            raise ProfileValidationError(f"缺少字段: {mw_fields.NAME}")
        if missing(mw_fields.NOZZLE_TEMP):
            raise ProfileValidationError(f"缺少字段: {mw_fields.NOZZLE_TEMP}")
        if missing(mw_fields.MAX_VOL_SPEED):
            raise ProfileValidationError(f"缺少字段: {mw_fields.MAX_VOL_SPEED}")

        name = str(fields[mw_fields.NAME]).strip()

        # 材料身份:优先表格「材料ID」列,缺省用品名。
        profile_id = fields.get(mw_fields.MATERIAL_ID)
        if profile_id is None or str(profile_id).strip() == "":
            profile_id = name
        else:
            profile_id = str(profile_id).strip()

        return cls(
            id=profile_id,
            name=name,
            nozzle_temperature=_as_number(
                mw_fields.NOZZLE_TEMP, fields[mw_fields.NOZZLE_TEMP]
            ),
            max_volumetric_speed=_as_number(
                mw_fields.MAX_VOL_SPEED, fields[mw_fields.MAX_VOL_SPEED]
            ),
            source_record_id=record_id,
            submitted_at=datetime.now().astimezone(),
        )

    def validate(self) -> None:
        if not self.id:
            raise ProfileValidationError("材料 ID 不能为空")
        if not self.name:
            raise ProfileValidationError("品名不能为空")
        if not 0 < self.nozzle_temperature <= 600:
            raise ProfileValidationError("喷嘴温度需大于 0 且不超过 600 °C")
        if not 0 < self.max_volumetric_speed <= 300:
            raise ProfileValidationError("最大体积流速需大于 0 且不超过 300 mm³/s")

    def to_dict(self) -> dict[str, Any]:
        """材料档案核心内容(无提交元数据)。"""
        return {
            "id": self.id,
            "name": self.name,
            "nozzle_temperature": self.nozzle_temperature,
            "max_volumetric_speed": self.max_volumetric_speed,
        }

    def to_repo_dict(self, submission_id: str) -> dict[str, Any]:
        """写入 Git 仓库的档案内容 = 核心字段 + 本次提交元数据。"""
        data = self.to_dict()
        data["submission_id"] = submission_id
        data["updated_at"] = self._updated_at_iso()
        return data

    def _updated_at_iso(self) -> str:
        moment = self.submitted_at or datetime.now().astimezone()
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment.isoformat(timespec="seconds")

    def to_json(self, submission_id: str | None = None) -> str:
        """规范化的 JSON 文本(固定键序、utf-8、2 空格缩进、末行换行)。"""
        if submission_id is None:
            data: dict[str, Any] = self.to_dict()
        else:
            data = self.to_repo_dict(submission_id)
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
