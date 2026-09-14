from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from material_worker import fields as mw_fields


class ProfileValidationError(ValueError):
    pass


def _blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def as_number(field_name: str, value: object) -> float:
    """把表格值/附件值(数字或数字字符串)转为 float,非法即抛校验错误。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProfileValidationError(
            f"字段「{field_name}」不是有效数字: {value!r}"
        ) from exc


_NIL_TOKENS = frozenset({"", "nil", "null", "none"})


def is_nil_token(value: object) -> bool:
    """PrusaSlicer .ini 的 `nil`/空值判定(附件导入侧跳过用)。"""
    return str(value).strip().lower() in _NIL_TOKENS


def coerce_attachment_number(field_name: str, raw: object) -> float | None:
    """把附件里的数值解析为标量 float;**无法唯一确定时返回 None(跳过该键)**。

    V3-P3「宽容取值」:附件导入只找关心的键,取不到就跳过、不报错。
    真实 BambuStudio 系文件把数值写成字符串数组(多喷头一行一个值),
    未启用的喷头槽位写 `nil`:

    - 标量 / 数字字符串 -> 取该值;
    - 数组**先剔除 `nil`/`null`/空槽位**,剩余值解析出的数字全部一致
      -> 取该值:`['300','nil']`、`['220','220']`、单元素 `['220']`
      都合法(实测 `nozzle_temperature` 常是 `['300','nil']`);
    - 剔除后取值仍不一致(多喷头真的调得不同)-> **返回 None**,
      由调用方跳过该键 —— 绝不静默挑一个喷头的值;
    - 剔除后无剩余值(全是 `nil`)/ 空数组 / 非法数字 -> 返回 None。
    """
    if isinstance(raw, list):
        slots = [item for item in raw if not is_nil_token(item)]
        if not slots:
            return None
        parsed: list[float] = []
        for item in slots:
            try:
                parsed.append(float(item))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
        first = parsed[0]
        if any(value != first for value in parsed[1:]):
            return None
        return first
    if is_nil_token(raw):
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# V3:canonical schema —— 一份字段表同时驱动 Bitable 解析、附件反写、
# validate() 与输出序列化。附件 JSON 的英文 key 与 Bitable 中文列名
# 一一对应(键名与取值范围以 BambuStudio 官方 key registry 为准)。
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ProfileField:
    """一个 canonical 字段的元描述。"""

    key: str          # 附件 JSON / 输出 JSON 的键名(BambuStudio 官方 key)
    column: str       # Bitable 列名(中文,唯一事实来源在 fields.py)
    required: bool    # 必填(缺失/为空 -> 逐字段报错,不产生提交)
    numeric: bool     # 数值字段(数字或数字字符串)
    backfill: bool = True
    """附件导入是否允许反写该列(V3-P3:单选列必须人工手选,不从附件反写)。"""


FIELD_SCHEMA: tuple[ProfileField, ...] = (
    # —— 身份三要素 + 调参方法版本:均为单选列,只能人工在下拉中选择 ——
    # key 与列名同形,仅用于内部寻址(附件不反写、也不作为输出键)。
    ProfileField("pi_code", mw_fields.PI_CODE, True, False, backfill=False),
    ProfileField("printer_model", mw_fields.PRINTER_MODEL, True, False, backfill=False),
    ProfileField("slicer", mw_fields.SLICER, True, False, backfill=False),
    ProfileField("pm_method_version", mw_fields.PM_METHOD_VERSION, True, False,
                 backfill=False),
    # —— 结构参数:继承预设(V3 升为必填)——
    ProfileField("inherits", mw_fields.INHERITS, True, False),
    # —— 核心参数(必填)——
    ProfileField("textured_plate_temp", mw_fields.BED_TEMP, True, True),
    ProfileField("nozzle_temperature", mw_fields.NOZZLE_TEMP, True, True),
    ProfileField("fan_cooling_layer_time", mw_fields.FAN_COOLING_LAYER_TIME, True, True),
    ProfileField("fan_max_speed", mw_fields.FAN_MAX_SPEED, True, True),
    ProfileField("fan_min_speed", mw_fields.FAN_MIN_SPEED, True, True),
    ProfileField("slow_down_layer_time", mw_fields.SLOW_DOWN_LAYER_TIME, True, True),
    ProfileField("filament_flow_ratio", mw_fields.FLOW_RATIO, True, True),
    ProfileField("filament_max_volumetric_speed", mw_fields.MAX_VOL_SPEED, True, True),
    # —— 可选参数(为空 -> 输出省略该键,由 inherits 继承父配置)——
    ProfileField("filament_density", mw_fields.FILAMENT_DENSITY, False, True),
    ProfileField("temperature_vitrification", mw_fields.VITRIFICATION, False, True),
    ProfileField("filament_retraction_length", mw_fields.RETRACTION_LENGTH, False, True),
    ProfileField("pressure_advance", mw_fields.PRESSURE_ADVANCE, False, True),
)

REQUIRED_FIELDS: tuple[ProfileField, ...] = tuple(
    f for f in FIELD_SCHEMA if f.required
)

# 输出 JSON 里数值键的稳定顺序(= 表格参数列顺序)
_NUMERIC_KEYS: tuple[str, ...] = tuple(
    f.key for f in FIELD_SCHEMA if f.numeric
)


def _label(key: str) -> str:
    for f in FIELD_SCHEMA:
        if f.key == key:
            return f"{key}({f.column})"
    return key


#: canonical key -> Bitable 列名(反向索引,column_for / PR 正文标签共用)
_COLUMN_BY_KEY: dict[str, str] = {f.key: f.column for f in FIELD_SCHEMA}


def column_for(key: str) -> str:
    return _COLUMN_BY_KEY[key]


# 目录布局(V3-P4):
#   preset/<PI Code>/<品牌>/<机型>/<切片软件>/<PI Code>@<品牌> <机型>.json
# 例如 preset/L1002/BBL/P2S/BambuStudio/L1002@BBL P2S.json
# 所有切片器统一 .json —— worker 只产基础数据,切片器相关的形态转换
# (PrusaSlicer .ini、字符串数组、_initial_layer 伴随键)由构建脚本负责。
PRESET_DIR = "preset"

# 目录段里禁止出现的字符 —— 路径分隔符/控制字符防注入,
# 以及 Windows 保留字符,保证布局可跨平台检出。空格与中文保留。
_FORBIDDEN_PATH_CHARS: frozenset[str] = frozenset(
    set("/\\") | {chr(i) for i in range(32)} | set('<>:"|?*')
)


@dataclass
class MaterialProfile:
    """一个行身份(PI Code × 打印机型号 × 切片软件)的基础数据档案。

    一份 schema 同时服务 Bitable 手工填表、附件导入与 Git 序列化。
    数值均为标量;输出只写「我们关心的键」,不做任何切片器相关的
    形态转换(V3:那是构建脚本的职责)。
    """

    # 身份三要素(单选列,必填)
    pi_code: str
    printer_model: str
    slicer: str
    # 结构参数
    pm_method_version: str
    inherits: str
    # 核心参数(必填)
    textured_plate_temp: float
    nozzle_temperature: float
    fan_cooling_layer_time: float
    fan_max_speed: float
    fan_min_speed: float
    slow_down_layer_time: float
    filament_flow_ratio: float
    filament_max_volumetric_speed: float
    # 可选参数(空 -> 输出省略,由 inherits 继承)
    filament_density: float | None = None
    temperature_vitrification: float | None = None
    filament_retraction_length: float | None = None
    pressure_advance: float | None = None

    # 元数据:不写入材料档案文件本身。
    source_record_id: str | None = None

    # ------------------------------------------------------------------
    # 派生视图(V3-P4)
    # ------------------------------------------------------------------
    @property
    def brand(self) -> str:
        """品牌段 = 打印机型号按第一个空格切分后的首词(如 BBL)。"""
        return self.printer_model.split(" ", 1)[0]

    @property
    def model(self) -> str:
        """机型段 = 打印机型号去掉首词后的其余部分(如 P2S / K2 Pro)。"""
        parts = self.printer_model.split(" ", 1)
        return parts[1] if len(parts) > 1 else ""

    @property
    def identity(self) -> str:
        """行身份(Git 文件名主干):`<PI Code>@<打印机型号>`。"""
        return f"{self.pi_code}@{self.printer_model}"

    @property
    def ignores_pressure_advance(self) -> bool:
        """V3:官方 BBL 机型忽略压力提前(不输出该键,也不报错)。"""
        return self.brand == "BBL"

    def repo_name(self) -> str:
        """兼容别名:文件主干 = identity。"""
        return self.identity

    def repo_relative_path(self) -> str:
        """该档案在 preset-db 中的相对路径(V3-P4 布局)。

        目录段来自用户输入,validate() 已保证不含分隔符/保留字符;
        这里再做一次防御(直接构造未校验对象时也不至于写出越界路径)。
        """
        for label, segment in (
            (mw_fields.PI_CODE, self.pi_code),
            ("品牌", self.brand),
            ("机型", self.model),
            (mw_fields.SLICER, self.slicer),
        ):
            if not segment or any(c in _FORBIDDEN_PATH_CHARS for c in segment):
                raise ProfileValidationError(
                    f"「{label}」不能用作仓库目录段: {segment!r}"
                )
        return (
            f"{PRESET_DIR}/{self.pi_code}/{self.brand}/{self.model}/"
            f"{self.slicer}/{self.identity}.json"
        )

    # ------------------------------------------------------------------
    # Bitable -> domain(列名唯一事实来源在 fields.py)
    # ------------------------------------------------------------------
    @classmethod
    def from_bitable_record(
        cls,
        record_id: str,
        record_fields: dict[str, Any],
    ) -> "MaterialProfile":
        """Bitable 行 -> domain 对象(与附件导入共用同一 schema)。

        必填字段缺失/为空 -> 一次性列出全部缺失列(逐字段报错);
        数值非法 -> 逐字段报错。取值范围由 validate() 统一把关 ——
        不完整输入永远不会进入 Git 提交。
        """
        missing = [
            f.column for f in REQUIRED_FIELDS if _blank(record_fields.get(f.column))
        ]
        if missing:
            raise ProfileValidationError(
                "缺少必填字段: " + "、".join(missing)
                + f"(可补填单元格,或在「{mw_fields.PROFILE_JSON}」列挂附件导入)"
            )

        values: dict[str, Any] = {}
        for f in FIELD_SCHEMA:
            raw = record_fields.get(f.column)
            if f.numeric:
                values[f.key] = None if _blank(raw) else as_number(f.column, raw)
            else:
                values[f.key] = None if _blank(raw) else str(raw).strip()

        return cls(**values, source_record_id=record_id)

    # ------------------------------------------------------------------
    # 校验(所有输入路径共用的一份规则)
    # ------------------------------------------------------------------
    def validate(self) -> None:
        for f in REQUIRED_FIELDS:
            value = getattr(self, f.key)
            if _blank(value):
                raise ProfileValidationError(f"缺少必填字段: {f.column}")

        if not self.model:
            raise ProfileValidationError(
                f"「{mw_fields.PRINTER_MODEL}」需为「品牌 机型」形式"
                f"(如 BBL P2S / Prusa Core One): {self.printer_model!r}"
            )
        for label, segment in (
            (mw_fields.PI_CODE, self.pi_code),
            ("品牌", self.brand),
            ("机型", self.model),
            (mw_fields.SLICER, self.slicer),
        ):
            if any(c in _FORBIDDEN_PATH_CHARS for c in segment):
                raise ProfileValidationError(
                    f"「{label}」含路径分隔符/保留字符,不能用作仓库目录: "
                    f"{segment!r}"
                )

        for key, (low, high, unit, lower_exclusive) in _NUMERIC_BOUNDS.items():
            value = getattr(self, key)
            if value is None:  # 仅可选字段能到 None(必填在上面已拦截)
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ProfileValidationError(
                    f"字段「{_label(key)}」不是数字: {value!r}"
                )
            column = column_for(key)
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
    # 序列化(V3-P4:基础数据视图)
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """写入 Git 的基础数据内容(全部内容 —— 不含任何 worker 溯源键)。

        - 键序稳定:身份 → 结构 → 数值参数 → 可选参数;
        - 数值为标量,整数不写成 x.0;
        - **不写** `_initial_layer`/`first_layer_*` 伴随键、不写 `id`/
          `filament_settings_id`(product name 版预设留给构建阶段);
        - **不写** `submission`/`generated_at` 之类的溯源键:文件里的每一行
          都是材料数据,谁在什么时候改的由 PR 与 commit message 交代;
        - 可选键为空则不出现;`pressure_advance` 在 BBL 机型上被忽略。
        """
        data: dict[str, Any] = {
            "name": self.identity,
            "pi_code": self.pi_code,
            "printer": self.printer_model,
            "slicer": self.slicer,
            "inherits": self.inherits,
            "pm_method_version": self.pm_method_version,
        }
        for key in _NUMERIC_KEYS:
            value = getattr(self, key)
            if value is None:
                continue
            if key == "pressure_advance" and self.ignores_pressure_advance:
                continue  # V3:BBL 官方机型忽略压力提前
            data[key] = _json_number(value)
        if (
            self.pressure_advance is not None
            and not self.ignores_pressure_advance
        ):
            data["enable_pressure_advance"] = 1
        return data

    def to_json(self) -> str:
        """规范化的 JSON 文本(固定键序、utf-8、2 空格缩进、末行换行)。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"


def _json_number(value: float) -> int | float:
    """整数按 int 输出(220.0 -> 220),避免 JSON 里出现 220.0。"""
    return int(value) if float(value).is_integer() else value


# ----------------------------------------------------------------------
# 差异描述(PR 正文用):把两版基础数据讲成人话
# ----------------------------------------------------------------------
#: 差异里**不出现**的键:身份四要素在每个文件路径下恒定(PR 第一行已交代
#: 身份);enable_pressure_advance 只是 pressure_advance 的派生影子,讲一遍
#: 就够;**内部溯源键**(已废弃,但仓库里遗留的旧文件仍带着它们)绝不能
#: 借差异描述重新漏回 PR 正文。
_DIFF_HIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "pi_code",
        "printer",
        "slicer",
        "enable_pressure_advance",
        "submission",
        "submission_id",
        "generated_at",
    }
)

#: 差异行的键序(与 to_dict() 输出顺序一致);没列到的键排在其后按字母序。
_DIFF_KEY_ORDER: tuple[str, ...] = ("inherits", "pm_method_version", *_NUMERIC_KEYS)

#: 缺值写法(与表格里的「空」对应,读 PR 的人一眼知道是没填而非没改)。
_UNSET_TEXT = "(未设置)"


def parse_profile_json(text: str | None) -> dict[str, Any] | None:
    """解析已落库的基础数据 JSON;读不回来一律按「无对照版本」处理(None)。

    只服务 PR 正文的差异描述 —— 仓库里的文件偶尔可能是手改的非 JSON、
    或旧格式,那种情况下正文退化为「列出全部取值」,绝不让读文件失败
    影响提交本身。
    """
    if not text:
        return None
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _diff_value_text(key: str, value: Any) -> str:
    """把一个输出键的值渲染成表格上认得的写法(带单位,缺值写作(未设置))。"""
    if value is None:
        return _UNSET_TEXT
    bounds = _NUMERIC_BOUNDS.get(key)
    if bounds is not None:
        try:
            return f"{_json_number(float(value))}{bounds[2]}"
        except (TypeError, ValueError):
            pass  # 手改出来的非数字 -> 原样展示,不因正文而失败
    return str(value)


def describe_changes(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
) -> list[str]:
    """把两版基础数据的差异讲成人话(PR 正文的主体)。

    - `before` 为 None = 默认分支上还没有这个档案(首次提交)-> 无旧值可比,
      只列取值;
    - 有旧版本时每行都是 `旧 → 新`:值变了写旧值,该键原先没有(如刚填上
      的可选参数)/现在没有了,另一边写 `(未设置)`;
    - 标签用表格列名、数值带单位,不出现内部键名(审查者不认 `fan_max_speed`);
    - 无差异的键不出现;身份四要素与派生键见 `_DIFF_HIDDEN_KEYS`。
    """
    first_submission = before is None
    old: Mapping[str, Any] = before or {}
    extra = sorted(
        key
        for key in set(old) | set(after)
        if key not in _DIFF_HIDDEN_KEYS and key not in _DIFF_KEY_ORDER
    )
    lines: list[str] = []
    for key in (*_DIFF_KEY_ORDER, *extra):
        was, now = old.get(key), after.get(key)
        if was == now:
            continue
        label = _COLUMN_BY_KEY.get(key, key)
        if first_submission:
            lines.append(f"{label}: {_diff_value_text(key, now)}")
        else:
            lines.append(
                f"{label}: {_diff_value_text(key, was)} → "
                f"{_diff_value_text(key, now)}"
            )
    return lines


# 数值安全护栏(拦截单位/小数位笔误;取值范围以 BambuStudio 官方
# key registry 的合理区间为准,宽松到不会误伤真实值):
#   key -> (下限, 上限, 单位, 下限是否开区间)
_NUMERIC_BOUNDS: dict[str, tuple[float, float, str, bool]] = {
    "textured_plate_temp": (0, 200, " °C", False),
    "nozzle_temperature": (0, 600, " °C", True),
    "fan_cooling_layer_time": (0, 600, " 秒", False),        # 最小层时
    "fan_max_speed": (0, 100, " %", False),
    "fan_min_speed": (0, 100, " %", False),
    "slow_down_layer_time": (0, 600, " 秒", False),
    "filament_flow_ratio": (0, 5, "", True),                 # 常见 0.6–1.5
    "filament_max_volumetric_speed": (0, 300, " mm³/s", True),
    "filament_density": (0, 50, " g/cm³", True),             # 常见 1.0–4
    "temperature_vitrification": (0, 400, " °C", True),      # Tg 常见 45–200
    "filament_retraction_length": (0, 100, " mm", False),    # 常见 0–18
    "pressure_advance": (0, 5, "", False),                   # 常见 0–0.3
}
