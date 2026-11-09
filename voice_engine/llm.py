"""Streaming client for an OpenAI-compatible chat endpoint (llama.cpp llama-server).

Yields LlmEvent objects: text deltas, completed tool calls, and a final event
with timings. Tool *execution* is not done here: the session forwards tool
calls to the BusinessGateway (n8n).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import aiohttp

from .config import LlmCfg
from .text import strip_reasoning


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_raw: str

    def arguments(self) -> dict[str, Any]:
        """Parsed arguments; raises ValueError for invalid JSON or non-object."""
        try:
            val = json.loads(self.arguments_raw or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"tool {self.name}: arguments are not valid JSON: {e}") from e
        if not isinstance(val, dict):
            raise ValueError(f"tool {self.name}: arguments must be a JSON object")
        return val


@dataclass
class LlmEvent:
    kind: str                         # "text" | "tool_calls" | "done"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    ttft_s: float | None = None
    timings: dict[str, Any] = field(default_factory=dict)


class ToolCallAccumulator:
    """Merge streamed `delta.tool_calls` fragments (keyed by index)."""

    def __init__(self):
        self._calls: dict[int, dict[str, str]] = {}

    def add(self, fragments: list[dict]) -> None:
        for frag in fragments:
            idx = frag.get("index", 0)
            cur = self._calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if frag.get("id"):
                cur["id"] = frag["id"]
            fn = frag.get("function") or {}
            if fn.get("name"):
                cur["name"] += fn["name"]
            if fn.get("arguments"):
                cur["arguments"] += fn["arguments"]

    def result(self) -> list[ToolCall]:
        return [ToolCall(c["id"] or f"call_{i}", c["name"], c["arguments"])
                for i, c in sorted(self._calls.items()) if c["name"]]


class LlmClient:
    def __init__(self, cfg: LlmCfg, api_key: str = ""):
        self.cfg = cfg
        self.api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # force_close: no keep-alive. llama-server may close a connection right after a streamed
            # response; reusing it for the immediate tool follow-up request failed with
            # "Server disconnected" in the first Colab test (2026-11-08). Localhost, so no cost.
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=self.cfg.timeout_s, sock_read=self.cfg.timeout_s))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def health(self) -> bool:
        url = self.cfg.base_url.rsplit("/v1", 1)[0] + "/health"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            s = await self._http()
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as r:
                return r.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[LlmEvent]:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
            "chat_template_kwargs": self.cfg.chat_template_kwargs,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        t0 = time.perf_counter()
        ttft = None
        yielded = False
        s = await self._http()
        for attempt in (1, 2):
            acc = ToolCallAccumulator()
            finish = None
            timings: dict[str, Any] = {}
            in_think = False
            try:
                async with s.post(f"{self.cfg.base_url}/chat/completions", json=body, headers=headers) as r:
                    if r.status != 200:
                        raise RuntimeError(f"LLM HTTP {r.status}: {(await r.text())[:300]}")
                    async for raw in r.content:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if chunk.get("timings"):
                            timings = chunk["timings"]
                        for ch in chunk.get("choices", []):
                            delta = ch.get("delta") or {}
                            if delta.get("tool_calls"):
                                acc.add(delta["tool_calls"])
                            content = delta.get("content")
                            if content:
                                # Defensive: drop inline reasoning if a template emits it.
                                if "<think>" in content:
                                    in_think = True
                                if in_think:
                                    if "</think>" in content:
                                        in_think = False
                                        content = content.split("</think>", 1)[1]
                                    else:
                                        continue
                                content = strip_reasoning(content)
                                if content:
                                    if ttft is None:
                                        ttft = time.perf_counter() - t0
                                    yielded = True
                                    yield LlmEvent("text", text=content, ttft_s=ttft)
                            if ch.get("finish_reason"):
                                finish = ch["finish_reason"]
                break
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError):
                # safe to retry only if nothing reached the caller yet
                if yielded or attempt == 2:
                    raise
        calls = acc.result()
        if calls:
            if ttft is None:
                ttft = time.perf_counter() - t0
            yield LlmEvent("tool_calls", tool_calls=calls, finish_reason=finish, ttft_s=ttft)
        yield LlmEvent("done", finish_reason=finish, ttft_s=ttft, timings=timings)
