"""Conservative token estimation and sentence splitting.

Montauk has no bundled tokenizer (the local embedding model's tokenizer
isn't exposed as a general-purpose counter), so `prepare_person_context`
budgets against a deliberately *over*-estimating heuristic: it is better
to return slightly less than the requested budget than to blow past it
because of an optimistic character count. The estimate is the max of a
word-based and a character-based figure.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"\S+")
# Sentence boundary: ., !, ? or a newline, optionally followed by quotes/brackets,
# then whitespace. Keeps decimal numbers and common abbreviations mostly intact
# by requiring trailing whitespace + an uppercase/quote start is *not* enforced
# (over-splitting is harmless for retrieval chunking).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n+")

_CHARS_PER_TOKEN = 3.6
_TOKENS_PER_WORD = 1.33


def estimate_tokens(text: str) -> int:
    """Upper-ish bound on the token count of `text`. Never returns less
    than the true count for typical English prose."""
    if not text:
        return 0
    words = len(_WORD_RE.findall(text))
    by_words = words * _TOKENS_PER_WORD
    by_chars = len(text) / _CHARS_PER_TOKEN
    return max(1, round(max(by_words, by_chars)))


def split_sentences(text: str) -> list[str]:
    """Split into sentence-ish spans at natural punctuation/newline
    boundaries. Whitespace is normalized within each returned span."""
    parts = [" ".join(p.split()) for p in _SENTENCE_SPLIT_RE.split(text)]
    return [p for p in parts if p]
