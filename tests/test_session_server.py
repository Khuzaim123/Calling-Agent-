"""Session orchestration and WebSocket server with test doubles for the GPU models.

What this proves: turn-taking, tool-call routing through the gateway, barge-in
cancellation and audio flush, auth and limits. What it does NOT prove: STT/LLM/TTS
quality or latency (Colab benchmarks cover those).
"""
import asyncio
import json

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from voice_engine.audio import float_to_pcm16
from voice_engine.business import StaticGateway
from voice_engine.config import BusinessCfg, Config
from voice_engine.llm import LlmClient
from voice_engine.metrics import JsonlLogger
from voice_engine.server import make_app
from voice_engine.session import Models, VoiceSession
from voice_engine.tts import TtsRouter

from conftest import ROOT, FakeStt, FakeTts, FakeVad, speech


class Recorder:
    output_rate = 16000

    def __init__(self):
        self.events, self.audio_bytes = [], 0

    async def send_json(self, obj):
        self.events.append(obj)

    async def send_audio(self, pcm):
        self.audio_bytes += len(pcm)

    def of(self, t):
        return [e for e in self.events if e["type"] == t]


def make_cfg(tmp_path, base_url):
    cfg = Config(project_root=str(ROOT))
    cfg.llm.base_url = base_url
    cfg.log.dir = str(tmp_path / "logs")
    cfg.vad.end_silence_ms = 320
    cfg.vad.barge_in_min_speech_ms = 200
    return cfg


def make_session(tmp_path, fake_llm, stt=None, greeting=False):
    cfg = make_cfg(tmp_path, fake_llm.base_url)
    models = Models(stt=stt or FakeStt(), tts=TtsRouter({"en": FakeTts(), "ur": FakeTts()}),
                    llm=LlmClient(cfg.llm), vad_factory=FakeVad)
    gw = StaticGateway(BusinessCfg(), ROOT / "config/dev_business.yaml", tmp_path / "sessions")
    if not greeting:
        gw.data["business"]["greeting"] = {}
    rec = Recorder()
    s = VoiceSession(cfg, models, gw, rec, JsonlLogger(tmp_path / "turns.jsonl"), {"session_id": "t1"})
    return s, rec, models


async def feed(s, audio):
    for i in range(0, len(audio), 320):
        await s.on_audio(float_to_pcm16(audio[i:i + 320]))


async def wait_done(s, timeout=5):
    for _ in range(int(timeout / 0.02)):
        if s._resp_task is None or s._resp_task.done():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("response did not finish")


async def test_full_turn_with_tool_call(tmp_path, fake_llm):
    fake_llm.scripts = [
        [{"tool": ("lookup_order", json.dumps({"order_id": "4821"}))}],
        [{"text": "Your order is out for delivery. "}, {"text": "It will arrive in 15 minutes."}],
    ]
    s, rec, models = make_session(tmp_path, fake_llm)
    await s.start()
    assert rec.of("ready")[0]["tools"][0] == "lookup_order"
    await feed(s, np.concatenate([speech(1.0), np.zeros(16000, np.float32)]))
    await wait_done(s)
    await s.close("test")
    await models.llm.close()

    users = [e for e in rec.of("transcript") if e["role"] == "user"]
    bots = [e["text"] for e in rec.of("transcript") if e["role"] == "assistant"]
    assert users[0]["text"] == "I want to check order 4821"
    assert bots == ["Your order is out for delivery.", "It will arrive in 15 minutes."]
    assert rec.of("tool")[0] == {"type": "tool", "name": "lookup_order", "ok": True, "fixture": True}
    second = fake_llm.requests[1]["messages"]
    assert second[-1]["role"] == "tool" and json.loads(second[-1]["content"])["_fixture"] is True
    assert second[-2]["tool_calls"][0]["function"]["name"] == "lookup_order"
    assert rec.audio_bytes > 0
    turns = [json.loads(line) for line in (tmp_path / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert turns[0]["first_audio_s"] is not None and turns[0]["tool_calls"] == ["lookup_order"]
    report = json.loads((tmp_path / "sessions" / "session_t1.json").read_text(encoding="utf-8"))
    assert [t["role"] for t in report["transcript"]] == ["user", "assistant"]


async def test_barge_in_cancels_and_clears(tmp_path, fake_llm):
    fake_llm.delay_s = 0.05
    fake_llm.scripts = [[{"text": f"This is sentence number {i}. "} for i in range(40)],
                        [{"text": "Sure, go ahead."}]]
    s, rec, models = make_session(tmp_path, fake_llm)
    await s.start()
    await feed(s, np.concatenate([speech(0.8), np.zeros(8000, np.float32)]))
    for _ in range(100):                               # wait until the agent is talking
        if rec.audio_bytes:
            break
        await asyncio.sleep(0.02)
    assert s.agent_speaking()
    await feed(s, speech(0.5))                         # caller talks over the agent
    assert rec.of("clear"), "barge-in must tell the client to flush audio"
    await feed(s, np.zeros(8000, np.float32))
    await wait_done(s)
    await s.close("test")
    await models.llm.close()
    first = [m for m in s.messages if m["role"] == "assistant"][0]["content"]
    assert first.endswith("[interrupted by caller]") and "sentence number 39" not in first
    assert len(fake_llm.requests) == 2                 # the interruption became a new turn


async def test_short_overlap_does_not_interrupt(tmp_path, fake_llm):
    fake_llm.delay_s = 0.02
    fake_llm.scripts = [[{"text": f"Part {i}. "} for i in range(10)]]
    s, rec, models = make_session(tmp_path, fake_llm)
    s.cfg.vad.barge_in_min_speech_ms = 300
    await s.start()
    await feed(s, np.concatenate([speech(0.8), np.zeros(8000, np.float32)]))
    for _ in range(100):
        if rec.audio_bytes:
            break
        await asyncio.sleep(0.02)
    # 220 ms: long enough to count as speech (>160 ms), too short to barge in (<300 ms)
    await feed(s, np.concatenate([speech(0.22), np.zeros(8000, np.float32)]))
    await wait_done(s)
    await s.close("test")
    await models.llm.close()
    assert not rec.of("clear")
    assert any(e.get("state") == "ignored_short_overlap" for e in rec.of("vad"))
    assert len(fake_llm.requests) == 1


async def test_rejected_transcript_does_not_call_llm(tmp_path, fake_llm):
    s, rec, models = make_session(tmp_path, fake_llm, stt=FakeStt(text=""))
    await s.start()
    await feed(s, np.concatenate([speech(0.6), np.zeros(16000, np.float32)]))
    await wait_done(s)
    await s.close("test")
    await models.llm.close()
    assert rec.of("transcript")[0]["rejected"] is True and fake_llm.requests == []


async def test_ws_auth_and_session_limit(tmp_path, fake_llm, monkeypatch):
    monkeypatch.setenv("VOICE_ENGINE_WS_TOKEN", "t" * 32)
    cfg = make_cfg(tmp_path, fake_llm.base_url)
    cfg.server.max_sessions = 1
    models = Models(stt=FakeStt(), tts=TtsRouter({"en": FakeTts()}), llm=LlmClient(cfg.llm), vad_factory=FakeVad)
    gw = StaticGateway(BusinessCfg(), ROOT / "config/dev_business.yaml", tmp_path / "s")
    client = TestClient(TestServer(make_app(cfg, models, gw)))
    await client.start_server()
    try:
        health = await client.get("/healthz")
        assert health.status == 200 and (await health.json())["llm_reachable"] is True

        bad = await client.ws_connect("/ws")
        await bad.send_str(json.dumps({"type": "start", "token": "wrong"}))
        await bad.receive()
        assert bad.close_code == 4401

        good = await client.ws_connect("/ws")
        await good.send_str(json.dumps({"type": "start", "token": "t" * 32}))
        ready = json.loads((await good.receive()).data)
        assert ready["type"] == "ready" and ready["output_rate"] == 24000

        second = await client.ws_connect("/ws")
        await second.send_str(json.dumps({"type": "start", "token": "t" * 32}))
        await second.receive()
        assert second.close_code == 4429
        await good.send_str(json.dumps({"type": "stop"}))
        await good.close()
    finally:
        await client.close()
        await models.llm.close()


def test_server_refuses_weak_token(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_ENGINE_WS_TOKEN", "short")
    try:
        make_app(make_cfg(tmp_path, "http://x/v1"), models=object(), gateway=object())
    except RuntimeError as e:
        assert "24 characters" in str(e)
    else:
        raise AssertionError("weak token accepted")
