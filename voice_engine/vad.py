"""Voice activity detection (Silero) + utterance endpointing.

`Endpointer` is pure logic over per-chunk speech probabilities, so it is unit
tested without the model. `SileroVad` produces the probabilities.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .config import VadCfg

SAMPLE_RATE = 16000
CHUNK = 512                     # Silero v5+/v6 requires 512 samples at 16 kHz
CHUNK_MS = CHUNK * 1000 / SAMPLE_RATE   # 32 ms


@dataclass
class VadEvent:
    kind: str                   # "speech_start" | "speech" | "utterance"
    speech_ms: float = 0.0
    audio: np.ndarray | None = None
    forced: bool = False        # utterance cut by max_utterance_s


class Endpointer:
    def __init__(self, cfg: VadCfg):
        self.cfg = cfg
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, int(cfg.preroll_ms / CHUNK_MS)))
        self._pending: list[np.ndarray] = []
        self._pending_ms = 0.0
        self._utt: list[np.ndarray] = []
        self.in_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._total_ms = 0.0

    def reset(self) -> None:
        self.__init__(self.cfg)

    def push(self, chunk: np.ndarray, prob: float) -> list[VadEvent]:
        c = self.cfg
        ev: list[VadEvent] = []
        if not self.in_speech:
            if prob >= c.threshold:
                self._pending.append(chunk)
                self._pending_ms += CHUNK_MS
                if self._pending_ms >= c.min_speech_ms:
                    self.in_speech = True
                    self._utt = list(self._preroll) + self._pending
                    self._speech_ms = self._pending_ms
                    self._total_ms = len(self._utt) * CHUNK_MS
                    self._silence_ms = 0.0
                    self._pending, self._pending_ms = [], 0.0
                    self._preroll.clear()
                    ev.append(VadEvent("speech_start", self._speech_ms))
            else:
                for p in self._pending:
                    self._preroll.append(p)
                self._pending, self._pending_ms = [], 0.0
                self._preroll.append(chunk)
            return ev

        self._utt.append(chunk)
        self._total_ms += CHUNK_MS
        if prob < c.neg_threshold:
            self._silence_ms += CHUNK_MS
        else:
            self._silence_ms = 0.0
            if prob >= c.threshold:
                self._speech_ms += CHUNK_MS
        ev.append(VadEvent("speech", self._speech_ms))

        forced = self._total_ms >= c.max_utterance_s * 1000
        if self._silence_ms >= c.end_silence_ms or forced:
            ev.append(VadEvent("utterance", self._speech_ms, np.concatenate(self._utt), forced))
            self.in_speech = False
            self._utt = []
            self._speech_ms = self._silence_ms = self._total_ms = 0.0
        return ev


class SileroVad:
    """Per-session Silero model instance (the model is stateful)."""

    def __init__(self):
        import torch
        from silero_vad import load_silero_vad

        torch.set_num_threads(1)
        self._torch = torch
        self.model = load_silero_vad()

    def prob(self, chunk: np.ndarray) -> float:
        if chunk.shape[0] != CHUNK:
            raise ValueError(f"Silero needs {CHUNK}-sample chunks, got {chunk.shape[0]}")
        with self._torch.no_grad():
            return float(self.model(self._torch.from_numpy(chunk), SAMPLE_RATE).item())

    def reset(self) -> None:
        self.model.reset_states()
