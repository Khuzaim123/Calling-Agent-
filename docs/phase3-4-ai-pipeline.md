# Phases 3–4: Colab infrastructure and AI voice pipeline

Status date: 2026-11-07. This document separates what was **executed** from what was only **written**.

## Status

| Component | Implemented | Tested | Where tested | Notes |
|---|---|---|---|---|
| Voice engine: audio utils, text/script detection, Urdu normalisation, sentence streaming | Yes | **Passed** | locally (pytest) | |
| VAD endpointing state machine (preroll, hysteresis, end-silence, forced cut, barge-in signal) | Yes | **Passed** | locally | Logic only. Silero itself not run. |
| STT language policy (restrict to en/ur, fold Hindi into Urdu, rejection) | Yes | **Passed** | locally | Policy only. faster-whisper not run. |
| LLM streaming client + tool-call fragment merging + reasoning stripping | Yes | **Passed** | locally, against a scripted SSE server | Real llama-server not run. |
| Session: turn-taking, tool calls via gateway, barge-in cancel + client flush, short-overlap ignore, rejected transcripts | Yes | **Passed** | locally, with model test doubles | Proves orchestration, not quality or latency. |
| WebSocket server: token auth, session limit, health | Yes | **Passed** | locally | |
| n8n gateway: HMAC signing, idempotency key on retry, unavailable-backend handling | Yes | **Passed** | locally, against a local HTTP server | The real n8n workflows don't exist until Phase 6. |
| Static dev gateway (fixtures tagged `_fixture`) | Yes | **Passed** | locally | Dev only. Not business logic. |
| Redmi 9C harness parsers (T1/T2/T3 dumps, T4 signal) | Yes | **Passed** | locally, canned adb output | **Not run on your phone.** |
| Colab notebook (57 cells) | Yes | Structure validated (nbformat, every cell parses, no undefined names across cells) | locally | **Not executed in Colab.** |
| faster-whisper STT on T4 | Yes | **Not tested** | needs Colab | |
| Silero VAD model | Yes | **Not tested** | needs Colab | |
| Kokoro TTS (English) | Yes | **Not tested** | needs Colab | Requires Python < 3.13. |
| MMS TTS (Urdu) | Yes | **Not tested** | needs Colab | CC-BY-NC: evaluation only. |
| Azure TTS reference | Yes | **Not tested** | needs key | Optional, paid. |
| llama.cpp build + llama-server | Yes | **Not tested** | needs Colab | First build ~10–25 min (estimate). |
| PostgreSQL install/bootstrap/backup/restore | Yes | **Not tested** | needs Colab | The schema itself is Phase 6. |
| n8n install/start on PostgreSQL | Yes | **Not tested** | needs Colab | |
| Cloudflare quick tunnel | Yes | **Not tested** | needs Colab | |
| Benchmarks (STT/LLM/TTS/VAD/concurrency/report) | Yes | **Not tested** | needs Colab | All benchmark numbers are pending. |
| Telephony: Redmi 9C via Bluetooth HFP gateway (`gateway/`, `voice_engine/telephony.py`) | Yes | Call flows **passed locally** with phone/audio test doubles | locally | Bluetooth, oFono and SCO audio **not tested on hardware**. See `docs/telephony-redmi9c-gateway.md`. SIP is not used. |

Local test run: `56 passed` (36 engine + 5 device-harness + 12 gateway + 3 benchmark-data), Python 3.13.2 on Windows, no GPU.

## Architecture (voice engine)

```
browser mic ──WSS──► cloudflared ──► voice_engine (aiohttp, 127.0.0.1:8765)
                                       │  per session:
                                       │  PCM16 16k ─► Silero VAD ─► Endpointer ─► utterance
                                       │                 └─ speech while agent talks ≥ 280 ms ─► barge-in: cancel + "clear"
                                       │  utterance ─► faster-whisper (GPU, shared 1-worker pool)
                                       │  text ─► llama-server /v1/chat/completions (stream, tools)
                                       │            tool_calls ─► BusinessGateway ─► n8n webhooks (Phase 7)
                                       │  sentences ─► TTS (GPU, shared 1-worker pool) ─► PCM ─► client
                                       ▼
                         logs/voice_engine/turns.jsonl (per-turn latency), session report ─► gateway.end_session
```

The engine holds **no business rules**. Business name, instructions, knowledge, customer memory, tool list and
tool results all come from the gateway. In Phase 7 that is n8n; until then, `config/dev_business.yaml`
provides clearly labelled fixtures.

**Latency of one turn** = end-of-speech silence (650 ms config) + STT + LLM time-to-first-sentence + TTS of
the first sentence. The engine logs each term separately (`turns.jsonl`) so it can be tuned with real data.
STT is per-utterance, not streaming partials. Streaming STT is a later optimisation, if measurements show
STT dominates.

## Model candidates and licences

| Role | Candidates | Licence note |
|---|---|---|
| STT | Whisper small / medium / large-v3-turbo (faster-whisper int8_float16) | MIT |
| LLM | Qwen2.5-3B, Qwen3-4B, Gemma-3-4B, Qwen2.5-7B, Qwen3-8B (GGUF Q4_K_M, llama.cpp) | **Qwen2.5-3B: Qwen Research License (non-commercial)**. Qwen2.5-7B / Qwen3: Apache-2.0. Gemma: Gemma Terms. |
| TTS en | Kokoro-82M | Apache-2.0 |
| TTS ur | MMS-TTS urd (baseline), Azure ur-PK (paid reference) | **MMS: CC-BY-NC-4.0 (non-commercial)** |
| VAD | Silero | MIT |

**Known gap: no permissively licensed, natural-sounding Urdu TTS is confirmed yet.** The next evaluation round
(not implemented) is VITS-OpenBible-Urdu (CC-BY-SA-4.0), Orpheus-Urdu (MIT fine-tune of a Llama-based base,
whose base licence applies), and F5-TTS-OpenBible-Urdu (CC-BY-SA-4.0). A paid Urdu TTS API is the fallback if none is
acceptable. This is a product decision to make after you score the listening sheet.

## T4 memory: sequential or concurrent?

Not decided yet, and deliberately so. Notebook section 16g (`bench.concurrency`) loads the active STT + TTS in
one process next to `llama-server`, then records:
- total VRAM resident, the per-process split, and the peak while overlapped;
- the latency of each stage alone vs. overlapped (two callers create exactly this overlap).

Decision rule once the numbers exist:
- **Keep everything resident and allow overlap** if the overlapped peak stays under ~90 % of VRAM and overlapped latency inflation is acceptable.
- **Otherwise keep the models resident but serialise GPU work.** The engine already does this for STT and TTS via single-worker pools; the LLM would get a single slot (`-np 1`).
- **Model swapping per turn is not considered.** Reloading weights costs seconds, which a live call can't absorb.

## Language handling

- STT restricts language ID to the business's allowed languages and folds Whisper's "Hindi" probability into
  Urdu. Spoken Urdu and Hindi overlap, and a Hindi transcription comes out in Devanagari, which nothing downstream can use.
- The LLM is instructed to reply in Urdu script for Urdu or mixed speech. TTS voice selection is per sentence by script.
- Roman Urdu is treated as a *text* phenomenon (SMS/WhatsApp, or STT emitting Latin script). It appears as an LLM test
  input; it is not assumed to be equivalent to spoken Urdu.
- **Known limitation:** MMS cannot pronounce Latin-script English words inside Urdu sentences (`t_mx*` TTS items
  measure this). Possible remedies after measurement: an LLM rule to write loanwords in Urdu script, a mixed-capable
  TTS, or transliteration.

## Colab limitations (development only)

- Sessions end on idle or at the maximum lifetime, and `/content` is wiped on reset. Re-run cells 1–14 to recover; Drive keeps models, caches and backups.
- PostgreSQL runs on local disk (Drive FUSE is unsuitable). Data written since the last 10-minute backup is lost on reset.
- The quick-tunnel URL changes on every start. A named tunnel (`CLOUDFLARE_TUNNEL_TOKEN`) gives a stable hostname.
- There's no inbound UDP, so SIP/RTP cannot terminate in Colab. The Redmi 9C gateway therefore connects *outbound* over the tunnel (WebSocket), and no SIP is needed.
- One T4 serves `max_sessions: 2` until load tests justify more.

## Benchmark data (existing public datasets, no recording)

| Set | Content | Licence / attribution |
|---|---|---|
| FLEURS `ur_pk`, `en_us` test | read sentences | CC-BY-4.0, Google (Conneau et al. 2022) |
| UrduSpeech benchmark CS / Std / EngPk (short clips, manually corrected) | real code-switched Urdu-English, Pakistani Urdu, Pakistani-accented English from podcasts, interviews, dramas, vlogs, news | CC-BY-4.0, ASLP-lab: Haq, Zhu, Hu, He, Xie, *UrduSpeech: A 156-Hour Urdu Speech Corpus with 12-Dimension Paralinguistic Annotations*, 2026, arXiv:2605.17846 |

The UrduSpeech loader was run against the live repository on 2026-11-08 (4 clips per subset): download,
manifest and 16 kHz loading all worked. Code-switched references mix Latin-script and Urdu-script English
words, so STT results add `wer_urdu_tokens` and `latin_word_recall` beside WER/CER.

## How to run

1. Upload this folder to `MyDrive/voice-agent/code_src/` (or push it to a private Git repo and set `GIT_REPO_URL`).
2. Open `notebooks/voice_agent_colab.ipynb` in Colab with a T4 runtime and run top to bottom.
3. Run the benchmarks (16a downloads the datasets, then 16c–16h), then score `results/phase4/tts_listening_sheet.csv` by ear.
4. Share `results/phase4/benchmark_report.md` and the `*.json` files. Model selection is made from those results.
