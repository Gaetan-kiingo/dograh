"""P-47 (Swiss Voice Platform, ADR-002; D6e, CF-073): what is not for a caller's ears is not
spoken.

A sentence the model wrote is dropped before the voice when it carries one of the signs seen on
the owner's calls and the probes - never a rewrite, the sentence whole:
- the model's own markup or a tool call written as text: « <|start|>assistant<|channel|> »
  (gpt-oss, 2026-10-31), « <call:vers_fin/> », « [ends call] », « call: default_api.… »
  (Gemini, CF-133), code fences;
- the platform's prompt: a section header in brackets (« [ALREADY KNOWN] », « [ROLE] »), the
  runtime's kick-off (« Let's begin. The caller is on the line. » - DeepSeek, CF-234);
- the caller spoken of in the third person, the sign of reasoning said aloud: « L'utilisateur dit
  … » (Kimi, CF-230), « the user », « der Benutzer », « l'utente ».
SVP_SPOKEN_TEXT_GUARD=on; unset = stock. A step change those words were holding still runs:
P-47's other half (SVP_HELD_TRANSITION_TIMEOUT_MS, pipecat's OpenAI service).
"""

import os
import re

from loguru import logger
from pipecat.utils.text.base_text_filter import BaseTextFilter

_SIGNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("model markup", re.compile(r"<\||\|>|<call:|```")),
    ("a tool call as text", re.compile(
        r"\[\s*(?:ends?\s+call|calling\s+[\w.]+|call\s*:)|\b(?:default_api|functions)\.\w+\s*\(",
        re.IGNORECASE,
    )),
    ("a prompt section", re.compile(r"\[[A-Z][A-Z _-]{3,}\]")),
    ("the runtime's kick-off", re.compile(
        r"\blet'?s begin\b|\bthe caller is on the line\b", re.IGNORECASE
    )),
    ("the caller in the third person", re.compile(
        r"\bl[’']utilisat(?:eur|rice)\b|\bthe user\b|\b(?:der|des|dem|den) Benutzers?\b"
        r"|\bl[’']utente\b",
        re.IGNORECASE,
    )),
)


def sign_in(text: str) -> str | None:
    """The first sign that ``text`` is not for the caller, by name; None for speech."""
    for name, pattern in _SIGNS:
        if pattern.search(text):
            return name
    return None


class SpokenTextGuardFilter(BaseTextFilter):
    async def update_settings(self, settings):
        pass

    async def filter(self, text: str) -> str:
        sign = sign_in(text)
        if sign is None:
            return text
        # what was dropped is never logged (it may carry what the caller said): its kind only
        logger.warning(f"P-47: a sentence was not spoken ({sign}, {len(text)} characters)")
        return ""

    async def handle_interruption(self):
        pass

    async def reset_interruption(self):
        pass


def spoken_text_guard_on() -> bool:
    return (os.getenv("SVP_SPOKEN_TEXT_GUARD") or "").strip().lower() in ("on", "1", "true")
