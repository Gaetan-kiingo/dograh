"""P-33 (Swiss Voice Platform, ADR-002 / ADR-052): the platform told a call's life, not
only its end.

With ``SVP_CALL_EVENTS_URL`` set, the runtime posts to it, with the bearer
``SVP_KEY_SERVICE_TOKEN`` (its credential to the platform, P-17), one JSON body per
moment of a call::

    {"kind": "started", "organization_id", "workflow_id", "run_id", "at", ...}

- ``started``: the call is answered - bidirectional media is up (the browser's peer or
  the phone leg's media connected): the pinned definition, the platform's publish marker
  frozen in its configurations (``svp_publish``), the caller's and the called number as
  the telephony leg gave them, the channel and whether the policy records.
- ``disclosure_played``: the opening - the words the platform compiled, AI disclosure
  included - has been spoken to its end: a hash of the text spoken, the delay from media
  to the first sound (PERF-002), ``outcome`` played.
- ``recording_started``: the audio capture began (at the answer when the policy records
  without a question; on the caller's yes when the policy asks first - CF-007).
- ``language_switched``: the caller spoke another of the agent's languages and the agent
  switched to it (P-07, ADR-058): ``language``, ``previous_language``.
- ``transfer_started`` / ``transferred`` / ``transfer_failed`` (P-40, ADR-060): the handoff
  to a person - the destination's name (the business's word, never a number), the mode,
  the seconds from the end of the pre-transfer sentence to the dial; the destination
  answered; or why it did not.
- ``ended``: the run is over, on EVERY path: the pipeline finished (the disposition),
  a failure before the pipeline started, a caller gone during the phone leg's setup,
  a carrier's terminal status, a run found abandoned by the sweep after a crash.

The ``ended`` of a run whose opening was never spoken says ``disclosure: not_played``.
P-20's ``completed`` notice (``SVP_CALL_ENDED_URL``, after the artifacts are uploaded)
is unchanged and still triggers the platform's ingestion.

Delivery: a post is made off the pipeline (a task), with three attempts a second or two
apart; a failure is logged, never raised - the platform's own sweep finalises what it
never heard about (CALL-015). Unset = stock behaviour (nothing is sent).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

POST_TIMEOUT = 5.0
ATTEMPT_DELAYS = (0.0, 1.0, 3.0)
# a call found still open this long after it was created, with no process speaking for
# it, was lost to a crash (the pipeline's own ceilings are far below this)
STALE_RUN_AFTER_SECONDS = int(os.getenv("SVP_STALE_RUN_AFTER_SECONDS", "7200") or 7200)
TEXT_MODES = ("textchat", "chat")
# the codec of a leg in this runtime, by channel: Twilio's media streams and Asterisk's
# external media are G.711 mu-law at 8 kHz; the browser negotiates Opus
CODEC_BY_MODE = {"twilio": "PCMU/8000", "ari": "PCMU/8000", "smallwebrtc": "opus/48000"}

_tasks: set[asyncio.Task] = set()
# per run, in this process: when media came up, when the first sound went out, the
# text of the opening, whether it was spoken to its end
_runs: dict[int, dict[str, Any]] = {}


def events_url() -> str:
    return os.getenv("SVP_CALL_EVENTS_URL", "").strip()


def enabled() -> bool:
    return bool(events_url())


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def text_hash(text: str) -> str:
    """The spoken text's fingerprint - never the text itself leaves with the event."""
    return hashlib.sha256(" ".join(str(text).split()).encode()).hexdigest()[:16]


def state(run_id: int) -> dict[str, Any]:
    return _runs.setdefault(int(run_id), {})


def forget(run_id: int) -> None:
    _runs.pop(int(run_id), None)


async def _post(body: dict[str, Any]) -> bool:
    url = events_url()
    token = os.getenv("SVP_KEY_SERVICE_TOKEN", "").strip()
    if not url:
        return False
    if not token:
        logger.warning("SVP_CALL_EVENTS_URL is set but SVP_KEY_SERVICE_TOKEN is not: nothing sent")
        return False
    last = ""
    for delay in ATTEMPT_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=POST_TIMEOUT) as client:
                response = await client.post(
                    url, json=body, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.HTTPError as e:
            last = type(e).__name__
            continue
        if response.status_code < 300:
            return True
        if 400 <= response.status_code < 500:
            # the platform refused it for what it is: saying it again changes nothing
            logger.warning(
                f"P-33: {body.get('kind')} for run {body.get('run_id')} refused "
                f"(HTTP {response.status_code})"
            )
            return False
        last = f"HTTP {response.status_code}"
    logger.warning(
        f"P-33: {body.get('kind')} for run {body.get('run_id')} not delivered ({last}); "
        "the platform's sweep finalises the call"
    )
    return False


def _track(coro) -> asyncio.Task | None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return None
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def _run_body(run_id: int) -> dict[str, Any] | None:
    """What every event says about the run: organisation, workflow, pinned definition."""
    from api.db import db_client

    try:
        workflow_run, organization_id = await db_client.get_workflow_run_with_context(run_id)
    except Exception as e:  # noqa: BLE001 - a notice never breaks a call
        logger.warning(f"P-33: run {run_id} not read ({type(e).__name__})")
        return None
    if not workflow_run or not organization_id:
        return None
    definition = getattr(workflow_run, "definition", None)
    configurations = (getattr(definition, "workflow_configurations", None) or {}) if definition else {}
    initial = getattr(workflow_run, "initial_context", None) or {}
    gathered = getattr(workflow_run, "gathered_context", None) or {}
    mode = str(getattr(workflow_run, "mode", "") or "")
    return {
        # P-35 (TEL-006): the carrier's own call id, the configuration the call came
        # through and the codec of the leg - fixed by the channel in this runtime
        "carrier_call_id": gathered.get("call_id"),
        "telephony_configuration_id": initial.get("telephony_configuration_id"),
        "codec": CODEC_BY_MODE.get(mode),
        "organization_id": organization_id,
        "workflow_id": workflow_run.workflow_id,
        "run_id": run_id,
        "definition_id": workflow_run.definition_id,
        "definition_version": getattr(definition, "version_number", None),
        "publish": configurations.get("svp_publish"),
        "recording_enabled": configurations.get("recording_enabled"),
        "recording_consent": configurations.get("svp_recording_consent"),
        "mode": getattr(workflow_run, "mode", None),
        "direction": initial.get("direction"),
        "provider": initial.get("provider"),
        "caller_number": initial.get("caller_number"),
        "called_number": initial.get("called_number"),
        # P-35 amended (D3b, ADR-056 amendment of 2026-10-27; TEL-007): the number the
        # carrier says the call was forwarded from - Twilio's ForwardedFrom
        "forwarded_from": forwarded_from_of(workflow_run),
    }


def forwarded_from_of(workflow_run: Any) -> str | None:
    """Twilio's `ForwardedFrom` on the inbound webhook the run was created from, if any."""
    logs = getattr(workflow_run, "logs", None) or {}
    raw = ((logs.get("inbound_webhook") or {}).get("raw_webhook_data") or {}) if isinstance(logs, dict) else {}
    value = str(raw.get("ForwardedFrom") or raw.get("forwarded_from") or "").strip()
    return value[:32] or None


def carrier_status(run_id: int, *, status: str, duration: Any = None, call_id: str | None = None) -> None:
    """P-35 amended (D3b): the carrier's terminal status of a CONNECTED call - the
    platform keeps it on the session even after the call ended on its own path."""
    notify(
        "carrier_status", run_id,
        carrier_status=str(status or "")[:20], carrier_duration=duration, carrier_call_id=call_id,
    )


async def send(kind: str, run_id: int, **payload: Any) -> bool:
    """One event, now (awaited). Returns whether the platform took it."""
    if not enabled():
        return False
    body = await _run_body(int(run_id))
    if body is None:
        return False
    body.update({"kind": kind, "at": payload.pop("at", None) or now_iso(), **payload})
    return await _post(body)


def notify(kind: str, run_id: int, **payload: Any) -> None:
    """One event, off the caller's path: never awaited, never raises."""
    if not enabled():
        return

    async def _go() -> None:
        try:
            await send(kind, run_id, **payload)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"P-33: {kind} for run {run_id} failed ({type(e).__name__}: {e})")

    _track(_go())


# --- the moments, as the pipeline sees them ---------------------------------------------------


def media_connected(run_id: int) -> None:
    """The call is answered: media is up both ways. Says `started`."""
    s = state(run_id)
    s["media_at"] = time.monotonic()
    s["media_at_iso"] = now_iso()
    notify("started", run_id, at=s["media_at_iso"])


def opening_queued(run_id: int, text: str) -> None:
    """The words the runtime is about to speak first (the platform's compiled opening)."""
    state(run_id)["opening"] = " ".join(str(text or "").split())


def first_sound(run_id: int) -> None:
    s = state(run_id)
    if "first_sound_at" not in s:
        s["first_sound_at"] = time.monotonic()


def opening_spoken(run_id: int) -> None:
    """The first bot utterance finished: the opening, disclosure included, was heard.
    Says `disclosure_played` once."""
    s = state(run_id)
    if s.get("opening_spoken"):
        return
    s["opening_spoken"] = True
    media_at, sound_at = s.get("media_at"), s.get("first_sound_at")
    delay = round(sound_at - media_at, 3) if media_at and sound_at else None
    notify(
        "disclosure_played",
        run_id,
        outcome="played",
        text_hash=text_hash(s.get("opening", "")) if s.get("opening") else None,
        first_sound_after_media_seconds=delay,
    )


def language_switched(run_id: int, language: str, *, previous: str | None = None) -> None:
    """P-07 (ADR-058, CALL-005): the caller spoke another of the agent's languages and the
    agent switched to it. Says `language_switched` with the language heard."""
    notify("language_switched", run_id, language=language, previous_language=previous)


def transfer_started(
    run_id: int, *, destination: str, mode: str, commit_to_dial_seconds: float | None
) -> None:
    """P-40 (ADR-060, PERF-010): the destination is being dialled."""
    notify(
        "transfer_started",
        run_id,
        destination=str(destination or "")[:64],
        mode=str(mode or "")[:12],
        commit_to_dial_seconds=commit_to_dial_seconds,
    )


def transferred(run_id: int, *, destination: str) -> None:
    """P-40 (ADR-060, TEL-008): the destination answered, the caller is with a person."""
    notify("transferred", run_id, destination=str(destination or "")[:64])


def transfer_failed(run_id: int, *, destination: str, reason: str) -> None:
    """P-40 (ADR-060, TEL-010): the handoff did not happen; the agent goes on."""
    notify(
        "transfer_failed", run_id, destination=str(destination or "")[:64], reason=str(reason or "")[:40]
    )


def recording_started(run_id: int, trigger: str) -> None:
    notify("recording_started", run_id, trigger=trigger)


def ended(run_id: int, reason: str, **extra: Any) -> None:
    """The run is over. Says whether the opening was ever spoken."""
    s = state(run_id)
    outcome = "played" if s.get("opening_spoken") else "not_played"
    notify("ended", run_id, reason=str(reason or "")[:60], disclosure=outcome, **extra)
    forget(run_id)


async def ended_now(run_id: int, reason: str, **extra: Any) -> bool:
    """`ended`, awaited - for paths that are about to return (a failure before start)."""
    s = state(run_id)
    outcome = "played" if s.get("opening_spoken") else "not_played"
    forget(run_id)
    return await send("ended", run_id, reason=str(reason or "")[:60], disclosure=outcome, **extra)


async def flush(timeout: float = 10.0) -> None:
    """Wait for the posts in flight (a process about to exit)."""
    pending = [t for t in _tasks if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
