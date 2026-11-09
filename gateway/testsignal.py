"""Countable speech-band test signal (vowel-like bursts) and WAV helpers, stdlib only.

Pure tones are a poor telephony test: phone noise suppression removes them.
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

GROUPS = (2, 3, 5)


def bursts_pcm(rate: int = 16000, groups=GROUPS) -> bytes:
    out = []
    f0 = 140.0
    for n in groups:
        for _ in range(n):
            dur = int(0.35 * rate)
            for i in range(dur):
                t = i / rate
                env = math.sin(math.pi * i / dur) ** 2 * (0.5 + 0.5 * math.sin(2 * math.pi * 5 * t))
                s = sum((1 / k) * math.sin(2 * math.pi * f0 * k * t) for k in range(1, 18) if f0 * k < 3400)
                out.append(0.35 * env * s)
            out += [0.0] * int(0.25 * rate)
        out += [0.0] * int(1.5 * rate)
    return b"".join(struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767)) for x in out)


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def rms_dbfs(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return -120.0
    vals = struct.unpack(f"<{n}h", pcm[: n * 2])
    r = math.sqrt(sum(v * v for v in vals) / n) / 32768
    return 20 * math.log10(max(r, 1e-6))
