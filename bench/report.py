"""Render results/phase4/*.json into results/phase4/benchmark_report.md.

Only numbers present in result files are printed. Anything missing is shown
as "not run" so a partial run can never look like a complete one.
"""
from __future__ import annotations

import json

from .common import results_dir


def _load(name: str):
    p = results_dir() / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _f(v, nd=3):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def stt_section(d) -> list[str]:
    if not d:
        return ["## STT\n\nnot run\n"]
    out = [f"## STT\n\nGPU: {d['environment'].get('gpu')} | compute: {d['compute_type']} | "
           f"language mode: {d['language_mode']}\n",
           "| model | channel | group | n | WER | CER | WER Urdu words | English-word recall | lang acc | p50 s | p95 s | RTF | VRAM load MB |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for mid, m in d["models"].items():
        for cond, c in m["conditions"].items():
            for g, s in c["groups"].items():
                out.append(f"| {mid} | {cond} | {g} | {s['n']} | {_f(s['wer_mean'])} | {_f(s['cer_mean'])} | "
                           f"{_f(s.get('wer_urdu_tokens_mean'))} | {_f(s.get('latin_word_recall'))} | "
                           f"{_f(s['language_accuracy'])} | {_f(s['latency_p50_s'])} | "
                           f"{_f(s['latency_p95_s'])} | {_f(s['rtf_mean'])} | {_f(m['vram_load_mb'], 0)} |")
    return out + ["\n`WER Urdu words` scores Urdu-script words only; `English-word recall` = share of the reference's "
                  "Latin-script English words that STT also wrote in Latin. Language accuracy is not defined for "
                  "code-switched clips (`us_mixed`).\n"]


def llm_section(files) -> list[str]:
    if not files:
        return ["## LLM\n\nnot run\n"]
    out = ["## LLM\n", "| model | license | tool select | tool args | reply script | TTFT s | follow-up TTFT s | decode tok/s | peak VRAM MB |",
           "|---|---|---|---|---|---|---|---|---|"]
    for d in files:
        s = d["summary"]
        out.append(f"| {d['model_id']} | {d.get('license')} | {_f(s['tool_selection_accuracy'])} | "
                   f"{_f(s['tool_args_accuracy'])} | {_f(s['reply_script_accuracy'])} | {_f(s['ttft_mean_s'])} | "
                   f"{_f(s['followup_ttft_mean_s'])} | {_f(s['decode_tps_mean'], 1)} | {_f(d.get('peak_vram_total_mb'), 0)} |")
    out.append("\nReply script accuracy by input kind:\n")
    for d in files:
        out.append(f"- **{d['model_id']}**: " + ", ".join(
            f"{k}: script {_f(v['script_accuracy'])} / tool {_f(v['tool_accuracy'])}" for k, v in d["summary"]["by_input_kind"].items()))
    return out + ["\nReply content is NOT auto-scored. Review `rows[].reply` in each llm_*.json.\n"]


def tts_section(d) -> list[str]:
    if not d:
        return ["## TTS\n\nnot run\n"]
    out = ["## TTS\n", "| engine | status | license | first chunk s | RTF | VRAM load MB |", "|---|---|---|---|---|---|"]
    for name, e in d["engines"].items():
        if e["status"] != "ok":
            out.append(f"| {name} | {e['status']}: {e.get('error', '')[:80]} | - | - | - | - |")
            continue
        out.append(f"| {name} | ok | {e['license']} | {_f(e['first_chunk_mean_s'])} | {_f(e['rtf_mean'])} | {_f(e['vram_load_mb'], 0)} |")
    return out + [f"\nNaturalness: score by ear in `{d.get('listening_sheet')}`.\n"]


def vad_section(d) -> list[str]:
    if not d:
        return ["## VAD\n\nnot run\n"]
    out = ["## VAD (Silero, CPU)\n", f"CPU per 32 ms chunk: mean {d['cpu_ms_per_32ms_chunk']['mean']} ms, "
           f"p99 {d['cpu_ms_per_32ms_chunk']['p99']} ms\n", "| channel | n | detection rate | utterances/clip | coverage |", "|---|---|---|---|---|"]
    for c, s in d["speech"].items():
        out.append(f"| {c} | {s['n']} | {_f(s['detection_rate'])} | {_f(s['mean_utterances_per_clip'])} | {_f(s['mean_coverage_ratio'])} |")
    out.append("\n| noise | false utterances / 60 s |\n|---|---|")
    out += [f"| {k} | {v['false_utterances']} |" for k, v in d["noise"].items()]
    return out + [""]


def conc_section(d) -> list[str]:
    if not d:
        return ["## STT + TTS + LLM on one GPU\n\nnot run\n"]
    v = d["vram_mb"]
    out = ["## STT + TTS + LLM on one GPU\n",
           f"VRAM: all resident {_f(v['all_resident'], 0)} MB, peak overlapped {_f(v['peak_overlapped'], 0)} MB "
           f"of {_f(v['device_total'], 0)} MB. Per process: {v['per_process']}\n",
           "| stage | alone mean s | overlapped mean s | inflation x |", "|---|---|---|---|"]
    pairs = {"stt_s": "stt_s", "tts_s": "tts_en_s", "llm_ttft_s": "llm_ttft_s", "llm_total_s": "llm_total_s"}
    for k, a in pairs.items():
        al, ov = d["alone"].get(a), d["overlapped"].get(k)
        out.append(f"| {k} | {_f(al and al['mean'])} | {_f(ov and ov['mean'])} | {_f(d['latency_inflation_x'].get(k), 2)} |")
    return out + [""]


def main():
    llm_files = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(results_dir().glob("llm_*.json"))]
    lines = ["# Phase 4 benchmark report (generated)\n",
             "Generated from result files only. Sections marked 'not run' have no measurements.\n"]
    lines += stt_section(_load("stt_results.json"))
    lines += llm_section(llm_files)
    lines += tts_section(_load("tts_results.json"))
    lines += vad_section(_load("vad_results.json"))
    lines += conc_section(_load("concurrency_results.json"))
    out = results_dir() / "benchmark_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out.read_text(encoding="utf-8"))
    print("written", out)


if __name__ == "__main__":
    main()
