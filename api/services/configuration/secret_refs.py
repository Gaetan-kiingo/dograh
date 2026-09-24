"""P-13 / P-17 (Swiss Voice Platform, ADR-002 / ADR-014 / ADR-031): provider-key
references.

A provider ``api_key`` of the form ``secretref:SVP_PROVIDER_<NAME>`` is replaced,
in memory, by the key. The database, its backups and the API responses only ever
hold the reference.

Where the key comes from (P-17):
- ``SVP_KEY_SERVICE_URL`` set: the runtime asks the platform once per call
  (``POST <url>/v1/internal/provider-keys``, bearer ``SVP_KEY_SERVICE_TOKEN``, the
  organisation, workflow, run and the names needed). The values are kept in memory
  for the run and forgotten when it completes (``forget_run``); a text session,
  which builds its configuration per message, finds them there. An unreachable
  platform fails the call closed: nothing is remembered across runs.
- unset: the runtime's own environment (P-13, stock behaviour of the fork).

Only names starting with ``SVP_PROVIDER_`` are resolvable: a configuration's
``base_url`` decides where a key is sent, so a reference must never be able to
name another variable. An unknown or disallowed reference is an error; it is
never passed on as if it were a key. Literal keys are untouched (stock behavior).
"""

from __future__ import annotations

import os
import re
import time
from collections import OrderedDict

import httpx
from loguru import logger

PREFIX = "secretref:"
_ALLOWED_NAME = re.compile(r"^SVP_PROVIDER_[A-Z0-9_]{1,64}$")
_SECRET_FIELDS = ("api_key",)
_SECTIONS = ("llm", "stt", "tts", "embeddings", "realtime")

KEY_SERVICE_TIMEOUT = 5.0
# A run's keys live here from call setup to completion; bounded, and swept by age
# should a completion hook never fire (a crashed pipeline).
_RUN_KEYS_MAX = 4096
_RUN_KEYS_MAX_AGE = 6 * 3600
_run_keys: OrderedDict[int, tuple[dict[str, str], float]] = OrderedDict()


class SecretReferenceError(ValueError):
    """Message is safe to show: it names the reference, never a value."""


def is_reference(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def reference_name(value: str) -> str:
    name = value[len(PREFIX) :]
    if not _ALLOWED_NAME.match(name):
        raise SecretReferenceError(
            f"secret reference {name!r} is not allowed (only SVP_PROVIDER_* names)"
        )
    return name


def key_service_url() -> str:
    return os.getenv("SVP_KEY_SERVICE_URL", "").strip().rstrip("/")


def _from_environment(name: str) -> str:
    secret = os.environ.get(name, "")
    if not secret:
        raise SecretReferenceError(
            f"secret reference {name!r} is not set in the runtime's environment"
        )
    return secret


def resolve_value(value, keys: dict[str, str] | None = None):
    """Reference -> secret; anything else is returned unchanged. ``keys`` are the
    values fetched for this run (P-17); without them the environment answers."""
    if isinstance(value, list):
        return [resolve_value(item, keys) for item in value]
    if not is_reference(value):
        return value
    name = reference_name(value)
    if keys is not None:
        if name not in keys:
            raise SecretReferenceError(
                f"secret reference {name!r} was not provided by the platform for this run"
            )
        return keys[name]
    return _from_environment(name)


def _names_in(configuration) -> list[str]:
    names: list[str] = []
    for section_name in _SECTIONS:
        section = getattr(configuration, section_name, None)
        if section is None:
            continue
        for field in _SECRET_FIELDS:
            value = getattr(section, field, None)
            values = value if isinstance(value, list) else [value]
            for v in values:
                if is_reference(v):
                    name = reference_name(v)
                    if name not in names:
                        names.append(name)
    return names


def _sweep(now: float) -> None:
    while _run_keys and (
        len(_run_keys) > _RUN_KEYS_MAX
        or now - next(iter(_run_keys.values()))[1] > _RUN_KEYS_MAX_AGE
    ):
        _run_keys.popitem(last=False)


async def _fetch(
    names: list[str],
    *,
    organization_id: int | None,
    workflow_id: int | None,
    run_id: int | None,
) -> dict[str, str]:
    """One request to the platform for this run's keys (ADR-031 point 3)."""
    url = key_service_url()
    token = os.getenv("SVP_KEY_SERVICE_TOKEN", "").strip()
    if not token:
        raise SecretReferenceError("SVP_KEY_SERVICE_TOKEN is not set in the runtime's environment")
    body = {
        "organization_id": organization_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "names": names,
    }
    headers = {"Authorization": f"Bearer {token}"}
    traceparent = os.getenv("SVP_TRACEPARENT", "")  # set by the platform per process, if any
    if traceparent:
        headers["traceparent"] = traceparent
    try:
        async with httpx.AsyncClient(timeout=KEY_SERVICE_TIMEOUT) as client:
            response = await client.post(f"{url}/v1/internal/provider-keys", json=body, headers=headers)
    except httpx.HTTPError as e:
        raise SecretReferenceError(
            f"the platform's key service could not be reached ({type(e).__name__}); "
            "the call cannot start"
        ) from e
    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("detail", {}).get("code", ""))
        except ValueError:
            pass
        raise SecretReferenceError(
            f"the platform refused the provider keys for this run ({response.status_code} {detail})"
        )
    keys = response.json().get("keys") or {}
    missing = [n for n in names if n not in keys]
    if missing:
        raise SecretReferenceError(f"the platform did not provide {missing}")
    return {name: str(entry["value"]) for name, entry in keys.items()}


async def keys_for_run(
    configuration,
    *,
    organization_id: int | None,
    workflow_id: int | None,
    run_id: int | None,
) -> dict[str, str] | None:
    """The keys this run needs: remembered for the run after the first request.
    None when the key service is off (the environment answers, P-13)."""
    if not key_service_url():
        return None
    names = _names_in(configuration)
    if not names:
        return {}
    now = time.monotonic()
    if run_id is not None:
        held = _run_keys.get(run_id)
        if held is not None and all(n in held[0] for n in names):
            _run_keys.move_to_end(run_id)
            return held[0]
    keys = await _fetch(
        names, organization_id=organization_id, workflow_id=workflow_id, run_id=run_id
    )
    if run_id is not None:
        merged = {**(_run_keys.get(run_id, ({}, now))[0]), **keys}
        _run_keys[run_id] = (merged, now)
        _run_keys.move_to_end(run_id)
        _sweep(now)
        return merged
    return keys


def forget_run(run_id: int | None) -> None:
    """The run completed: its keys leave memory (ADR-031 point 3)."""
    if run_id is not None and _run_keys.pop(run_id, None) is not None:
        logger.debug(f"P-17: provider keys of run {run_id} forgotten")


def held_runs() -> int:
    """How many runs currently hold keys (tests, diagnostics)."""
    return len(_run_keys)


def resolve_configuration(configuration, keys: dict[str, str] | None = None):
    """A copy of an effective configuration with every reference resolved.

    The argument is left untouched, so what callers persist or display keeps the
    reference. ``keys`` are the run's values from the platform (P-17); None means
    the environment (P-13)."""
    if configuration is None:
        return configuration
    updates = {}
    for section_name in _SECTIONS:
        section = getattr(configuration, section_name, None)
        if section is None:
            continue
        changed = {
            field: resolve_value(getattr(section, field), keys)
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
