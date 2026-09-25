"""P-21 (ADR-002): digit groups joined by dashes are said as groups, never as a sum.

Azure's fr-CH voice reads « 1-5-2 » as « un moins cinq moins deux » and its de-CH voice
as « eins bis fünf bis zwei » (heard on 2026-09-25 by synthesising the agent's sentence
and transcribing it back, after the owner's call 675); a comma is a short pause in every
language. A chain of three or more digit groups (« 1-5-2 », « 2026-02-132 ») is never a
range, so its dashes become commas; two groups (« 9-12 ») may be a range and are left to
the voice. SVP_TTS_DIGIT_GROUPS=comma turns the filter on; unset = stock behaviour.
"""

import os
import re

from pipecat.utils.text.base_text_filter import BaseTextFilter

_DASH = r"[  ]?[-‐‑‒–][  ]?"
_CHAIN = re.compile(rf"(?<!\d)\d+(?:{_DASH}\d+){{2,}}(?!\d)")


def said_as_groups(text: str) -> str:
    """« Le numéro est 1-5-2. » -> « Le numéro est 1, 5, 2. »"""
    return _CHAIN.sub(lambda chain: re.sub(_DASH, ", ", chain.group(0)), text)


class DigitGroupsTextFilter(BaseTextFilter):
    async def update_settings(self, settings):
        pass

    async def filter(self, text: str) -> str:
        return said_as_groups(text)

    async def handle_interruption(self):
        pass

    async def reset_interruption(self):
        pass


def svp_text_filters() -> list[BaseTextFilter]:
    """The platform's filters for every TTS service (after the stock XML tag filter)."""
    if (os.getenv("SVP_TTS_DIGIT_GROUPS") or "").strip().lower() == "comma":
        return [DigitGroupsTextFilter()]
    return []
