"""STT benchmark: accuracy, language ID, latency and VRAM per model and channel.

  python -m bench.stt_bench --models whisper-small,whisper-large-v3-turbo \
      --manifests datasets/manifest_fleurs_ur_pk.jsonl,datasets/manifest_own.jsonl --conditions wide,phone

Conditions: wide = 16 kHz as recorded; phone = 8 kHz G.711 mu-law simulation
(what a SIP trunk delivers). Language mode is the production policy
(restricted auto-detect, Hindi folded into Urdu) unless --forced.
"""
from __future__ import annotations

import argparse
import gc
import statistics
import time
from pathlib import Path

import jiwer
import numpy as np
import soundfile as sf

from voice_engine.audio import resample, simulate_phone_channel
from voice_engine.config import SttCfg
from voice_engine.stt import SttEngine
from voice_engine.text import normalize_for_scoring

from .common import GpuPeak, ROOT, environment, gpu_used_mb, keyword_hits, load_yaml, write_json
from .datasets import load_manifest
from .scoring import latin_recall, split_scripts


def load_audio(path: str) -> np.ndarray:
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    return resample(x.mean(axis=1), sr, 16000)


def load_items_audio(items: list[dict]) -> tuple[list[dict], dict[str, np.ndarray], list[dict]]:
    """Load every item's audio; unreadable files are skipped (and reported), not fatal."""
    ok, audio, skipped = [], {}, []
    for it in items:
        try:
            audio[it["id"]] = load_audio(it["path"])
            ok.append(it)
        except Exception as e:
            skipped.append({"id": it["id"], "path": it["path"], "error": f"{type(e).__name__}: {e}"[:200]})
    if skipped:
        print(f"WARNING: skipped {len(skipped)} unreadable audio files (first: {skipped[0]['error']})")
    return ok, audio, skipped


def score(hyp: str, refs: list[str], lang: str) -> tuple[float, float, str]:
    h = normalize_for_scoring(hyp, lang)
    best = (1.0, 1.0, refs[0])
    for r in refs:
        rn = normalize_for_scoring(r, lang)
        if not rn:
            continue
        w = jiwer.wer(rn, h) if h else 1.0
        c = jiwer.cer(rn, h) if h else 1.0
        if w < best[0]:
            best = (w, c, rn)
    return best


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def run(models: list[str], manifests: list[str], conditions: list[str], forced: bool, device: str,
        compute_type: str, limit: int | None) -> dict:
    catalog = {m["id"]: m for m in load_yaml("config/models.yaml")["stt"]}
    items = []
    for mf in manifests:
        items += load_manifest(ROOT / mf if not Path(mf).is_absolute() else Path(mf))
    if limit:
        items = items[:limit]
    items, audio, skipped = load_items_audio(items)
    out = {"environment": environment(), "device": device, "compute_type": compute_type,
           "language_mode": "forced" if forced else "policy_auto", "skipped_files": skipped, "models": {}}

    for mid in models:
        meta = catalog[mid]
        path = ROOT / "models" / meta["dir"]
        cfg = SttCfg(model_path=str(path), device=device, compute_type=compute_type)
        before = gpu_used_mb()
        t0 = time.perf_counter()
        eng = SttEngine(cfg, path)
        load_s = time.perf_counter() - t0
        after = gpu_used_mb()
        eng.recognize(np.zeros(16000, np.float32), hint="en")   # warm-up (CUDA kernels, allocator)
        res = {"load_s": round(load_s, 2), "vram_load_mb": (after - before) if before is not None and after is not None else None,
               "license": meta["license"], "conditions": {}}
        for cond in conditions:
            rows = []
            with GpuPeak() as peak:
                for it in items:
                    x = audio[it["id"]]
                    if cond == "phone":
                        x = simulate_phone_channel(x)
                    hint = it["language"] if forced else "auto"
                    r = eng.recognize(x, hint=hint)
                    w, c, ref_used = score(r.text, it["refs"], it["language"])
                    kh, kt = keyword_hits(r.text, it.get("keywords", []), it["language"])
                    hyp_norm = normalize_for_scoring(r.text, it["language"])
                    ref_ur, _ = split_scripts(ref_used)
                    hyp_ur, _ = split_scripts(hyp_norm)
                    w_ur = (jiwer.wer(" ".join(ref_ur), " ".join(hyp_ur)) if hyp_ur else 1.0) if ref_ur else None
                    la_hits, la_total = latin_recall(ref_used, hyp_norm)
                    rows.append({"id": it["id"], "group": it["group"], "language": it["language"],
                                 "detected": r.language, "lang_probs": r.lang_probs, "hyp": r.text,
                                 "ref": ref_used, "wer": round(w, 4), "cer": round(c, 4),
                                 "wer_urdu_tokens": None if w_ur is None else round(w_ur, 4),
                                 "latin_hits": la_hits, "latin_total": la_total,
                                 "kw_hits": kh, "kw_total": kt, "latency_s": round(r.elapsed_s, 3),
                                 "audio_s": round(r.audio_s, 2), "rejected": r.reject_reason})
            groups = {}
            for g in sorted({r["group"] for r in rows}):
                gr = [r for r in rows if r["group"] == g]
                lat = [r["latency_s"] for r in gr]
                kwt = sum(r["kw_total"] for r in gr)
                lat_total = sum(r["latin_total"] for r in gr)
                wur = [r["wer_urdu_tokens"] for r in gr if r["wer_urdu_tokens"] is not None]
                mixed = "mixed" in g          # for code-switched clips either language label is defensible
                groups[g] = {
                    "n": len(gr),
                    "wer_mean": round(statistics.mean(r["wer"] for r in gr), 4),
                    "cer_mean": round(statistics.mean(r["cer"] for r in gr), 4),
                    "wer_urdu_tokens_mean": round(statistics.mean(wur), 4) if wur else None,
                    "latin_word_recall": round(sum(r["latin_hits"] for r in gr) / lat_total, 4) if lat_total else None,
                    "keyword_recall": round(sum(r["kw_hits"] for r in gr) / kwt, 4) if kwt else None,
                    "language_accuracy": None if mixed else
                        round(sum(r["detected"] == r["language"] for r in gr) / len(gr), 4),
                    "detected_en_share": round(sum(r["detected"] == "en" for r in gr) / len(gr), 4),
                    "latency_p50_s": pct(lat, 0.5), "latency_p95_s": pct(lat, 0.95),
                    "rtf_mean": round(statistics.mean(r["latency_s"] / max(r["audio_s"], 0.1) for r in gr), 4),
                }
            res["conditions"][cond] = {"peak_vram_total_mb": peak.peak_mb, "groups": groups, "items": rows}
            print(f"[{mid}/{cond}] " + "; ".join(f"{g}: WER {v['wer_mean']:.3f} CER {v['cer_mean']:.3f} "
                                                f"p50 {v['latency_p50_s']}s" for g, v in groups.items()))
        out["models"][mid] = res
        del eng
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--manifests", required=True)
    ap.add_argument("--conditions", default="wide,phone")
    ap.add_argument("--forced", action="store_true")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="int8_float16")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default="stt_results.json")
    a = ap.parse_args()
    res = run(a.models.split(","), a.manifests.split(","), a.conditions.split(","), a.forced, a.device,
              a.compute_type, a.limit)
    print("written", write_json(a.out, res))


if __name__ == "__main__":
    main()
