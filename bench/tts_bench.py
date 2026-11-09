"""TTS benchmark: latency, real-time factor, VRAM; saves WAVs for listening.

  python -m bench.tts_bench --engines kokoro,mms[,azure]

Naturalness cannot be measured automatically with any honesty here. The run
writes results/phase4/tts_listening_sheet.csv; fill in the 1-5 scores by ear.
"""
from __future__ import annotations

import argparse
import csv
import gc
import os
import statistics
import time

import soundfile as sf

from voice_engine.config import TtsCfg, load_config
from voice_engine.tts import build_engine, timed_synthesis

from .common import GpuPeak, ROOT, engine_config_path, environment, gpu_used_mb, load_yaml, results_dir, write_json

SUPPORTED = {"kokoro": {"en"}, "mms": {"ur"}, "azure": {"en", "ur"}}


def run(engines: list[str]) -> dict:
    cfg = load_config(engine_config_path())
    tcfg: TtsCfg = cfg.tts
    sentences = load_yaml("bench/prompts.yaml")["tts_sentences"]
    out = {"environment": environment(), "engines": {}}
    sheet = []
    for name in engines:
        langs = SUPPORTED[name]
        loaded = {}
        before = gpu_used_mb()
        t0 = time.perf_counter()
        try:
            for lang in sorted(langs):
                if name == "azure":
                    loaded[lang] = build_engine(name, tcfg, lambda p: ROOT / p, lang,
                                                os.environ.get(tcfg.azure_key_env, ""))
                elif not loaded:
                    eng = build_engine(name, tcfg, lambda p: ROOT / p, lang)
                    for lg in langs:
                        loaded[lg] = eng
        except Exception as e:
            out["engines"][name] = {"status": "load_failed", "error": f"{type(e).__name__}: {e}"}
            print(f"[{name}] load failed: {e}")
            continue
        load_s = time.perf_counter() - t0
        after = gpu_used_mb()
        any_eng = next(iter(loaded.values()))
        rows = []
        adir = results_dir() / "tts_audio" / name
        adir.mkdir(parents=True, exist_ok=True)
        with GpuPeak() as peak:
            for s in sentences:
                if s["language"] not in langs:
                    continue
                eng = loaded[s["language"]]
                timed_synthesis(eng, s["text"])                  # warm-up per sentence shape
                audio, first_s, total_s = timed_synthesis(eng, s["text"])
                dur = len(audio) / eng.sample_rate
                path = adir / f"{s['id']}.wav"
                sf.write(path, audio, eng.sample_rate, subtype="PCM_16")
                rows.append({"id": s["id"], "language": s["language"], "text": s["text"],
                             "first_chunk_s": round(first_s, 3), "total_s": round(total_s, 3),
                             "audio_s": round(dur, 2), "rtf": round(total_s / dur, 3) if dur else None,
                             "wav": str(path)})
                sheet.append({"engine": name, "id": s["id"], "language": s["language"], "text": s["text"],
                              "wav": str(path), "naturalness_1_5": "", "intelligibility_1_5": "",
                              "pronunciation_errors": "", "notes": ""})
                print(f"[{name}] {s['id']} first={first_s:.3f}s total={total_s:.3f}s audio={dur:.2f}s")
        rtfs = [r["rtf"] for r in rows if r["rtf"]]
        out["engines"][name] = {
            "status": "ok", "license": any_eng.license_note, "sample_rate": any_eng.sample_rate,
            "load_s": round(load_s, 2),
            "vram_load_mb": (after - before) if before is not None and after is not None else None,
            "peak_vram_total_mb": peak.peak_mb,
            "rtf_mean": round(statistics.mean(rtfs), 3) if rtfs else None,
            "first_chunk_mean_s": round(statistics.mean(r["first_chunk_s"] for r in rows), 3) if rows else None,
            "rows": rows}
        del loaded, any_eng
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
    sheet_path = results_dir() / "tts_listening_sheet.csv"
    with sheet_path.open("w", newline="", encoding="utf-8-sig") as fh:   # BOM so Excel shows Urdu
        w = csv.DictWriter(fh, fieldnames=list(sheet[0].keys()) if sheet else ["engine"])
        w.writeheader()
        w.writerows(sheet)
    out["listening_sheet"] = str(sheet_path)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default="kokoro,mms")
    a = ap.parse_args()
    res = run(a.engines.split(","))
    print("written", write_json("tts_results.json", res))


if __name__ == "__main__":
    main()
