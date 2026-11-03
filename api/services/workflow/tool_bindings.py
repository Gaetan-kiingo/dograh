"""P-49 (Swiss Voice Platform, ADR-074): typed variable bindings for a tool's parameters.

The platform compiles, in a version's configurations, which parameters of a tool are filled
from the call's variables - a collected item (its key and its type) or one of the call's own
values - instead of being written by the model:

    configurations["svp_tool_bindings"] = {
        "<tool uuid>": {"date": {"variable": "creneau", "type": "datetime", "part": "date",
                                 "label": "Créneau choisi"}, ...}}

Why: on the owner's booking call (2026-11-03) the model spent 1.8 s writing the booking's
fields - date, time, name, number - that the caller had already given and confirmed, and
copied them again by hand. With `SVP_TOOL_BINDINGS=on`:
- the function schema the model sees keeps a bound parameter as OPTIONAL, described as
  filled by the platform, so the model leaves it out (and may still give it when it must);
- at the call, the bound value from the extracted variables (or the call's context for the
  call's own values) replaces whatever the model wrote; a datetime variable is split into
  its date or time part; a value that is missing on both sides stops the call before any
  request leaves, with an error naming the field, so the model asks the caller.
Unset = stock: the bindings are ignored and the model's arguments go as they are.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

from loguru import logger

CALL_VARIABLES = ("caller_number",)
_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::\d{2})?)?")


def bindings_on() -> bool:
    return os.environ.get("SVP_TOOL_BINDINGS", "").strip().lower() in ("on", "1", "true")


def bindings_for(configurations: dict[str, Any] | None, tool_uuid: str | None) -> dict[str, dict]:
    """The bindings of one runtime tool from a run's configurations; {} when none or off."""
    if not bindings_on() or not tool_uuid:
        return {}
    table = (configurations or {}).get("svp_tool_bindings") or {}
    bound = table.get(str(tool_uuid)) if isinstance(table, dict) else None
    return {str(k): dict(v) for k, v in bound.items()} if isinstance(bound, dict) else {}


def bound_schema(
    properties: dict[str, Any], required: list[str], bindings: dict[str, dict]
) -> tuple[dict[str, Any], list[str]]:
    """The model's view of a tool: a bound parameter stays, optional, told to be left out."""
    if not bindings:
        return properties, required
    out = {}
    for name, prop in properties.items():
        binding = bindings.get(name)
        if binding:
            label = str(binding.get("label") or binding.get("variable") or name)
            prop = {**prop, "description": f"Filled by the platform ({label}); leave it out."}
        out[name] = prop
    return out, [r for r in required if r not in bindings]


def _convert(value: Any, binding: dict) -> Any | None:
    """The value as the parameter takes it; None when it cannot be used."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    kind, part = str(binding.get("type") or "text"), binding.get("part")
    if kind == "datetime":
        match = _ISO.match(str(value).strip())
        if not match:
            return None  # the caller's own words, not a date the system can take
        if part == "date":
            return match.group(1)
        if part == "time":
            return f"{match.group(2)}:{match.group(3)}" if match.group(2) else None
        try:
            return datetime.fromisoformat(str(value).strip().replace(" ", "T")).isoformat()
        except ValueError:
            return str(value).strip()
    if kind == "number":
        try:
            return float(value) if "." in str(value) else int(value)
        except (TypeError, ValueError):
            return None
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "oui", "ja", "sì", "si", "1")
    return str(value).strip()


def apply_bindings(
    bindings: dict[str, dict],
    arguments: dict[str, Any],
    extracted: dict[str, Any] | None,
    call_context: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """The arguments with every bound parameter filled from the call's variables; the model's
    value is kept only when nothing was extracted; returns (arguments, missing labels)."""
    if not bindings:
        return dict(arguments or {}), []
    out = dict(arguments or {})
    missing: list[str] = []
    extracted = extracted if isinstance(extracted, dict) else {}
    call_context = call_context if isinstance(call_context, dict) else {}
    for parameter, binding in bindings.items():
        key = str(binding.get("variable") or "")
        raw = call_context.get(key) if key in CALL_VARIABLES else extracted.get(key)
        value = _convert(raw, binding)
        if value is not None:
            out[parameter] = value
        elif out.get(parameter) in (None, ""):
            missing.append(str(binding.get("label") or key))
    return out, missing
