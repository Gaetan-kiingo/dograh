"""P-07 (Swiss Voice Platform, ADR-002 / ADR-058; CALL-005): the caller's language heard,
the agent switched.

With ``SVP_LANGUAGE_SWITCH=on`` and a workflow whose configurations carry
``svp_languages`` - one entry per language the agent answers in::

    {"fr": {"stt_language": "fr-CH", "tts_voice": "fr-FR-DeniseNeural",
            "tts_language": "fr-FR", "instruction": "Réponds en français.",
            "announce": "Je vous réponds en français."}, "de": {...}}

this processor sits after the recogniser. Azure's continuous language identification
puts the locale it heard on every transcript (pipecat's ``TranscriptionFrame.language``).
When a caller's FINAL transcript is in another admitted language than the one the agent
speaks, the switch is made once: the voice changes to that language's
(``TTSUpdateSettingsFrame``), the engine is told so its system instructions say, on this
step and every later one, to answer in that language and to open the next reply with the
announcement, and the platform is told (``language_switched``, P-33). Interim transcripts
never switch; a transcript in a language the agent does not answer in is left alone.

Unset, or no ``svp_languages``: nothing is added to the pipeline - stock behaviour."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.frames.frames import Frame, TranscriptionFrame, TTSUpdateSettingsFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

if TYPE_CHECKING:
    from api.services.workflow.pipecat_engine import PipecatEngine


def enabled() -> bool:
    return os.environ.get("SVP_LANGUAGE_SWITCH", "").strip().lower() in ("on", "1", "true")


def languages_of(configurations: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """The agent's languages from the run's workflow configurations - {} when one."""
    raw = (configurations or {}).get("svp_languages") or {}
    out: dict[str, dict[str, str]] = {}
    if isinstance(raw, dict):
        for code, entry in raw.items():
            if isinstance(entry, dict) and entry.get("stt_language"):
                out[str(code)] = {k: str(v) for k, v in entry.items()}
    return out if len(out) >= 2 else {}


def language_code(locale: str | None) -> str | None:
    """« fr-CH » -> « fr »; None when nothing."""
    if not locale:
        return None
    return str(locale).split("-")[0].lower() or None


class LanguageSwitchProcessor(FrameProcessor):
    """The switch, once per change of language, on the caller's final transcripts."""

    def __init__(
        self,
        *,
        languages: dict[str, dict[str, str]],
        current: str,
        engine: "PipecatEngine | None",
        run_id: int | None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._languages = languages
        self._current = current
        self._engine = engine
        self._run_id = run_id
        self.switches: list[str] = []

    @property
    def current(self) -> str:
        return self._current

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            heard = language_code(str(frame.language) if frame.language else None)
            if heard and heard != self._current and heard in self._languages and frame.text.strip():
                await self._switch_to(heard)
        await self.push_frame(frame, direction)

    async def _switch_to(self, code: str) -> None:
        entry = self._languages[code]
        previous = self._current
        self._current = code
        self.switches.append(code)
        logger.info(f"P-07: the caller speaks {code} (was {previous}); run {self._run_id}")
        # the voice of that language, from the next sentence on
        await self.push_frame(
            TTSUpdateSettingsFrame(
                settings={"voice": entry["tts_voice"], "language": entry["tts_language"]}
            ),
            FrameDirection.DOWNSTREAM,
        )
        if self._engine is not None:
            await self._engine.set_caller_language(
                code, instruction=entry.get("instruction", ""), announce=entry.get("announce", "")
            )
        if self._run_id is not None:
            from api.services import svp_call_events

            svp_call_events.language_switched(self._run_id, code, previous=previous)
