"""Speech-to-text with faster-whisper and an explicit language policy.

Language policy (why): Whisper's free language ID on short phone utterances
is unreliable, and spoken Urdu is often detected as Hindi ("hi") because the
spoken languages overlap; transcribing as Hindi yields Devanagari text that
is useless downstream. We therefore restrict detection to the business's
allowed languages and fold the Hindi probability into Urdu.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import SttCfg


@dataclass
class SttResult:
    text: str
    language: str
    language_prob: float
    avg_logprob: float
    no_speech_prob: float
    audio_s: float
    elapsed_s: float
    rejected: bool = False
    reject_reason: str = ""
    lang_probs: dict[str, float] = field(default_factory=dict)


def choose_language(probs: dict[str, float], allowed: list[str], previous: str | None) -> tuple[str, float]:
    """Pick the most likely allowed language; Hindi probability counts toward Urdu."""
    scores = {lang: probs.get(lang, 0.0) for lang in allowed}
    if "ur" in scores:
        scores["ur"] += probs.get("hi", 0.0)
    best = max(scores, key=scores.get)
    if scores[best] <= 0.0:
        fallback = previous if previous in allowed else allowed[0]
        return fallback, 0.0
    return best, scores[best]


def should_reject(text: str, avg_logprob: float, no_speech_prob: float, cfg: SttCfg) -> str:
    if not text.strip():
        return "empty"
    if no_speech_prob > cfg.no_speech_threshold and avg_logprob < cfg.min_avg_logprob:
        return "no_speech"
    return ""


class SttEngine:
    def __init__(self, cfg: SttCfg, model_path: Path):
        from faster_whisper import WhisperModel

        if not Path(model_path).exists():
            raise FileNotFoundError(f"STT model not found at {model_path}")
        self.cfg = cfg
        self.model = WhisperModel(str(model_path), device=cfg.device, compute_type=cfg.compute_type)

    def _lang_probs(self, audio: np.ndarray) -> dict[str, float]:
        _, _, all_probs = self.model.detect_language(audio)
        return {lang: float(p) for lang, p in all_probs}

    def recognize(self, audio: np.ndarray, hint: str = "auto", previous: str | None = None) -> SttResult:
        t0 = time.perf_counter()
        allowed = self.cfg.allowed_languages
        probs: dict[str, float] = {}
        if hint in allowed:
            lang, lprob = hint, 1.0
        else:
            probs = self._lang_probs(audio)
            lang, lprob = choose_language(probs, allowed, previous)

        segments, _info = self.model.transcribe(
            audio,
            language=lang,
            beam_size=self.cfg.beam_size,
            vad_filter=False,                 # Silero endpointing already ran
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        if segs:
            weights = [max(len(s.text), 1) for s in segs]
            avg_lp = float(np.average([s.avg_logprob for s in segs], weights=weights))
            nsp = float(max(s.no_speech_prob for s in segs))
        else:
            avg_lp, nsp = -10.0, 1.0
        reason = should_reject(text, avg_lp, nsp, self.cfg)
        return SttResult(
            text=text, language=lang, language_prob=lprob, avg_logprob=avg_lp,
            no_speech_prob=nsp, audio_s=len(audio) / 16000, elapsed_s=time.perf_counter() - t0,
            rejected=bool(reason), reject_reason=reason,
            lang_probs={k: probs[k] for k in ("en", "ur", "hi") if k in probs},
        )
