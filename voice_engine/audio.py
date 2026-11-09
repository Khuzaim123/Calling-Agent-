"""PCM helpers: conversion, resampling, G.711 mu-law (telephony simulation)."""
from __future__ import annotations

from math import gcd

import numpy as np
from scipy.signal import resample_poly


def pcm16_to_float(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(x: np.ndarray) -> bytes:
    y = np.clip(np.asarray(x, dtype=np.float32), -1.0, 1.0)
    return (y * 32767.0).astype("<i2").tobytes()


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or x.size == 0:
        return np.asarray(x, dtype=np.float32)
    g = gcd(src, dst)
    return resample_poly(x, dst // g, src // g).astype(np.float32)


def rms_dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * np.log10(max(r, 1e-6))


# --- G.711 mu-law (ITU-T G.711), used to simulate the narrowband phone path ----
_MU = 255.0


def mulaw_encode(x: np.ndarray) -> np.ndarray:
    """float [-1,1] -> uint8 codes (continuous mu-law companding, 8-bit quantised)."""
    x = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
    y = np.sign(x) * np.log1p(_MU * np.abs(x)) / np.log1p(_MU)
    return np.round((y + 1.0) / 2.0 * 255.0).astype(np.uint8)


def mulaw_decode(codes: np.ndarray) -> np.ndarray:
    y = codes.astype(np.float64) / 255.0 * 2.0 - 1.0
    return (np.sign(y) * (np.power(1.0 + _MU, np.abs(y)) - 1.0) / _MU).astype(np.float32)


def simulate_phone_channel(x16k: np.ndarray, rate: int = 16000) -> np.ndarray:
    """Band-limit to 8 kHz sampling + mu-law round trip, then back to `rate`.

    Approximates what STT will receive from a G.711 SIP trunk.
    """
    nb = resample(x16k, rate, 8000)
    nb = mulaw_decode(mulaw_encode(nb))
    return resample(nb, 8000, rate)


class FrameAssembler:
    """Re-chunk arbitrary-size PCM16 byte messages into fixed sample frames."""

    def __init__(self, frame_samples: int):
        self.frame_bytes = frame_samples * 2
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        out = []
        while len(self._buf) >= self.frame_bytes:
            out.append(bytes(self._buf[: self.frame_bytes]))
            del self._buf[: self.frame_bytes]
        return out
