"""LLM benchmark against a running llama-server (one model per run).

  python -m bench.llm_bench --base-url http://127.0.0.1:8081/v1 --model-id qwen3-4b

For each scenario: first-token latency, decode speed, tool-call correctness,
then (if a tool was called) a second request with the dev fixture result to
measure the spoken follow-up. Reply *script* is checked automatically; reply
*quality* is written out for human review (it is not auto-scored).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time

from voice_engine.business import StaticGateway
from voice_engine.config import BusinessCfg, LlmCfg
from voice_engine.llm import LlmClient
from voice_engine.prompts import build_system_prompt
from voice_engine.text import dominant_script_language

from .common import CODE, GpuPeak, environment, gpu_processes_mb, keyword_hits, load_yaml, write_json


def args_match(expected: dict, got: dict) -> bool:
    for k, v in (expected or {}).items():
        if k not in got:
            return False
        a, b = str(got[k]).strip().lower(), str(v).strip().lower()
        if a != b and a.replace(" ", "") != b.replace(" ", ""):
            return False
    return True


async def one_request(client: LlmClient, msgs, tools):
    t0 = time.perf_counter()
    text, calls, ttft, timings = "", [], None, {}
    async for ev in client.stream(msgs, tools):
        if ev.kind == "text":
            text += ev.text
            ttft = ttft or ev.ttft_s
        elif ev.kind == "tool_calls":
            calls = ev.tool_calls
            ttft = ttft or ev.ttft_s
        elif ev.kind == "done":
            timings = ev.timings
    return {"text": text.strip(), "calls": calls, "ttft_s": ttft, "total_s": time.perf_counter() - t0,
            "decode_tps": timings.get("predicted_per_second"), "prompt_tps": timings.get("prompt_per_second")}


async def run(base_url: str, model_id: str, repeats: int) -> dict:
    catalog = {m["id"]: m for m in load_yaml("config/models.yaml")["llm"]}
    meta = catalog.get(model_id, {})
    cfg = LlmCfg(base_url=base_url, chat_template_kwargs=meta.get("chat_template_kwargs", {}) or {})
    client = LlmClient(cfg, os.environ.get(cfg.api_key_env, ""))
    if not await client.health():
        raise SystemExit(f"llama-server not healthy at {base_url}")
    gw = StaticGateway(BusinessCfg(), CODE / "config/dev_business.yaml", CODE / "logs/bench_sessions")
    ctx = await gw.start_session({"session_id": "bench"})
    system = {"role": "system", "content": build_system_prompt(ctx)}
    scenarios = load_yaml("bench/prompts.yaml")["llm_scenarios"]

    await one_request(client, [system, {"role": "user", "content": "Hello"}], ctx.tools)   # warm-up
    rows = []
    with GpuPeak() as peak:
        for sc in scenarios:
            for rep in range(repeats):
                msgs = [system, {"role": "user", "content": sc["user"]}]
                r1 = await one_request(client, msgs, ctx.tools)
                call = r1["calls"][0] if r1["calls"] else None
                row = {"id": sc["id"], "rep": rep, "input_kind": sc["input_kind"], "user": sc["user"],
                       "ttft_s": r1["ttft_s"], "total_s": r1["total_s"], "decode_tps": r1["decode_tps"],
                       "expect_tool": sc.get("expect_tool"), "tool": call.name if call else None,
                       "tool_args_raw": call.arguments_raw if call else None, "first_text": r1["text"]}
                args_ok = None
                if call:
                    try:
                        args = call.arguments()
                        args_ok = args_match(sc.get("expect_args", {}), args)
                    except ValueError:
                        args, args_ok = None, False
                    result = await gw.execute_tool(ctx, call.id, call.name, args or {})
                    msgs += [{"role": "assistant", "content": r1["text"] or None, "tool_calls": [
                        {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_raw}}]},
                        {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)}]
                    r2 = await one_request(client, msgs, ctx.tools)
                    row.update({"followup_ttft_s": r2["ttft_s"], "followup_total_s": r2["total_s"]})
                    reply = r2["text"]
                else:
                    reply = r1["text"]
                row["reply"] = reply
                row["tool_ok"] = (row["tool"] == sc.get("expect_tool"))
                row["args_ok"] = args_ok
                row["reply_script"] = dominant_script_language(reply) if reply else None
                row["script_ok"] = bool(reply) and row["reply_script"] == sc["expect_reply_script"]
                if sc.get("expect_reply_keywords"):
                    h, t = keyword_hits(reply, sc["expect_reply_keywords"], sc["expect_reply_script"])
                    row["reply_keywords"] = f"{h}/{t}"
                row["review_note"] = sc.get("review_note", "")
                rows.append(row)
                print(f"[{model_id}] {sc['id']}#{rep} tool={row['tool']} ok={row['tool_ok']} args={args_ok} "
                      f"script={row['reply_script']} ttft={row['ttft_s'] and round(row['ttft_s'], 3)}s :: {reply[:80]}")
    await client.close()

    def mean(key, rs):
        vals = [r[key] for r in rs if r.get(key) is not None]
        return round(statistics.mean(vals), 3) if vals else None

    summary = {"n": len(rows),
               "tool_selection_accuracy": round(sum(r["tool_ok"] for r in rows) / len(rows), 3),
               "tool_args_accuracy": (round(sum(bool(r["args_ok"]) for r in rows if r["args_ok"] is not None)
                                            / max(1, sum(r["args_ok"] is not None for r in rows)), 3)),
               "reply_script_accuracy": round(sum(r["script_ok"] for r in rows) / len(rows), 3),
               "ttft_mean_s": mean("ttft_s", rows), "followup_ttft_mean_s": mean("followup_ttft_s", rows),
               "decode_tps_mean": mean("decode_tps", rows),
               "by_input_kind": {k: {"script_accuracy": round(sum(r["script_ok"] for r in rs) / len(rs), 3),
                                     "tool_accuracy": round(sum(r["tool_ok"] for r in rs) / len(rs), 3)}
                                 for k in sorted({r["input_kind"] for r in rows})
                                 for rs in [[r for r in rows if r["input_kind"] == k]]}}
    return {"environment": environment(), "model_id": model_id, "license": meta.get("license"),
            "file": meta.get("file"), "gpu_processes_mb": gpu_processes_mb(), "peak_vram_total_mb": peak.peak_mb,
            "summary": summary, "rows": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8081/v1")
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--repeats", type=int, default=2)
    a = ap.parse_args()
    res = asyncio.run(run(a.base_url, a.model_id, a.repeats))
    print(json.dumps(res["summary"], indent=2))
    print("written", write_json(f"llm_{a.model_id}.json", res))


if __name__ == "__main__":
    main()
