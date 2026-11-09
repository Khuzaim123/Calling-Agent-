"""CLI.

  python -m voice_engine serve    --config config/voice_engine.yaml
  python -m voice_engine selftest --config config/voice_engine.yaml --wav in.wav --out out.wav

`selftest` runs the full pipeline (VAD -> STT -> LLM -> TTS, gateway included)
on a WAV file through an in-memory transport. It needs the models and the LLM
server, i.e. it runs in Colab, not on a CPU-only laptop.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time

import numpy as np


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class MemoryTransport:
    def __init__(self, output_rate: int):
        self.output_rate = output_rate
        self.events: list[dict] = []
        self.audio = bytearray()

    async def send_json(self, obj: dict) -> None:
        self.events.append(obj)
        print("EVENT", json.dumps(obj, ensure_ascii=False))

    async def send_audio(self, pcm16: bytes) -> None:
        self.audio.extend(pcm16)


async def _selftest(cfg, wav_in: str, wav_out: str, realtime: bool) -> int:
    import soundfile as sf

    from .audio import float_to_pcm16, resample
    from .metrics import JsonlLogger
    from .server import build_gateway, load_models
    from .session import VoiceSession

    audio, sr = sf.read(wav_in, dtype="float32", always_2d=True)
    audio = resample(audio.mean(axis=1), sr, 16000)
    audio = np.concatenate([audio, np.zeros(int(16000 * 2.0), np.float32)])  # trailing silence ends the turn

    models = load_models(cfg)
    if not await models.llm.health():
        print("ERROR: LLM server not reachable at", cfg.llm.base_url, file=sys.stderr)
        return 3
    gw = build_gateway(cfg)
    tx = MemoryTransport(cfg.output.browser_rate)
    s = VoiceSession(cfg, models, gw, tx, JsonlLogger(cfg.path(cfg.log.dir) / "selftest_turns.jsonl"),
                     {"channel": "selftest"})
    await s.start()
    if s._resp_task:            # let the greeting finish so the test turn is not treated as an overlap
        await s._resp_task
    frame = 320                 # 20 ms
    for i in range(0, len(audio), frame):
        await s.on_audio(float_to_pcm16(audio[i:i + frame]))
        if realtime:
            await asyncio.sleep(0.02)
    t0 = time.time()
    while s._resp_task and not s._resp_task.done() and time.time() - t0 < 60:
        await asyncio.sleep(0.05)
    await s.close("selftest_done")
    await models.llm.close()
    await gw.close()
    pcm = np.frombuffer(bytes(tx.audio), dtype="<i2")
    sf.write(wav_out, pcm, cfg.output.browser_rate, subtype="PCM_16")
    users = [e for e in tx.events if e.get("type") == "transcript" and e.get("role") == "user"]
    bots = [e for e in tx.events if e.get("type") == "transcript" and e.get("role") == "assistant"]
    print(f"\nSELFTEST user_turns={len(users)} assistant_sentences={len(bots)} "
          f"audio_out_s={len(pcm) / cfg.output.browser_rate:.2f} -> {wav_out}")
    ok = bool(users) and not users[-1].get("rejected") and bool(bots) and len(pcm) > 0
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    from aiohttp import web

    from .config import load_config

    ap = argparse.ArgumentParser(prog="voice_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "selftest"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name == "selftest":
            p.add_argument("--wav", required=True)
            p.add_argument("--out", required=True)
            p.add_argument("--realtime", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    _setup_logging(cfg.log.level)

    if args.cmd == "serve":
        from .server import make_app
        web.run_app(make_app(cfg), host=cfg.server.host, port=cfg.server.port, access_log=None)
        return 0
    return asyncio.run(_selftest(cfg, args.wav, args.out, args.realtime))


if __name__ == "__main__":
    sys.exit(main())
