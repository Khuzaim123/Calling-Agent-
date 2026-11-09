"""Do STT, TTS and the LLM fit and run together on one T4, or must they take turns?

  python -m bench.concurrency --manifest datasets/manifest_fleurs_ur_pk.jsonl --iters 8

Requires llama-server running with the candidate LLM. Measures:
  * total VRAM with everything resident (+ per-process split),
  * latency of each stage alone vs. overlapped (the situation two callers create),
  * peak VRAM while overlapped.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from voice_engine.config import load_config, secret
from voice_engine.llm import LlmClient
from voice_engine.server import load_models

from .common import GpuPeak, ROOT, engine_config_path, environment, gpu_processes_mb, gpu_used_mb, write_json
from .datasets import load_manifest
from .stt_bench import load_items_audio

PROMPT = [{"role": "system", "content": "You are a concise phone agent."},
          {"role": "user", "content": "Explain your delivery options and payment methods in three sentences."}]


async def llm_once(client: LlmClient):
    t0, ttft = time.perf_counter(), None
    async for ev in client.stream(PROMPT):
        if ev.kind == "text" and ttft is None:
            ttft = time.perf_counter() - t0
    return ttft, time.perf_counter() - t0


def summarize(xs):
    xs = [x for x in xs if x is not None]
    return {"mean": round(statistics.mean(xs), 3), "max": round(max(xs), 3)} if xs else None


async def run(manifest: str, iters: int) -> dict:
    cfg = load_config(engine_config_path())
    base = gpu_used_mb()
    models = await asyncio.get_running_loop().run_in_executor(None, load_models, cfg)
    resident = gpu_used_mb()
    llm = LlmClient(cfg.llm, secret(cfg.llm.api_key_env, required=False))
    if not await llm.health():
        raise SystemExit("llama-server is not running")
    items, clips, _ = load_items_audio(load_manifest(ROOT / manifest if not Path(manifest).is_absolute()
                                                     else Path(manifest)))
    item = next((it for it in items if 3 <= len(clips[it["id"]]) / 16000 <= 12), None)
    if item is None:
        raise SystemExit(f"no readable 3-12 s clip in {manifest}")
    audio = clips[item["id"]]
    tts_text = {"en": "Your order is out for delivery and will arrive in fifteen minutes.",
                "ur": "آپ کا آرڈر راستے میں ہے اور پندرہ منٹ میں پہنچ جائے گا۔"}
    pool = ThreadPoolExecutor(4)
    loop = asyncio.get_running_loop()

    def stt():
        t0 = time.perf_counter()
        models.stt.recognize(audio, hint=item["language"])
        return time.perf_counter() - t0

    def tts(lang):
        t0 = time.perf_counter()
        list(models.tts.for_language(lang).synthesize(tts_text[lang]))
        return time.perf_counter() - t0

    stt()
    tts("en")
    await llm_once(llm)                                   # warm-ups
    alone = {"stt_s": [], "tts_en_s": [], "tts_ur_s": [], "llm_ttft_s": [], "llm_total_s": []}
    for _ in range(iters):
        alone["stt_s"].append(stt())
        alone["tts_en_s"].append(tts("en"))
        if "ur" in models.tts.engines:
            alone["tts_ur_s"].append(tts("ur"))
        a, b = await llm_once(llm)
        alone["llm_ttft_s"].append(a)
        alone["llm_total_s"].append(b)

    overlapped = {"stt_s": [], "tts_s": [], "llm_ttft_s": [], "llm_total_s": []}
    with GpuPeak() as peak:
        for _ in range(iters):
            r = await asyncio.gather(loop.run_in_executor(pool, stt), loop.run_in_executor(pool, tts, "en"),
                                     llm_once(llm))
            overlapped["stt_s"].append(r[0])
            overlapped["tts_s"].append(r[1])
            overlapped["llm_ttft_s"].append(r[2][0])
            overlapped["llm_total_s"].append(r[2][1])
    await llm.close()
    await models.llm.close()

    A = {k: summarize(v) for k, v in alone.items()}
    O = {k: summarize(v) for k, v in overlapped.items()}
    inflation = {k: round(O[k]["mean"] / A[a]["mean"], 2)
                 for k, a in (("stt_s", "stt_s"), ("tts_s", "tts_en_s"), ("llm_ttft_s", "llm_ttft_s"),
                              ("llm_total_s", "llm_total_s")) if O.get(k) and A.get(a)}
    total = None
    try:
        import torch
        total = torch.cuda.get_device_properties(0).total_memory / 2**20
    except Exception:
        pass
    return {"environment": environment(), "stt_model": cfg.stt.model_path, "tts": cfg.tts.engine_by_language,
            "llm_base_url": cfg.llm.base_url, "test_clip": item["id"], "test_clip_s": round(len(audio) / 16000, 2),
            "vram_mb": {"before_engine": base, "all_resident": resident, "peak_overlapped": peak.peak_mb,
                        "device_total": total, "per_process": gpu_processes_mb()},
            "alone": A, "overlapped": O, "latency_inflation_x": inflation, "iters": iters,
            "note": f"pid {os.getpid()} = voice engine models; the other GPU process is llama-server"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--iters", type=int, default=8)
    a = ap.parse_args()
    res = asyncio.run(run(a.manifest, a.iters))
    print({k: res[k] for k in ("vram_mb", "alone", "overlapped", "latency_inflation_x")})
    print("written", write_json("concurrency_results.json", res))


if __name__ == "__main__":
    main()
