"""P-21 (ADR-002): digit groups joined by dashes are said as groups, never as a sum.

Azure's fr-CH voice reads « 1-5-2 » as « un moins cinq moins deux » and its de-CH voice
as « eins bis fünf bis zwei » (heard on 2026-09-25 by synthesising the agent's sentence
and transcribing it back, after the owner's call 675); a comma is a short pause in every
language. A chain of three or more digit groups (« 1-5-2 », « 2026-02-132 ») is never a
range; two groups (« 9-12 ») may be a range and are left to the voice.
SVP_TTS_DIGIT_GROUPS=digits (the owner's rule of 2026-09-25: a reference is said digit by
digit) says « 2026-02-147 » as « 2, 0, 2, 6. 0, 2. 1, 4, 7 » - digits apart, a pause between
groups, the one written form all four voices said digit by digit (spaces alone made de-CH
say « zweitausendzweihundertzwei »); =comma says the groups (« 2026, 02, 147 »); unset =
stock behaviour.
"""

import os
import re

from pipecat.utils.text.base_text_filter import BaseTextFilter

_DASH = r"[  ]?[-‐‑‒–][  ]?"
_CHAIN = re.compile(rf"(?<!\d)\d+(?:{_DASH}\d+){{2,}}(?!\d)")
# CF-116 (D6b, ADR-059 decision 5): a reference of one to three letters and digits joined
# by a dash (« R-1044 », « AN-00132 ») - the voices read « R moins mille quarante-quatre »
_LETTERED = re.compile(rf"(?<![\w-])([A-Z]{{1,3}}){_DASH}(\d{{2,}}(?:{_DASH}\d+)*)(?![\w-])")


def said_as_groups(text: str) -> str:
    """« Le numéro est 1-5-2. » -> « Le numéro est 1, 5, 2. »"""
    return _CHAIN.sub(lambda chain: re.sub(_DASH, ", ", chain.group(0)), text)


def said_digit_by_digit(text: str) -> str:
    """« Le devis 2026-02-147. » -> « Le devis 2, 0, 2, 6. 0, 2. 1, 4, 7. »;
    « R-1044 » -> « R, 1, 0, 4, 4 » (CF-116, D6b)."""

    def spell(chain: re.Match) -> str:
        groups = re.split(_DASH, chain.group(0))
        if all(len(g) == 1 for g in groups):  # « 1-5-2 »: already one digit per group
            return ", ".join(groups)
        return ". ".join(", ".join(g) for g in groups)

    def spell_lettered(ref: re.Match) -> str:
        letters = ", ".join(ref.group(1))
        digits = ". ".join(", ".join(g) for g in re.split(_DASH, ref.group(2)))
        return f"{letters}, {digits}"

    return _CHAIN.sub(spell, _LETTERED.sub(spell_lettered, text))


class DigitGroupsTextFilter(BaseTextFilter):
    def __init__(self, mode: str = "comma"):
        self._say = said_digit_by_digit if mode == "digits" else said_as_groups

    async def update_settings(self, settings):
        pass

    async def filter(self, text: str) -> str:
        return self._say(text)

    async def handle_interruption(self):
        pass

    async def reset_interruption(self):
        pass


def svp_text_filters() -> list[BaseTextFilter]:
    """The platform's filters for every TTS service (after the stock XML tag filter): what is
    not for the caller dropped first (P-47), then digit groups said as the owner's rule says."""
    from api.services.pipecat.spoken_text_guard import SpokenTextGuardFilter, spoken_text_guard_on

    filters: list[BaseTextFilter] = []
    if spoken_text_guard_on():
        filters.append(SpokenTextGuardFilter())
    mode = (os.getenv("SVP_TTS_DIGIT_GROUPS") or "").strip().lower()
    if mode in ("comma", "digits"):
        filters.append(DigitGroupsTextFilter(mode))
    return filters
