"""附件导入的**宽容取值**(V3-P3):只找关心的键,找不到就跳过。

与 V2 的根本区别:不再要求附件含全部必填键。真实切片器导出的
base 文件普遍缺键(如 `Generic PETG-CF @base.json` 没有
`nozzle_temperature`/`fan_cooling_layer_time`),V2 会因此整行失败。
V3 改为逐键查找 —— 找到就取值、找不到就跳过,不报错。

支持两种格式:
- `.json`:顶层对象,键名 = canonical 英文键(BambuStudio 系导出形态,
  数值可能是标量、数字字符串或字符串数组,见 coerce_attachment_number);
- `.ini`:PrusaSlicer 扁平 `key = value`,按 slicer_import 的映射反查。

本模块只产出「列 -> 值」的字典,不落表、不校验必填 ——
反写策略(只填空、不覆盖)与失败呈现由 service 层决定。
"""

from __future__ import annotations

import json
from typing import Any

from preset_sync_worker.domain import slicer_import
from preset_sync_worker.domain.profile import (
    FIELD_SCHEMA,
    coerce_attachment_number,
    is_nil_token,
)

JSON_EXTENSION = ".json"
INI_EXTENSION = ".ini"
SUPPORTED_EXTENSIONS: tuple[str, ...] = (JSON_EXTENSION, INI_EXTENSION)


class AttachmentFormatError(ValueError):
    """附件**文件级**问题(格式不支持/非法 JSON/顶层不是对象)。

    与「键找不到」区分:后者是正常的宽容跳过,绝不报错。
    """


def extract_attachment_values(text: str, filename: str) -> dict[str, Any]:
    """附件文本 -> {Bitable 列名: 值}(只含认得的键)。

    - 文件级问题(扩展名不支持/JSON 非法/顶层非对象)抛 AttachmentFormatError;
    - 单个键的值取不到(缺失/`nil`/非法数字/多喷头取值不一致)一律**跳过**;
    - 返回空字典表示「一个关心的键都没认出来」,由调用方静默跳过。
    """
    lower = filename.lower()
    if lower.endswith(JSON_EXTENSION):
        canonical = _canonical_from_json(text)
    elif lower.endswith(INI_EXTENSION):
        canonical = dict(slicer_import.extract_prusa_values(text))
    else:
        raise AttachmentFormatError(
            f"附件格式不支持: {filename!r}。"
            f"仅支持 {' / '.join(SUPPORTED_EXTENSIONS)}"
        )
    return _to_column_values(canonical)


def _canonical_from_json(text: str) -> dict[str, Any]:
    """JSON 附件 -> {canonical key: 原始值}(其余键一律忽略)。"""
    if not text or not text.strip():
        raise AttachmentFormatError("附件内容为空")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AttachmentFormatError(
            f"附件不是合法 JSON: 第 {exc.lineno} 行第 {exc.colno} 列 "
            f"({exc.msg})"
        ) from exc
    if not isinstance(raw, dict):
        raise AttachmentFormatError(
            f"附件 JSON 顶层必须是对象,实际是 {type(raw).__name__}"
        )
    return {f.key: raw[f.key] for f in FIELD_SCHEMA if f.key in raw}


def _to_column_values(canonical: dict[str, Any]) -> dict[str, Any]:
    """canonical 键值 -> Bitable 列值(按 FIELD_SCHEMA 过滤与转型)。

    单选列(backfill=False)一律不从附件取 —— 必须人工在下拉中选择。
    """
    values: dict[str, Any] = {}
    for f in FIELD_SCHEMA:
        if not f.backfill or f.key not in canonical:
            continue
        raw = canonical[f.key]
        if f.numeric:
            number = coerce_attachment_number(f.column, raw)
            if number is not None:
                values[f.column] = number
        else:
            if is_nil_token(raw):
                continue
            text_value = str(raw).strip()
            if text_value:
                values[f.column] = text_value
    return values
