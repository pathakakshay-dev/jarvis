"""Validate skill arguments against a flat object schema. No code is accepted."""

from __future__ import annotations


def validate_arguments(schema: dict, arguments: object) -> dict:
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise ValueError("invalid input schema")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError("invalid input schema")
    if not isinstance(arguments, dict):
        raise ValueError("invalid arguments")
    unknown = [key for key in arguments if key not in properties]
    if unknown or schema.get("additionalProperties", False) is not False:
        if unknown:
            raise ValueError("unknown argument")
    missing = [key for key in required if key not in arguments]
    if missing:
        raise ValueError("missing argument")
    cleaned: dict = {}
    for name, value in arguments.items():
        rule = properties.get(name)
        if not isinstance(rule, dict):
            raise ValueError("invalid input schema")
        cleaned[name] = _coerce(rule, value)
    return cleaned


def _coerce(rule: dict, value: object) -> object:
    kind = rule.get("type")
    if kind == "string":
        if not isinstance(value, str):
            raise ValueError("invalid argument")
        limit = rule.get("maxLength", 2000)
        if not isinstance(limit, int) or len(value) > limit:
            raise ValueError("invalid argument")
        return value
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid argument")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid argument")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("invalid argument")
        return value
    raise ValueError("invalid input schema")
