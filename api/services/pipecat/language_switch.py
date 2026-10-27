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

``SVP_LANGUAGE_SWITCH_MIN_WORDS`` (ADR-058 amendment of 2026-10-27, CF-190): a final
transcript shorter than that many words never switches - Azure's identification tags a
short utterance in the wrong language too often (« Oui » heard as the English « We. »,
« Deutsch. » tagged French); a caller who wants another language says a sentence
(« Do you speak English? »). Every decision is logged with the word count and the tag,
never the words. Unset = no floor, P-07 as first built.

``SVP_LANGUAGE_SWITCH_CHECK_TEXT=on`` (the same amendment, the owner's call of 01:49): the
recogniser's tag alone is not trusted - Cartesia's Ink 2 tagged « Oui, bonjour, je souhaite
prendre rendez-vous pour une coiffure. » English. The transcript's own words are counted
against a short table of function words per language (``STOP_WORDS``) and **the words
decide**: the switch is made to the language they say, the tag only logged - Cartesia's Ink 2
tags every transcript ``en`` (run 52), so an agent that trusted the tag could never come
back to French. A transcript whose words decide nothing is held. Unset = the tag decides
(stock P-07).

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


def check_text() -> bool:
    return os.environ.get("SVP_LANGUAGE_SWITCH_CHECK_TEXT", "").strip().lower() in ("on", "1", "true")


# Function words and language names that seldom cross languages (no « non », « un », « la »,
# « il », « a », which French, Italian and German share); lower-case, apostrophes kept.
STOP_WORDS: dict[str, frozenset[str]] = {
    "fr": frozenset("""je vous nous oui bonjour bonsoir merci est pour une les des du et pas ne ce
        c'est j'ai avec mon ma mes votre vos s'il plaît rendez-vous voudrais souhaite aimerais
        français anglais allemand italien parlez est-ce qu'il""".split()),
    "de": frozenset("""ich sie wir ja nein bitte danke nicht und ist ein eine einen der die das mit
        für auf termin möchte hätte gerne gern deutsch englisch französisch guten hallo grüezi
        sprechen können""".split()),
    "it": frozenset("""io lei noi sì grazie prego per una uno gli che con sono vorrei buongiorno
        buonasera appuntamento italiano inglese francese tedesco parla posso""".split()),
    "en": frozenset("""i you we yes please thank thanks hello the is are do can with for and not
        would like want appointment english french german italian speak my your this""".split()),
}


def text_language(text: str, candidates: "set[str] | frozenset[str]") -> str | None:
    """The language the transcript's own words say, among the candidates: the one with the
    most function-word hits, two at least and strictly ahead of the next - else None."""
    tokens = [t.strip(".,;:!?¿¡«»\"()") for t in text.lower().split()]
    scores = {
        code: sum(1 for t in tokens if t in STOP_WORDS.get(code, frozenset()))
        for code in candidates
    }
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked or ranked[0][1] < 2:
        return None
    if len(ranked) > 1 and ranked[1][1] >= ranked[0][1]:
        return None
    return ranked[0][0]


def min_words() -> int:
    """The floor under which a transcript never switches; 0 = none (stock P-07)."""
    try:
        return max(0, int(os.environ.get("SVP_LANGUAGE_SWITCH_MIN_WORDS", "0").strip() or 0))
    except ValueError:
        return 0


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
        self._announce_pending = False

    @property
    def current(self) -> str:
        return self._current

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            heard = language_code(str(frame.language) if frame.language else None)
            words = len(frame.text.split())
            if check_text():
                # the words decide; the tag is logged (Cartesia's is constant, Azure's wrong on
                # short utterances)
                target = text_language(frame.text, set(self._languages)) if words else None
                verdict = target or "nothing"
            else:
                target = heard
                verdict = None
            if words and target and target != self._current and target in self._languages:
                floor = min_words()
                if words < floor:
                    logger.info(
                        f"P-07: a {words}-word transcript tagged {heard} held (floor {floor} words); "
                        f"run {self._run_id}"
                    )
                else:
                    logger.info(
                        f"P-07: a {words}-word transcript tagged {heard}"
                        + (f", its words say {verdict}" if verdict else "")
                        + f": switches to {target}; run {self._run_id}"
                    )
                    await self._switch_to(target)
            elif words and verdict and heard and heard != self._current:
                logger.info(
                    f"P-07: a {words}-word transcript tagged {heard} held - its words say "
                    f"{verdict}; run {self._run_id}"
                )
            elif self._announce_pending and frame.text.strip() and self._engine is not None:
                # the reply after the switch carried the announcement; from this utterance
                # on the instructions say the language only
                self._announce_pending = False
                await self._engine.clear_language_announcement()
        await self.push_frame(frame, direction)

    async def _switch_to(self, code: str) -> None:
        entry = self._languages[code]
        previous = self._current
        self._current = code
        self.switches.append(code)
        self._announce_pending = True
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
