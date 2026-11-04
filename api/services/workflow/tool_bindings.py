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
- a bound parameter is NOT in the function schema the model sees (ADR-074 amendment of
  2026-11-04: kept optional and told « leave it out », DeepSeek wrote every one of them
  anyway on the owner's two calls - 1.1 s and 1.75 s of arguments the runtime then
  overwrote);
- at the call, the bound value comes from the extracted variables (or the call's context
  for the call's own values); a datetime variable is split into its date or time part; a
  value missing on both sides stops the call before any request leaves, with an error
  naming the field, so the model asks the caller (the handler first waits for a pending
  extraction - the step's values are extracted in the background when it is left);
- the confirmation token is the runtime's: the proposal a write tool answered is
  remembered per tool and call, the model's `confirm` says « yes » (any word) and the
  runtime puts the token in - 80 characters the model never sees nor writes
  (`remember_proposal` hands the model the proposal without it, `confirm_argument`).
Unset = stock: the bindings are ignored and the model's arguments go as they are.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

from loguru import logger

CALL_VARIABLES = ("caller_number",)
EXTRACTION_WAIT_SECONDS = 3.0  # a step's values are extracted when it is left, in the background
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


CONFIRM = "confirm"
CONFIRM_DESCRIPTION = (
    "Leave empty to get the read-back. Only after the caller explicitly said yes to the "
    "read-back, call again with 'yes' here."
)
_TOKEN = re.compile(r"^p\d+\.[^.]+\.[^.]+\.[0-9a-f]{16,}\.")


def bound_schema(
    properties: dict[str, Any], required: list[str], bindings: dict[str, dict]
) -> tuple[dict[str, Any], list[str]]:
    """The model's view of a tool: a bound parameter is gone - the platform fills it - and
    the confirmation asks for a word, not the token."""
    if not bindings_on():
        return properties, required
    out = {name: prop for name, prop in properties.items() if name not in bindings}
    if CONFIRM in out and isinstance(out[CONFIRM], dict):
        out[CONFIRM] = {**out[CONFIRM], "description": CONFIRM_DESCRIPTION}
    return out, [r for r in required if r not in bindings]


CONFIRM_MESSAGE = (
    "Nothing was done yet. Read `read_back` to the caller word for word; only after an "
    "explicit yes call this tool again with confirm: 'yes'."
)


def remember_proposal(memory: dict[str, str], tool_uuid: str | None, result: Any) -> Any:
    """Keep the confirmation token a write tool's proposal answered (the executor's result:
    {"status": "success", "data": {"status": "proposal", "confirm": ...}}) and hand the model
    a result without it - on the owner's run 11 (2026-11-04) the model copied the token
    from the result, 1.2 s of streaming, although asked for a word. Any other answer of
    that tool forgets the proposal."""
    if not bindings_on() or not tool_uuid:
        return result
    data = result.get("data") if isinstance(result, dict) else None
    token = data.get(CONFIRM) if isinstance(data, dict) and data.get("status") == "proposal" else None
    if isinstance(token, str) and token:
        memory[str(tool_uuid)] = token
        shown = {**data, CONFIRM: "yes"}
        if "message" in shown:
            shown["message"] = CONFIRM_MESSAGE
        return {**result, "data": shown}
    memory.pop(str(tool_uuid), None)
    return result


def confirm_argument(
    memory: dict[str, str], tool_uuid: str | None, arguments: dict[str, Any]
) -> dict[str, Any]:
    """The model's `confirm` as the gateway takes it: empty stays a proposal, a token the
    model copied is kept, any other word becomes the remembered token - or empty (a new
    proposal) when no proposal was answered yet."""
    if not bindings_on() or CONFIRM not in (arguments or {}):
        return dict(arguments or {})
    out = dict(arguments)
    value = out.get(CONFIRM)
    if not isinstance(value, str) or not value.strip():
        return out
    if _TOKEN.match(value.strip()):
        return out
    token = memory.get(str(tool_uuid)) if tool_uuid else None
    out[CONFIRM] = token or ""
    return out


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
