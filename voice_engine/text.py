"""Text utilities: script detection, Urdu normalisation, streaming sentence splitting."""
from __future__ import annotations

import re
import unicodedata

# Arabic-script blocks used by Urdu.
_ARABIC_RANGES = ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
_DEVANAGARI = (0x0900, 0x097F)


def _in(ch: str, ranges) -> bool:
    o = ord(ch)
    return any(a <= o <= b for a, b in ranges)


def script_profile(text: str) -> dict[str, float]:
    """Fraction of letters that are Arabic-script, Latin, Devanagari."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return {"arabic": 0.0, "latin": 0.0, "devanagari": 0.0}
    n = len(letters)
    ar = sum(_in(c, _ARABIC_RANGES) for c in letters)
    dv = sum(_in(c, (_DEVANAGARI,)) for c in letters)
    la = sum(("a" <= c.lower() <= "z") for c in letters)
    return {"arabic": ar / n, "latin": la / n, "devanagari": dv / n}


def dominant_script_language(text: str) -> str:
    """'ur' if mostly Arabic script, else 'en'. Used to pick a TTS voice per sentence."""
    p = script_profile(text)
    return "ur" if p["arabic"] >= 0.5 else "en"


# --- Urdu normalisation for scoring (WER/CER) -----------------------------------
_URDU_MAP = str.maketrans({
    "ي": "ی",  # Arabic yeh -> Farsi yeh
    "ى": "ی",  # alef maksura -> Farsi yeh
    "ك": "ک",  # Arabic kaf -> keheh
    "ه": "ہ",  # Arabic heh -> heh goal
    "ۀ": "ہ",  # heh with yeh above -> heh goal
    "ة": "ۃ",  # teh marbuta -> teh marbuta goal
})
_DIACRITICS = re.compile(r"[ً-ٰٟۖ-ۭـ]")  # harakat, superscript alef, tatweel
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_for_scoring(text: str, language: str) -> str:
    t = unicodedata.normalize("NFC", text)
    if language == "ur":
        t = t.translate(_URDU_MAP)
        t = _DIACRITICS.sub("", t)
    t = t.lower()
    t = t.replace("۔", " ").replace("،", " ").replace("؟", " ")  # Urdu full stop, comma, question
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


# --- Streaming sentence splitter --------------------------------------------------
_HARD_END = re.compile(r"[.!?۔؟]+\s+")   # . ! ? Urdu full stop, Arabic question mark
_SOFT_END = re.compile(r"([,;:،])(\s+)")      # , ; : Urdu comma


class SentenceStreamer:
    """Accumulate streamed LLM tokens and release speakable chunks early.

    The first chunk may be cut at a soft boundary (comma) once it is long enough,
    to reduce time-to-first-audio. Later chunks wait for sentence ends.
    """

    def __init__(self, first_soft_min_chars: int = 40, max_chars: int = 220):
        self.buf = ""
        self.first = True
        self.first_soft_min = first_soft_min_chars
        self.max_chars = max_chars

    def push(self, token: str) -> list[str]:
        self.buf += token
        out: list[str] = []
        while True:
            chunk = self._take()
            if chunk is None:
                break
            out.append(chunk)
        return out

    def _take(self) -> str | None:
        # A boundary needs whitespace after the punctuation, so "3.5" never splits and
        # punctuation at the end of the buffer waits for the next token (or flush()).
        m = _HARD_END.search(self.buf)
        if m:
            return self._cut(m.end())
        if self.first:
            s = _SOFT_END.search(self.buf)
            if s and s.end(1) >= self.first_soft_min:
                return self._cut(s.end())
        if len(self.buf) >= self.max_chars:
            sp = self.buf.rfind(" ", 0, self.max_chars)
            return self._cut(sp + 1 if sp > 0 else self.max_chars)
        return None

    def _cut(self, idx: int) -> str | None:
        chunk, self.buf = self.buf[:idx].strip(), self.buf[idx:]
        if not chunk:
            return None
        self.first = False
        return chunk

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_reasoning(text: str) -> str:
    return _THINK.sub("", text)
