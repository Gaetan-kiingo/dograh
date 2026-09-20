"""P-13 (Swiss Voice Platform, ADR-002 / ADR-014): provider-key references.

A provider ``api_key`` of the form ``secretref:SVP_PROVIDER_<NAME>`` is replaced,
in memory, by the value of that environment variable. The database, its backups
and the API responses only ever hold the reference.

Only names starting with ``SVP_PROVIDER_`` are resolvable: a configuration's
``base_url`` decides where a key is sent, so a reference must never be able to
name another variable. An unknown or disallowed reference is an error; it is
never passed on as if it were a key. Literal keys are untouched (stock behavior).
"""

from __future__ import annotations

import os
import re

PREFIX = "secretref:"
_ALLOWED_NAME = re.compile(r"^SVP_PROVIDER_[A-Z0-9_]{1,64}$")
_SECRET_FIELDS = ("api_key",)
_SECTIONS = ("llm", "stt", "tts", "embeddings", "realtime")


class SecretReferenceError(ValueError):
    """Message is safe to show: it names the reference, never a value."""


def is_reference(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def resolve_value(value):
    """Reference -> secret; anything else is returned unchanged."""
    if isinstance(value, list):
        return [resolve_value(item) for item in value]
    if not is_reference(value):
        return value
    name = value[len(PREFIX) :]
    if not _ALLOWED_NAME.match(name):
        raise SecretReferenceError(
            f"secret reference {name!r} is not allowed (only SVP_PROVIDER_* names)"
        )
    secret = os.environ.get(name, "")
    if not secret:
        raise SecretReferenceError(
            f"secret reference {name!r} is not set in the runtime's environment"
        )
    return secret


def resolve_configuration(configuration):
    """A copy of an effective configuration with every reference resolved.

    The argument is left untouched, so what callers persist or display keeps the
    reference."""
    if configuration is None:
        return configuration
    updates = {}
    for section_name in _SECTIONS:
        section = getattr(configuration, section_name, None)
        if section is None:
            continue
        changed = {
            field: resolve_value(getattr(section, field))
            for field in _SECRET_FIELDS
            if is_reference(getattr(section, field, None))
            or (
                isinstance(getattr(section, field, None), list)
                and any(is_reference(v) for v in getattr(section, field))
            )
        }
        if changed:
            updates[section_name] = section.model_copy(update=changed)
    return configuration.model_copy(update=updates) if updates else configuration
