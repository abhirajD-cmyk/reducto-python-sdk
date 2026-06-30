from __future__ import annotations

import json
from typing import Any
from decimal import Decimal
from dataclasses import asdict, is_dataclass

from .models import JsonValue


def to_plain_data(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_plain_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain_data(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return to_plain_data(asdict(value))
    if hasattr(value, "to_dict"):
        return to_plain_data(value.to_dict(mode="json", exclude_unset=False))
    if hasattr(value, "model_dump"):
        return to_plain_data(value.model_dump(mode="json"))
    raise TypeError(f"Cannot convert {type(value).__name__} to JSON-compatible data")


def as_dict(value: JsonValue) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    raise TypeError("Expected a JSON object")


def json_dumps(value: JsonValue) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def read_lob(value: Any) -> Any:
    if hasattr(value, "read"):
        return value.read()
    return value


def int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, (float, str, bytes, bytearray, Decimal)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
