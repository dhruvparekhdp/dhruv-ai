"""Validate instances against the JSON Schema subset Pydantic emits.

Enough for trading-v1: objects, arrays, enums, patterns, date-time, anyOf,
$ref, and additionalProperties. Not a general JSON Schema implementation.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


class SchemaError(ValueError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


def validate_instance(instance: Any, schema: dict[str, Any]) -> None:
    defs = schema.get("$defs", {})
    _check(instance, schema, defs, "$")


def _check(instance: Any, schema: dict[str, Any], defs: dict[str, Any], path: str) -> None:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name not in defs:
            raise SchemaError(path, f"unresolved ref {schema['$ref']}")
        _check(instance, defs[name], defs, path)
        return

    if "anyOf" in schema:
        errors: list[str] = []
        for option in schema["anyOf"]:
            try:
                _check(instance, option, defs, path)
                return
            except SchemaError as exc:
                errors.append(str(exc))
        raise SchemaError(path, "no anyOf branch matched")

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(path, "value not in enum")

    expected = schema.get("type")
    if expected is not None and not _type_ok(instance, expected):
        raise SchemaError(path, f"expected {expected}")

    if expected == "object":
        if not isinstance(instance, dict):
            raise SchemaError(path, "expected object")
        props: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                raise SchemaError(f"{path}.{key}", "required")
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in props:
                _check(value, props[key], defs, f"{path}.{key}")
            elif additional is False:
                raise SchemaError(f"{path}.{key}", "additional property")
            elif isinstance(additional, dict):
                _check(value, additional, defs, f"{path}.{key}")
        return

    if expected == "array":
        if not isinstance(instance, list):
            raise SchemaError(path, "expected array")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _check(item, item_schema, defs, f"{path}[{index}]")
        return

    if expected == "string" and isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(path, "shorter than minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise SchemaError(path, "longer than maxLength")
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            raise SchemaError(path, "does not match pattern")
        if schema.get("format") == "date-time":
            text = instance[:-1] + "+00:00" if instance.endswith("Z") else instance
            try:
                datetime.fromisoformat(text)
            except ValueError as exc:
                raise SchemaError(path, "not a date-time") from exc
        return

    if expected == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(path, "below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaError(path, "above maximum")


def _type_ok(instance: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "null":
        return instance is None
    return False
