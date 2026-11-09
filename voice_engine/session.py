"""One live conversation: audio in -> VAD -> STT -> LLM (+tools via gateway) -> TTS -> audio out.

Transport-agnostic: the browser test client, or a phone call from the Redmi 9C HFP gateway.
Concurrency model:
  * one asyncio task per response (`_respond`), cancelled on barge-in;
  * inside it, the LLM stream feeds a sentence queue consumed by a speaker
    task, so TTS of sentence N overlaps generation of sentence N+1;
  * GPU work (STT, TTS) runs in shared single-worker executors so concurrent
    sessions queue instead of oversubscribing VRAM.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import numpy as np

from .audio import FrameAssembler, float_to_pcm16, pcm16_to_float, resample
from .business import BusinessGateway, SessionContext
from .config import Config
from .llm import LlmClient
from .metrics import JsonlLogger
from .prompts import build_system_prompt, trim_history, with_language_hint
from .stt import SttEngine
from .text import SentenceStreamer, dominant_script_language
from .tts import TtsRouter
from .vad import CHUNK, Endpointer, SileroVad

log = logging.getLogger("voice_engine.session")


class Transport(Protocol):
    output_rate: int

    async def send_json(self, obj: dict) -> None: ...
    async def send_audio(self, pcm16: bytes) -> None: ...


@dataclass
class Models:
    stt: SttEngine
    tts: TtsRouter
    llm: LlmClient
    vad_factory: Callable[[], SileroVad]
    stt_pool: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(1, thread_name_prefix="stt"))
    tts_pool: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(1, thread_name_prefix="tts"))


@dataclass
class TurnMetrics:
    turn: int
    speech_end: float
    utterance_s: float = 0.0
    stt_s: float | None = None
    stt_language: str | None = None
    llm_ttft_s: float | None = None
    first_sentence_s: float | None = None
    first_audio_s: float | None = None     # speech end -> first audio byte sent
    tool_calls: list[str] = field(default_factory=list)
    interrupted: bool = False
    error: str | None = None

    def since(self) -> float:
        return time.perf_counter() - self.speech_end


class VoiceSession:
    def __init__(self, cfg: Config, models: Models, gateway: BusinessGateway, transport: Transport,
                 metrics_log: JsonlLogger, request: dict[str, Any]):
        self.cfg = cfg
        self.m = models
        self.gw = gateway
        self.tx = transport
        self.mlog = metrics_log
        self.request = {**request, "session_id": request.get("session_id") or str(uuid.uuid4())}
        self.session_id = self.request["session_id"]
        self.ctx: SessionContext | None = None
        self.messages: list[dict] = []
        self.transcript: list[dict] = []
        self.vad = models.vad_factory()
        self.endpointer = Endpointer(cfg.vad)
        self.assembler = FrameAssembler(CHUNK)
        self._resp_task: asyncio.Task | None = None
        self._play_until = 0.0
        self._barged = False
        self._turn = 0
        self._language: str | None = None
        self._started = time.time()
        self._closed = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.ctx = await self.gw.start_session(self.request)
        self.messages = [{"role": "system", "content": build_system_prompt(self.ctx)}]
        lang = self.ctx.language if self.ctx.language in ("en", "ur") else "en"
        self._language = lang
        await self.tx.send_json({"type": "ready", "session_id": self.session_id,
                                 "business": self.ctx.business_name, "output_rate": self.tx.output_rate,
                                 "tools": [t["function"]["name"] for t in self.ctx.tools]})
        greeting = self.ctx.greeting.get(lang)
        if greeting:
            self._resp_task = asyncio.create_task(self._speak_only(greeting))

    async def close(self, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        await self._cancel_response()
        report = {"session_id": self.session_id, "business_id": self.ctx.business_id if self.ctx else None,
                  "started_at": self._started, "ended_at": time.time(), "end_reason": reason,
                  "transcript": self.transcript, "turns": self._turn}
        if self.ctx:
            try:
                await self.gw.end_session(self.ctx, report)
            except Exception as e:  # post-call failure must not crash the engine
                log.error("end_session failed for %s: %s", self.session_id, e)

    def expired(self) -> bool:
        limit = min(self.cfg.server.max_session_seconds,
                    self.ctx.max_call_seconds if self.ctx else self.cfg.server.max_session_seconds)
        return time.time() - self._started > limit

    # ------------------------------------------------------------------ audio in
    def agent_speaking(self) -> bool:
        active = self._resp_task is not None and not self._resp_task.done()
        return active or time.perf_counter() < self._play_until

    async def on_audio(self, pcm16: bytes) -> None:
        for frame in self.assembler.push(pcm16):
            chunk = pcm16_to_float(frame)
            prob = self.vad.prob(chunk)
            for ev in self.endpointer.push(chunk, prob):
                if ev.kind == "speech_start":
                    self._barged = False
                    await self.tx.send_json({"type": "vad", "state": "speech_start"})
                elif ev.kind == "speech":
                    if (not self._barged and self.agent_speaking()
                            and ev.speech_ms >= self.cfg.vad.barge_in_min_speech_ms):
                        await self._barge_in()
                elif ev.kind == "utterance":
                    await self.tx.send_json({"type": "vad", "state": "speech_end", "forced": ev.forced})
                    if self.agent_speaking() and not self._barged:
                        # Short sound below the barge-in threshold while the agent talks
                        # (cough, "mm-hmm", residual echo): do not abandon the reply.
                        await self.tx.send_json({"type": "vad", "state": "ignored_short_overlap"})
                        continue
                    await self._cancel_response()
                    self._turn += 1
                    tm = TurnMetrics(turn=self._turn, speech_end=time.perf_counter(),
                                     utterance_s=len(ev.audio) / 16000)
                    self._resp_task = asyncio.create_task(self._respond(ev.audio, tm))

    async def _barge_in(self) -> None:
        self._barged = True
        await self._cancel_response()
        self._play_until = 0.0
        await self.tx.send_json({"type": "clear"})          # client must drop queued audio now
        log.info("[%s] barge-in", self.session_id)

    async def _cancel_response(self) -> None:
        t = self._resp_task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error("[%s] response task error on cancel: %s", self.session_id, e)
        self._resp_task = None

    # ------------------------------------------------------------------ response
    async def _respond(self, audio: np.ndarray, tm: TurnMetrics) -> None:
        loop = asyncio.get_running_loop()
        spoken: list[str] = []
        try:
            hint = self.ctx.language if self.ctx else "auto"
            res = await loop.run_in_executor(self.m.stt_pool, self.m.stt.recognize, audio, hint, self._language)
            tm.stt_s, tm.stt_language = tm.since(), res.language
            await self.tx.send_json({"type": "transcript", "role": "user", "text": res.text,
                                     "language": res.language, "rejected": res.rejected,
                                     "reason": res.reject_reason, "stt_s": round(res.elapsed_s, 3)})
            if res.rejected:
                return
            self._language = res.language
            self.transcript.append({"role": "user", "text": res.text, "language": res.language, "t": time.time()})
            if len(self.messages) > 1 and self.messages[-1]["role"] == "user":
                self.messages[-1]["content"] += " " + res.text      # user continued after an interruption
            else:
                self.messages.append({"role": "user", "content": res.text})

            queue: asyncio.Queue[str | None] = asyncio.Queue()
            speaker = asyncio.create_task(self._speaker(queue, tm, spoken))
            try:
                await self._generate(queue, tm)
            finally:
                await queue.put(None)
                await speaker
        except asyncio.CancelledError:
            tm.interrupted = True
            raise
        except Exception as e:
            tm.error = f"{type(e).__name__}: {e}"
            log.exception("[%s] turn %d failed", self.session_id, tm.turn)
            await self.tx.send_json({"type": "error", "where": "turn", "message": str(e)[:200]})
        finally:
            text = " ".join(spoken).strip()
            if text:
                content = text + (" [interrupted by caller]" if tm.interrupted else "")
                self.messages.append({"role": "assistant", "content": content})
                self.transcript.append({"role": "assistant", "text": text, "interrupted": tm.interrupted,
                                        "t": time.time()})
            rec = {k: (round(v, 3) if isinstance(v, float) else v)
                   for k, v in tm.__dict__.items() if k != "speech_end"}
            self.mlog.write({"type": "turn", "session_id": self.session_id, **rec})
            if not self._closed:
                # fire-and-forget: this block may run while the task is being cancelled
                asyncio.ensure_future(self.tx.send_json({"type": "metrics", **rec}))

    async def _generate(self, queue: asyncio.Queue, tm: TurnMetrics) -> None:
        assert self.ctx is not None
        for _round in range(self.cfg.llm.max_tool_rounds + 1):
            streamer = SentenceStreamer()
            full = ""
            calls = []
            msgs = with_language_hint(trim_history(self.messages, self.cfg.llm.history_char_budget),
                                      self._language)
            async for ev in self.m.llm.stream(msgs, self.ctx.tools or None):
                if ev.kind == "text":
                    if tm.llm_ttft_s is None:
                        tm.llm_ttft_s = tm.since()
                    full += ev.text
                    for sentence in streamer.push(ev.text):
                        await self._queue_sentence(queue, sentence, tm)
                elif ev.kind == "tool_calls":
                    calls = ev.tool_calls
            for sentence in streamer.flush():
                await self._queue_sentence(queue, sentence, tm)
            if not calls:
                return
            self.messages.append({"role": "assistant", "content": full or None, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments_raw}}
                for c in calls]})
            for c in calls:
                tm.tool_calls.append(c.name)
                try:
                    args = c.arguments()
                    result = await self.gw.execute_tool(self.ctx, c.id, c.name, args)
                except ValueError as e:
                    result = {"ok": False, "error": str(e)}
                await self.tx.send_json({"type": "tool", "name": c.name, "ok": bool(result.get("ok")),
                                         "fixture": bool(result.get("_fixture"))})
                self.messages.append({"role": "tool", "tool_call_id": c.id,
                                      "content": json.dumps(result, ensure_ascii=False)})
        log.warning("[%s] tool loop limit (%d) reached", self.session_id, self.cfg.llm.max_tool_rounds)

    async def _queue_sentence(self, queue: asyncio.Queue, sentence: str, tm: TurnMetrics) -> None:
        if not sentence.strip():
            return
        if tm.first_sentence_s is None:
            tm.first_sentence_s = tm.since()
        await queue.put(sentence)

    async def _speaker(self, queue: asyncio.Queue, tm: TurnMetrics | None, spoken: list[str]) -> None:
        while True:
            sentence = await queue.get()
            if sentence is None:
                return
            await self._speak(sentence, tm)
            spoken.append(sentence)
            await self.tx.send_json({"type": "transcript", "role": "assistant", "text": sentence})

    async def _speak_only(self, text: str) -> None:
        await self._speak(text, None)
        self.transcript.append({"role": "assistant", "text": text, "t": time.time()})
        self.messages.append({"role": "assistant", "content": text})

    async def _speak(self, text: str, tm: TurnMetrics | None) -> None:
        engine = self.m.tts.for_language(dominant_script_language(text))
        loop = asyncio.get_running_loop()
        chunks = await loop.run_in_executor(self.m.tts_pool, lambda: list(engine.synthesize(text)))
        for chunk in chunks:
            out = resample(chunk, engine.sample_rate, self.tx.output_rate)
            if tm is not None and tm.first_audio_s is None:
                tm.first_audio_s = tm.since()
            await self.tx.send_audio(float_to_pcm16(out))
            now = time.perf_counter()
            self._play_until = max(now, self._play_until) + len(out) / self.tx.output_rate
