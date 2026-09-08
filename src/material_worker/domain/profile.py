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
        raise ProfileValidationError(
            f"字段「{field_name}」不是有效数字: {value!r}"
        ) from exc


_NIL_TOKENS = frozenset({"", "nil", "null", "none"})


def _attachment_number(field_name: str, raw: object) -> float:
    """把附件 JSON 的数值解析为标量 float(V2-P4 放宽后)。

    真实 BambuStudio 系文件把数值写成字符串数组(多喷头一行一个值),
    上传的 JSON 可能是标量、数字字符串,也可能是数组:

    - 数组各值(剔除后)解析出的数字全部一致 -> 取该值
      (单元素 ['220']、多元素 ['220','220'] 都合法);
    - 数组含 nil/null/空、或各值解析后不一致 -> 报错提示改为单值
      或在表格手填(绝不静默挑一个喷头的值)。

    输出侧仍是标量(见 to_dict —— 输入宽容、canonical 输出)。
    """
    if not isinstance(raw, list):
        return _as_number(field_name, raw)
    if not raw:
        raise ProfileValidationError(f"附件字段「{field_name}」数值为空数组")
    parsed: list[float] = []
    for item in raw:
        text = "" if item is None else str(item).strip()
        if text.lower() in _NIL_TOKENS:
            raise ProfileValidationError(
                f"附件字段「{field_name}」数值含未设置值(nil/空),"
                f"无法确定唯一数值: {raw!r}。请改为单值或在表格手填"
            )
        parsed.append(_as_number(field_name, item))
    first = parsed[0]
    if any(value != first for value in parsed[1:]):
        raise ProfileValidationError(
            f"附件字段「{field_name}」数值数组各值不一致(多喷头取值不同):"
            f" {raw!r}。请改为单值或在表格手填"
        )
    return first


def _blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


# ----------------------------------------------------------------------
# V2-P4:canonical schema —— 一份字段表同时驱动 Bitable 解析、附件 JSON
# 解析、validate()、附件反写。附件 JSON 的英文 key 与 Bitable 中文列名
# 一一对应(键名与取值范围以 BambuStudio 官方 key registry 为准,§8/§8.1)。
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ProfileField:
    """一个 canonical 字段的元描述。"""

    key: str          # 附件 JSON / 属性名(BambuStudio 官方 key,§8)
    column: str       # Bitable 列名(中文,唯一事实来源在 fields.py)
    required: bool    # 必填(缺失/为空 -> 逐字段报错,不产生提交)
    numeric: bool     # 数值字段(数字或数字字符串;可空的可选值空 -> None)


FIELD_SCHEMA: tuple[ProfileField, ...] = (
    # —— 结构参数:B(§9)输入侧;品牌/机型/切片器同时决定目录布局(§12) ——
    ProfileField("name", mw_fields.NAME, True, False),          # 品名 = 目录段1
    ProfileField("brand", mw_fields.BRAND, True, False),        # 目录段2
    ProfileField("model", mw_fields.MODEL, True, False),        # 目录段3(不含喷嘴)
    ProfileField("slicer", mw_fields.SLICER, True, False),      # 目录段4
    # —— 耗材参数 A(§8,除压力提前外全部必填,进入 Git JSON) ——
    ProfileField("filament_density", mw_fields.FILAMENT_DENSITY, True, True),
    ProfileField("temperature_vitrification", mw_fields.VITRIFICATION, True, True),
    ProfileField("fan_cooling_layer_time", mw_fields.FAN_COOLING_LAYER_TIME, True, True),
    ProfileField("fan_max_speed", mw_fields.FAN_MAX_SPEED, True, True),
    ProfileField("fan_min_speed", mw_fields.FAN_MIN_SPEED, True, True),
    ProfileField("slow_down_layer_time", mw_fields.SLOW_DOWN_LAYER_TIME, True, True),
    ProfileField("nozzle_temperature", mw_fields.NOZZLE_TEMP, True, True),
    ProfileField("filament_flow_ratio", mw_fields.FLOW_RATIO, True, True),
    ProfileField("filament_max_volumetric_speed", mw_fields.MAX_VOL_SPEED, True, True),
    ProfileField("filament_retraction_length", mw_fields.RETRACTION_LENGTH, True, True),
    # —— 可选 A:压力提前(仅第三方机器;官方 BBL 机型填了即报错,见 validate) ——
    ProfileField("pressure_advance", mw_fields.PRESSURE_ADVANCE, False, True),
    # —— 可选 B 结构参数(留空 -> Git JSON 省略对应键) ——
    ProfileField("inherits", mw_fields.INHERITS, False, False),
    ProfileField("version", mw_fields.SLICER_VERSION, False, False),
    ProfileField("pm_method_version", mw_fields.PM_METHOD_VERSION, False, False),
    # V2-P4 PI Code seam:映射到 product name(name/目录段)的映射表尚未确定,
    # 当前仅建模存储(见 MaterialProfile.pi_code 与 Git 输出省略规则)。
    ProfileField("pi_code", mw_fields.PI_CODE, False, False),
    # 材料身份(可选;Git JSON 里缺省落品名,不再参与文件名)
    ProfileField("id", mw_fields.MATERIAL_ID, False, False),
)

REQUIRED_FIELDS: tuple[ProfileField, ...] = tuple(
    f for f in FIELD_SCHEMA if f.required
)

def _label(key: str) -> str:
    for f in FIELD_SCHEMA:
        if f.key == key:
            return f"{key}({f.column})"
    return key


# 目录布局(照搬 Polymaker-Preset 实况,§12 R2):
#   preset/<品名>/<品牌>/<机型>/<切片器>/<品名> @<品牌> <机型>.json
# 例如 preset/PolyTerra PLA/BBL/H2C/BambuStudio/PolyTerra PLA @BBL H2C.json
PRESET_DIR = "preset"

# V2-P4:目录段里禁止出现的字符 —— 路径分隔符/控制字符防注入,
# 以及 Windows 保留字符,保证布局可跨平台检出。空格与中文保留。
_FORBIDDEN_PATH_CHARS: frozenset[str] = frozenset(
    set("/\\") | {chr(i) for i in range(32)} | set('<>:"|?*')
)
# 受支持切片器(输出 BambuStudio 系 JSON);PrusaSlicer 产物是 .ini,暂不支持
ALLOWED_SLICERS: tuple[str, ...] = (
    "BambuStudio",
    "Orcaslicer",
    "OrcaSlicer",
    "ElegooSlicer",
    "CrealityPrint",
)


@dataclass
class MaterialProfile:
    """材料×机型 的一份规范档案(domain model,不依赖任何外部 SDK)。

    V2-P4:一份 schema 同时服务 Bitable 手工填表、JSON 附件导入与
    Git 序列化(§11 —— 无两套规则);数值均为标量(Bitable/附件/
    Git JSON 同构,与实况 preset 文件的字符串数组形态无关)。

    Git 提交产物是 worker 派生的视图:
      - JSON 键 name / 文件名主干 = "<品名> @<品牌> <机型>"(用户确认);
      - id 缺省 = 品名(材料ID 只作备注,不再参与文件名);
      - filament_settings_id = name;enable_pressure_advance 由
        pressure_advance 派生;submission/generated_at worker 生成。
    """

    # 结构参数 B(输入侧;必填项参与目录布局)
    name: str
    brand: str
    model: str
    slicer: str
    # 耗材参数 A(物理实测,全量必填)
    filament_density: float
    temperature_vitrification: float
    fan_cooling_layer_time: float
    fan_max_speed: float
    fan_min_speed: float
    slow_down_layer_time: float
    nozzle_temperature: float
    filament_flow_ratio: float
    filament_max_volumetric_speed: float
    filament_retraction_length: float
    # 可选 A:压力提前(仅第三方机器;官方 BBL 机型填了即报错)
    pressure_advance: float | None = None
    # 可选 B 结构参数(留空 -> Git JSON 省略对应键)
    inherits: str | None = None
    version: str | None = None
    pm_method_version: str | None = None
    # TODO(V2-P4 seam):PI Code -> 产品名(name/目录段)的映射表尚未确定。
    # 映射落地后:name 由 pi_code 查表派生或校验,并决定目录段/文件名前缀。
    # 当前 pi_code 仅建模存储(表列 + 附件键可填),validate 只做基本
    # 长度/字符校验,**不进入 Git JSON**(用户确认:仅建模不入 Git)。
    pi_code: str | None = None
    # 材料身份(可选;空 -> Git JSON 里缺省落品名)
    id: str | None = None

    # 元数据:不写入材料档案文件本身。
    source_record_id: str | None = None
    submitted_at: datetime | None = None

    # ------------------------------------------------------------------
    # 派生视图(V2-P4)
    # ------------------------------------------------------------------
    def repo_name(self) -> str:
        """Git JSON 的 name 键 / 文件名主干(worker 派生,用户确认)。"""
        return f"{self.name} @{self.brand} {self.model}"

    def repo_relative_path(self) -> str:
        """该档案在仓库中的相对路径(V2-P4 布局,见 PRESET_DIR docstring)。

        目录段来自用户输入,validate() 已保证不含分隔符/保留字符;
        这里再做一次防御(直接构造未校验对象时也不至于写出越界路径)。
        """
        for label, segment in (
            ("品名", self.name),
            ("品牌", self.brand),
            ("机型", self.model),
            ("切片器", self.slicer),
        ):
            if not segment or any(c in _FORBIDDEN_PATH_CHARS for c in segment):
                raise ProfileValidationError(
                    f"「{label}」不能用作仓库目录段: {segment!r}"
                )
        return (
            f"{PRESET_DIR}/{self.name}/{self.brand}/{self.model}/"
            f"{self.slicer}/{self.repo_name()}.json"
        )

    @property
    def resolved_id(self) -> str:
        """Git JSON 的 id:材料ID 优先,缺省用品名(P1 语义延续)。"""
        return self.id or self.name

    # ------------------------------------------------------------------
    # Bitable -> domain(列名唯一事实来源在 fields.py)
    # ------------------------------------------------------------------
    @classmethod
    def from_bitable_record(
        cls,
        record_id: str,
        record_fields: dict[str, Any],
    ) -> "MaterialProfile":
        """Bitable 行 -> domain 对象(与附件 JSON 共用同一 schema)。

        必填字段缺失/为空 -> 一次性列出全部缺失列(逐字段报错);
        数值非法 -> 逐字段报错。取值范围/切片器白名单/BBL-PA 规则
        由 validate() 统一把关 —— 不完整输入永远不会进入 Git 提交。
        """
        missing = [
            f.column for f in REQUIRED_FIELDS if _blank(record_fields.get(f.column))
        ]
        if missing:
            raise ProfileValidationError(
                "缺少必填字段: " + "、".join(missing)
                + "(可补填单元格,或在「Profile JSON」列挂附件一次导入)"
            )

        values: dict[str, Any] = {}
        for f in FIELD_SCHEMA:
            raw = record_fields.get(f.column)
            if f.numeric:
                values[f.key] = None if _blank(raw) else _as_number(f.column, raw)
            else:
                values[f.key] = None if _blank(raw) else str(raw).strip()

        return cls(
            **values,
            source_record_id=record_id,
            submitted_at=datetime.now().astimezone(),
        )

    # ------------------------------------------------------------------
    # 附件 JSON -> domain(必填 14 键解析,其余键忽略;V2-P4 上传语义)
    # ------------------------------------------------------------------
    @classmethod
    def parse_attachment_json(cls, text: str) -> "MaterialProfile":
        """附件 JSON -> MaterialProfile(与 Bitable 手工填表同一 schema)。

        规则(V2-P4,用户确认的上传语义):
        - 上传 JSON 一定是新建材料 —— 顶层必须是 JSON 对象;
        - 必填键 = 与表格相同的 14 项(name/brand/model/slicer + 10 项 A),
          只要这 14 项能解析出来就认为合法;缺失一次性全部列出;
        - 其余键(真实 BambuStudio 系文件的附加键:plate 温度/filament_*
          等)一律**忽略**,不做未知键报错;worker 派生键(submission/
          generated_at/filament_settings_id/enable_pressure_advance)也
          由 worker 自己生成,附件携带同名键不读取;
        - 数值接受数字/数字字符串/字符串数组(单元素或各值一致,
          见 _attachment_number);取值由 validate() 把关;
        - 输出侧仍是 canonical 标量视图(to_dict,输入宽容、输出一致)。
        """
        if not text or not text.strip():
            raise ProfileValidationError("附件 JSON 为空")

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

        missing = [
            _label(f.key) for f in REQUIRED_FIELDS if f.key not in raw
        ]
        if missing:
            raise ProfileValidationError(
                "附件 JSON 缺少必填字段: " + "、".join(missing)
            )

        values: dict[str, Any] = {}
        for f in FIELD_SCHEMA:
            if f.key not in raw:
                values[f.key] = None
                continue
            raw_value = raw[f.key]
            if _blank(raw_value):
                values[f.key] = None
                continue
            if f.numeric:
                values[f.key] = _attachment_number(_label(f.key), raw_value)
            else:
                values[f.key] = str(raw_value).strip()

        return cls(
            **values,
            submitted_at=datetime.now().astimezone(),
        )

    # ------------------------------------------------------------------
    # 校验(所有输入路径共用的一份规则,§11)
    # ------------------------------------------------------------------
    def validate(self) -> None:
        for f in REQUIRED_FIELDS:
            value = getattr(self, f.key)
            if _blank(value):
                raise ProfileValidationError(f"缺少必填字段: {f.column}")
        if isinstance(self.name, str) and any(
            c in _FORBIDDEN_PATH_CHARS for c in self.name
        ):
            raise ProfileValidationError(
                f"「品名」含路径分隔符/保留字符,不能用作仓库目录: {self.name!r}"
            )

        if self.slicer not in ALLOWED_SLICERS:
            raise ProfileValidationError(
                f"「切片器」不支持: {self.slicer!r}。"
                f"可选值: {' / '.join(ALLOWED_SLICERS)}"
                f"(PrusaSlicer 产物为 .ini 预设,暂不支持)"
            )
        # 压力提前仅第三方机器(实况:BBL 官方机型文件一律不带 PA 键)
        if self.pressure_advance is not None and self.brand == "BBL":
            raise ProfileValidationError(
                "官方机型(BBL)不支持压力提前参数:「压力提前」仅适用于第三方机器"
            )

        for key, (low, high, unit, lower_exclusive) in _NUMERIC_BOUNDS.items():
            value = getattr(self, key)
            if value is None:  # 仅可选字段能到 None(必填在上面已拦截)
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ProfileValidationError(
                    f"字段「{_label(key)}」不是数字: {value!r}"
                )
            column = next(f.column for f in FIELD_SCHEMA if f.key == key)
            if lower_exclusive:
                ok = low < value <= high
                desc = f"需大于 {low} 且不超过 {high}"
            else:
                ok = low <= value <= high
                desc = f"需在 {low}–{high} 之间"
            if not ok:
                raise ProfileValidationError(
                    f"「{column}」{desc}{unit}: {value!r}"
                )

    # ------------------------------------------------------------------
    # 序列化(V2-P4:Git 产物为派生视图,见类 docstring)
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """写入 Git 的文件内容(不含 worker 提交溯源键 submission/generated_at)。

        name = worker 派生复合名;<品名> 不单独成键(用户确认);
        pi_code 仅建模存储、不进入 Git JSON(用户确认);数值标量,
        整数不写成 x.0;键顺序稳定。
        """
        data: dict[str, Any] = {
            "id": self.resolved_id,
            "name": self.repo_name(),
            "brand": self.brand,
            "model": self.model,
            "slicer": self.slicer,
        }
        for key in (
            "inherits",
            "version",
            "pm_method_version",
        ):
            value = getattr(self, key)
            if value:
                data[key] = value
        data["filament_settings_id"] = self.repo_name()
        for f in FIELD_SCHEMA:
            if not f.numeric or not f.required:
                continue
            data[f.key] = _json_number(getattr(self, f.key))
        if self.pressure_advance is not None:
            data["pressure_advance"] = _json_number(self.pressure_advance)
            data["enable_pressure_advance"] = 1
        return data

    def to_repo_dict(self, submission_id: str) -> dict[str, Any]:
        """写入 Git 仓库的完整内容 = 档案 + 本次提交溯源(§9/§15)。"""
        data = self.to_dict()
        data["submission"] = submission_id
        data["generated_at"] = self._generated_at_iso()
        return data

    def _generated_at_iso(self) -> str:
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


def _json_number(value: float) -> int | float:
    """整数按 int 输出(220.0 -> 220),避免 JSON 里出现 220.0。"""
    return int(value) if float(value).is_integer() else value


# V2-P4:数值安全护栏(拦截单位/小数位笔误;取值范围以 BambuStudio
# 官方 key registry 的合理区间为准,宽松到不会误伤真实值):
#   key -> (下限, 上限, 单位, 下限是否开区间)
_NUMERIC_BOUNDS: dict[str, tuple[float, float, str, bool]] = {
    "filament_density": (0, 50, " g/cm³", True),             # 常见 1.0–4
    "temperature_vitrification": (0, 400, " °C", True),      # Tg 常见 45–200
    "fan_cooling_layer_time": (0, 600, " 秒", False),        # 最小层时
    "fan_max_speed": (0, 100, " %", False),
    "fan_min_speed": (0, 100, " %", False),
    "slow_down_layer_time": (0, 600, " 秒", False),
    "nozzle_temperature": (0, 600, " °C", True),
    "filament_flow_ratio": (0, 5, "", True),                 # 常见 0.6–1.5
    "filament_max_volumetric_speed": (0, 300, " mm³/s", True),
    "filament_retraction_length": (0, 100, " mm", False),    # 常见 0–18
    "pressure_advance": (0, 5, "", False),                   # 常见 0–0.3
}
