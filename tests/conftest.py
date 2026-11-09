"""Shared fixtures: a local OpenAI-compatible SSE server and test doubles for GPU models.

The fake LLM server is a test fixture for the HTTP/streaming contract; it says
nothing about real model quality. Real models are exercised in Colab.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voice_engine.stt import SttResult  # noqa: E402


def sse(obj) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


class FakeLlm:
    """Scripted llama-server stand-in. Each request pops the next script entry."""

    def __init__(self):
        self.requests: list[dict] = []
        self.scripts: list[list[dict]] = []
        self.delay_s = 0.0
        self.drop_next = 0          # close the connection without answering (simulates "Server disconnected")

    async def handler(self, req: web.Request) -> web.StreamResponse:
        import asyncio
        body = await req.json()
        if self.drop_next > 0:
            self.drop_next -= 1
            req.transport.close()
            raise asyncio.CancelledError()
        self.requests.append(body)
        script = self.scripts.pop(0) if self.scripts else [{"text": "OK. "}]
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(req)
        for step in script:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if "text" in step:
                await resp.write(sse({"choices": [{"index": 0, "delta": {"content": step["text"]}}]}))
            if "tool" in step:
                name, args = step["tool"]
                half = len(args) // 2
                await resp.write(sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function", "function": {"name": name, "arguments": args[:half]}}]}}]}))
                await resp.write(sse({"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": args[half:]}}]}}]}))
        finish = "tool_calls" if any("tool" in s for s in script) else "stop"
        await resp.write(sse({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                              "timings": {"predicted_per_second": 42.0}}))
        await resp.write(b"data: [DONE]\n\n")
        return resp

    async def health(self, _req):
        return web.json_response({"status": "ok"})


@pytest.fixture
async def fake_llm():
    fl = FakeLlm()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fl.handler)
    app.router.add_get("/health", fl.health)
    server = TestServer(app)
    await server.start_server()
    fl.base_url = str(server.make_url("/v1"))
    yield fl
    await server.close()


class FakeVad:
    """Energy threshold in place of Silero (the endpointer logic is the thing under test)."""

    def prob(self, chunk):
        return 0.95 if float(np.sqrt(np.mean(chunk ** 2))) > 0.05 else 0.0

    def reset(self):
        pass


class FakeStt:
    def __init__(self, text="I want to check order 4821", language="en"):
        self.text, self.language, self.calls = text, language, 0

    def recognize(self, audio, hint="auto", previous=None):
        from voice_engine.config import SttCfg
        from voice_engine.stt import should_reject

        self.calls += 1
        reason = should_reject(self.text, -0.2, 0.01, SttCfg())   # the real rejection policy
        return SttResult(self.text, self.language, 0.99, -0.2, 0.01, len(audio) / 16000, 0.05,
                         rejected=bool(reason), reject_reason=reason)


class FakeTts:
    name, sample_rate, license_note = "fake", 16000, "test"

    def synthesize(self, text):
        yield np.zeros(int(0.02 * 16000 * max(1, len(text))), np.float32)  # 20 ms per character


def speech(seconds, amp=0.3):
    t = np.arange(int(16000 * seconds)) / 16000
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
