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


# V2-P2:附件 JSON 的合法键 = 与 Git Profile 同构的英文 key(全量见 P4)。
# 展示用中文名只出现在错误提示里,解析器不做任何键名猜测。
_PROFILE_JSON_KEY_LABELS: dict[str, str] = {
    "id": "材料ID",
    "name": "品名",
    "nozzle_temperature": "喷嘴温度",
    "max_volumetric_speed": "最大体积流速",
}
# worker 生成、用户不得携带的保留键(提交溯源/时间戳)
_RESERVED_PROFILE_KEYS: frozenset[str] = frozenset(
    {"submission_id", "updated_at"}
)


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

    @classmethod
    def parse_attachment_json(cls, text: str) -> "MaterialProfile":
        """V2-P2:附件 JSON -> MaterialProfile(键名规范:R2-英文同构,不许猜)。

        规则(与 Bitable 提交共用同一 validate,见 service 反写处):
        - 必须是 JSON 对象;键名必须是已知英文 key(id/name/nozzle_temperature/
          max_volumetric_speed),未知键、worker 保留键(submission_id/
          updated_at)一律报错并列出;
        - name/nozzle_temperature/max_volumetric_speed 必填(缺一报错);
          id 可选,缺省用品名(与表格「材料ID」语义一致);
        - 数值只做类型解析(数字或数字字符串),取值范围由 validate() 把关;
        - 不做任何宽松猜测(不忽略未知键、不猜别名)。
        """
        if not text or not text.strip():
            raise ProfileValidationError("附件 JSON 为空")

        def _label(key: str) -> str:
            label = _PROFILE_JSON_KEY_LABELS.get(key)
            return f"{key}({label})" if label else key

        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProfileValidationError(
                f"附件不是合法 JSON: 第 {exc.lineno} 行第 {exc.colno} 列 "
                f"({exc.msg})"
            ) from exc
        if not isinstance(raw, dict):
            raise ProfileValidationError(
                f"附件 JSON 顶层必须是对象,实际是 {type(raw).__name__}"
            )

        unknown = sorted(set(raw) - set(_PROFILE_JSON_KEY_LABELS))
        if unknown:
            reserved = sorted(set(unknown) & _RESERVED_PROFILE_KEYS)
            hints = f"(其中 worker 保留键: {', '.join(reserved)})" if reserved else ""
            raise ProfileValidationError(
                "附件 JSON 含未建模键: "
                + ", ".join(_label(k) for k in unknown)
                + f";只接受: {', '.join(sorted(_PROFILE_JSON_KEY_LABELS))}"
                + hints
            )

        for required in ("name", "nozzle_temperature", "max_volumetric_speed"):
            if required not in raw:
                raise ProfileValidationError(
                    f"附件 JSON 缺少必填字段: {_label(required)}"
                )
        if raw.get("name") is None or str(raw["name"]).strip() == "":
            raise ProfileValidationError("附件 JSON 的 name(品名)不能为空")

        profile_id = raw.get("id")
        if profile_id is None or str(profile_id).strip() == "":
            profile_id = str(raw["name"]).strip()
        else:
            profile_id = str(profile_id).strip()

        return cls(
            id=profile_id,
            name=str(raw["name"]).strip(),
            nozzle_temperature=_as_number(
                _label("nozzle_temperature"), raw["nozzle_temperature"]
            ),
            max_volumetric_speed=_as_number(
                _label("max_volumetric_speed"), raw["max_volumetric_speed"]
            ),
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
