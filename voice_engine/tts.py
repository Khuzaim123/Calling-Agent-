"""Text-to-speech engines behind one interface.

Engines (Phase 4 candidates):
  kokoro : English, Apache-2.0, 24 kHz. Python < 3.13 only (package constraint).
  mms    : Urdu (Arabic script), facebook/mms-tts-urd-script_arabic, 16 kHz.
           LICENSE CC-BY-NC-4.0 -> evaluation only, NOT for commercial use.
  azure  : Optional paid reference (ur-PK / en-US neural voices) via REST,
           enabled only when AZURE_SPEECH_KEY and region are configured.

`synthesize()` is blocking; the session runs it in an executor.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator, Protocol
from xml.sax.saxutils import escape

import numpy as np

from .config import TtsCfg


class TtsEngine(Protocol):
    name: str
    sample_rate: int
    license_note: str

    def synthesize(self, text: str) -> Iterator[np.ndarray]: ...


class KokoroTts:
    name = "kokoro"
    sample_rate = 24000
    license_note = "Apache-2.0"

    def __init__(self, cfg: TtsCfg, voice: str | None = None, lang_code: str | None = None):
        from kokoro import KPipeline

        self.voice = voice or cfg.kokoro_voice
        self.pipe = KPipeline(lang_code=lang_code or cfg.kokoro_lang_code, repo_id=cfg.kokoro_repo,
                              device=cfg.device)

    def synthesize(self, text: str) -> Iterator[np.ndarray]:
        for result in self.pipe(text, voice=self.voice, speed=1.0):
            audio = getattr(result, "audio", None)
            if audio is None:            # older tuple-style results: (graphemes, phonemes, audio)
                audio = result[2]
            if audio is not None:
                yield np.asarray(audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio,
                                 dtype=np.float32)


class MmsTts:
    name = "mms"
    sample_rate = 16000
    license_note = "CC-BY-NC-4.0 (non-commercial; evaluation only)"

    def __init__(self, cfg: TtsCfg, model_path: Path):
        import torch
        from transformers import AutoTokenizer, VitsModel

        if not Path(model_path).exists():
            raise FileNotFoundError(f"MMS model not found at {model_path}")
        self._torch = torch
        self.device = cfg.device if torch.cuda.is_available() else "cpu"
        self.tok = AutoTokenizer.from_pretrained(str(model_path))
        self.model = VitsModel.from_pretrained(str(model_path)).to(self.device).eval()
        self.sample_rate = int(self.model.config.sampling_rate)
        self._uroman = None
        if getattr(self.tok, "is_uroman", False):
            import uroman as ur  # tokenizer expects romanised input
            self._uroman = ur.Uroman()

    def synthesize(self, text: str) -> Iterator[np.ndarray]:
        if self._uroman is not None:
            text = self._uroman.romanize_string(text)
        inputs = self.tok(text, return_tensors="pt").to(self.device)
        if inputs["input_ids"].shape[-1] == 0:
            return
        with self._torch.no_grad():
            wav = self.model(**inputs).waveform[0]
        yield wav.float().cpu().numpy()


class AzureTts:
    """Paid reference engine. Uses the documented REST endpoint; no SDK."""

    name = "azure"
    sample_rate = 24000
    license_note = "Commercial API (paid per character)"

    def __init__(self, cfg: TtsCfg, key: str, language: str):
        import requests  # preinstalled in Colab

        if not (cfg.azure_region and key):
            raise RuntimeError("Azure TTS needs tts.azure_region and AZURE_SPEECH_KEY")
        self._requests = requests
        self.url = f"https://{cfg.azure_region}.tts.speech.microsoft.com/cognitiveservices/v1"
        self.key = key
        self.voice = cfg.azure_voice_ur if language == "ur" else cfg.azure_voice_en
        self.lang = "ur-PK" if language == "ur" else "en-US"

    def synthesize(self, text: str) -> Iterator[np.ndarray]:
        ssml = (f"<speak version='1.0' xml:lang='{self.lang}'><voice name='{self.voice}'>"
                f"{escape(text)}</voice></speak>")
        r = self._requests.post(self.url, data=ssml.encode("utf-8"), timeout=15, headers={
            "Ocp-Apim-Subscription-Key": self.key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm",
            "User-Agent": "voice-agent-dev",
        })
        if r.status_code != 200:
            raise RuntimeError(f"Azure TTS HTTP {r.status_code}: {r.text[:200]}")
        yield np.frombuffer(r.content, dtype="<i2").astype(np.float32) / 32768.0


def build_engine(name: str, cfg: TtsCfg, resolve_path, language: str, azure_key: str = "") -> TtsEngine:
    if name == "kokoro":
        return KokoroTts(cfg)
    if name == "mms":
        # one MMS model per language: the Urdu model cannot read Latin script (English came out silent)
        return MmsTts(cfg, resolve_path(cfg.mms_model_path_en if language == "en" else cfg.mms_model_path))
    if name == "azure":
        return AzureTts(cfg, azure_key, language)
    raise ValueError(f"Unknown TTS engine {name!r}")


class TtsRouter:
    """Maps a language ('en' / 'ur') to a loaded engine."""

    def __init__(self, engines: dict[str, TtsEngine]):
        if not engines:
            raise ValueError("No TTS engines loaded")
        self.engines = engines

    def for_language(self, lang: str) -> TtsEngine:
        return self.engines.get(lang) or next(iter(self.engines.values()))


def timed_synthesis(engine: TtsEngine, text: str) -> tuple[np.ndarray, float, float]:
    """Return (audio, time_to_first_chunk_s, total_s). Used by benchmarks."""
    t0 = time.perf_counter()
    first = None
    parts = []
    for chunk in engine.synthesize(text):
        if first is None:
            first = time.perf_counter() - t0
        parts.append(chunk)
    total = time.perf_counter() - t0
    audio = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return audio, (first if first is not None else total), total
