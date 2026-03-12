"""Config schema validation for Penny.

Validates the structure and types of config.yaml after YAML parsing.
Returns human-readable error messages so users can fix problems quickly.
No external dependencies -- uses only stdlib types.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Schema definition
#
# Each key maps to a dict with:
#   "type"     - expected Python type(s)
#   "required" - bool (default False)
#   "children" - nested schema for dict-type values
#   "item_schema" - schema for each item in a list
# ---------------------------------------------------------------------------

_PROJECT_SCHEMA: dict[str, Any] = {
    "path": {"type": str, "required": True},
    "priority": {"type": (int, float)},
}

_TRIGGER_SCHEMA: dict[str, Any] = {
    "min_capacity_percent": {"type": (int, float)},
    "max_days_remaining": {"type": (int, float)},
}

_WORK_SCHEMA: dict[str, Any] = {
    "max_agents_per_run": {"type": int},
    "agent_permissions": {"type": str},
    "allowed_tools": {"type": list},
    "task_priority_levels": {"type": list},
}

_NOTIFICATIONS_SCHEMA: dict[str, Any] = {
    "spawn": {"type": bool},
    "completion": {"type": bool},
}

_SERVICE_SCHEMA: dict[str, Any] = {
    "keep_alive": {"type": bool},
    "launch_at_login": {"type": bool},
}

_ROOT_SCHEMA: dict[str, Any] = {
    "projects": {"type": list, "item_schema": _PROJECT_SCHEMA},
    "trigger": {"type": dict, "children": _TRIGGER_SCHEMA},
    "work": {"type": dict, "children": _WORK_SCHEMA},
    "notifications": {"type": dict, "children": _NOTIFICATIONS_SCHEMA},
    "service": {"type": dict, "children": _SERVICE_SCHEMA},
    "plugins": {"type": dict},
    "stats_cache_path": {"type": str},
}


def validate_config(config: dict[str, Any]) -> list[str]:
    """Validate a parsed config dict against the schema.

    Returns a list of human-readable error strings. An empty list means
    the config is valid.
    """
    if not isinstance(config, dict):
        return [f"Config must be a mapping (dict), got {type(config).__name__}"]

    errors: list[str] = []
    _validate_dict(config, _ROOT_SCHEMA, prefix="", errors=errors)
    return errors


def _validate_dict(
    data: dict[str, Any],
    schema: dict[str, Any],
    prefix: str,
    errors: list[str],
) -> None:
    """Recursively validate a dict against a schema."""
    for key, rules in schema.items():
        full_key = f"{prefix}{key}" if prefix else key

        if key not in data:
            if rules.get("required"):
                errors.append(f"Missing required key: {full_key}")
            continue

        value = data[key]
        expected_type = rules.get("type")

        if expected_type is not None and value is not None:
            if not isinstance(value, expected_type):
                type_name = (
                    expected_type.__name__
                    if isinstance(expected_type, type)
                    else " or ".join(t.__name__ for t in expected_type)
                )
                errors.append(
                    f"{full_key}: expected {type_name}, "
                    f"got {type(value).__name__} ({value!r})"
                )
                continue

        # Validate nested dict
        children = rules.get("children")
        if children and isinstance(value, dict):
            _validate_dict(value, children, prefix=f"{full_key}.", errors=errors)

        # Validate list items
        item_schema = rules.get("item_schema")
        if item_schema and isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    _validate_dict(
                        item, item_schema, prefix=f"{full_key}[{i}].", errors=errors
                    )
                else:
                    errors.append(
                        f"{full_key}[{i}]: expected a mapping, "
                        f"got {type(item).__name__}"
                    )
