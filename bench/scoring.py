"""Script-aware scoring for Urdu-English code-switched speech.

Reference transcripts of mixed speech write some English words in Latin script and
others in Urdu script; STT does the same, inconsistently. Plain WER counts
`image` vs `امیج` as an error even when the word was heard correctly, so mixed
sets are also scored on (a) Urdu-script words only and (b) recall of the
reference's Latin-script (English) words. Inputs are already normalised with
voice_engine.text.normalize_for_scoring.
"""
from __future__ import annotations

import re
from collections import Counter

_LATIN = re.compile(r"^[a-z0-9']+$")


def split_scripts(normalized: str) -> tuple[list[str], list[str]]:
    """(non-Latin tokens, Latin tokens). Digits count as Latin."""
    ur, la = [], []
    for tok in normalized.split():
        (la if _LATIN.match(tok) else ur).append(tok)
    return ur, la


def latin_recall(ref_norm: str, hyp_norm: str) -> tuple[int, int]:
    """How many of the reference's Latin-script words appear (as Latin) in the hypothesis."""
    ref_la = Counter(split_scripts(ref_norm)[1])
    hyp_la = Counter(split_scripts(hyp_norm)[1])
    hits = sum(min(c, hyp_la[w]) for w, c in ref_la.items())
    return hits, sum(ref_la.values())
