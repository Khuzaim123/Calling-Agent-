"""VAD benchmark: Silero CPU cost, utterance detection on real speech, false triggers on noise.

  python -m bench.vad_bench --manifests datasets/manifest_fleurs_ur_pk.jsonl,datasets/manifest_fleurs_en_us.jsonl
"""
from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import numpy as np

from voice_engine.audio import simulate_phone_channel
from voice_engine.config import load_config
from voice_engine.vad import CHUNK, Endpointer, SileroVad

from .common import ROOT, engine_config_path, environment, write_json
from .datasets import load_manifest
from .stt_bench import load_items_audio


def endpoint(vad: SileroVad, ep: Endpointer, x: np.ndarray):
    vad.reset()
    ep.reset()
    utts, lat = [], []
    for i in range(0, len(x) - CHUNK + 1, CHUNK):
        c = x[i:i + CHUNK]
        t0 = time.perf_counter()
        p = vad.prob(c)
        lat.append(time.perf_counter() - t0)
        utts += [e for e in ep.push(c, p) if e.kind == "utterance"]
    return utts, lat


def noise(kind: str, seconds: float, dbfs: float, rng) -> np.ndarray:
    n = int(16000 * seconds)
    w = rng.standard_normal(n)
    if kind == "pink":
        f = np.fft.rfft(w)
        f /= np.sqrt(np.maximum(np.arange(len(f)), 1))
        w = np.fft.irfft(f, n)
    w = w / np.sqrt(np.mean(w ** 2)) * (10 ** (dbfs / 20))
    return w.astype(np.float32)


def run(manifests: list[str], limit: int) -> dict:
    cfg = load_config(engine_config_path())
    vad = SileroVad()
    ep = Endpointer(cfg.vad)
    items = []
    for mf in manifests:
        items += load_manifest(ROOT / mf if not Path(mf).is_absolute() else Path(mf))[:limit]
    items, audio, skipped = load_items_audio(items)
    pad = np.zeros(16000, np.float32)
    res = {"environment": environment(), "vad_config": cfg.vad.__dict__, "skipped_files": skipped,
           "speech": {}, "noise": {}}
    all_lat = []
    for cond in ("wide", "phone"):
        detected, frags, cover = 0, [], []
        for it in items:
            x = audio[it["id"]]
            if cond == "phone":
                x = simulate_phone_channel(x)
            utts, lat = endpoint(vad, ep, np.concatenate([pad, x, pad]))
            all_lat += lat
            if utts:
                detected += 1
            frags.append(len(utts))
            cover.append(sum(len(u.audio) for u in utts) / max(len(x), 1))
        res["speech"][cond] = {"n": len(items), "detection_rate": round(detected / max(1, len(items)), 4),
                               "mean_utterances_per_clip": round(statistics.mean(frags), 3) if frags else None,
                               "mean_coverage_ratio": round(statistics.mean(cover), 3) if cover else None}
        print(f"[vad/{cond}] {res['speech'][cond]}")
    rng = np.random.default_rng(0)
    for kind in ("white", "pink"):
        for db in (-50, -40, -30):
            utts, lat = endpoint(vad, ep, noise(kind, 60, db, rng))
            all_lat += lat
            res["noise"][f"{kind}_{db}dBFS"] = {"seconds": 60, "false_utterances": len(utts)}
            print(f"[vad/noise] {kind} {db} dBFS: {len(utts)} false utterances / 60 s")
    ms = [v * 1000 for v in all_lat]
    res["cpu_ms_per_32ms_chunk"] = {"mean": round(statistics.mean(ms), 3),
                                    "p99": round(sorted(ms)[int(0.99 * (len(ms) - 1))], 3)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifests", required=True)
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    res = run(a.manifests.split(","), a.limit)
    print("written", write_json("vad_results.json", res))


if __name__ == "__main__":
    main()
