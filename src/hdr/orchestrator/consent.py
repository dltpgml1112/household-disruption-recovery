"""Deterministic check of a parent's answer to a plan. Shared by both agents: whichever
agent understood the words, this decides whether they count as a yes."""

from __future__ import annotations

import re

WAKE = re.compile(r"^\s*(hey\s+)?alexa[,!.\s]+", re.I)

# Words that make a "yes" hesitant, conditional or a question. Any of them blocks an
# approval; the parent can always follow up with a plain yes. Speech recognition drops
# question marks, so question phrases are listed as words too.
CONSENT_VETO = re.compile(
    r"\?|\b(no|nope|not|never|don'?t|do not|doesn'?t|isn'?t|can'?t|won'?t|wait|hold on|hang on|stop|cancel|"
    r"but|except|unless|instead|if|why|what about|how about|maybe|perhaps|not sure|hmm|later)\b",
    re.I,
)


def normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").lower()
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return " ".join(text.split())


def hedge(utterance: str) -> str | None:
    """The word that keeps this from being a plain yes, if there is one."""
    hit = CONSENT_VETO.search(WAKE.sub("", utterance))
    return hit.group(0) if hit else None


def consent_problem(utterance: str, quoted: str, decision: str) -> str | None:
    """Why the parent's message cannot count as this decision, or None if it can."""
    if not normalize(quoted) or normalize(quoted) not in normalize(utterance):
        return "parent_words must be copied from the parent's latest message"
    if decision == "approve":
        word = hedge(utterance)
        if word:
            return f'the parent did not give a plain yes ("{word}"); answer them and ask again'
    return None
