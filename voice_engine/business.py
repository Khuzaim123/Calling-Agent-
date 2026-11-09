"""Boundary between the voice engine and business logic (n8n).

The voice engine never decides business outcomes. It asks the gateway for:
  * session context (business profile, agent instructions, allowed tools,
    customer memory, language) at session start,
  * tool execution (orders, bookings, handoff, ...) during the call,
  * post-call processing at session end.

Gateways:
  N8nGateway    : production path. HMAC-signed HTTPS POSTs to n8n webhooks.
                  STATUS: implemented, NOT TESTED until the Phase 6 workflows exist.
  StaticGateway : development only. Loads a YAML profile and answers tool calls
                  from clearly labelled fixtures (`"_fixture": true`). It exists so
                  the audio/LLM pipeline can be exercised before n8n is built.
                  It is not business logic and must never be used in production.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import aiohttp
import yaml

from .config import BusinessCfg


@dataclass
class SessionContext:
    session_id: str
    business_id: str
    business_name: str
    agent_instructions: str
    language: str = "auto"                     # business default: auto | en | ur
    knowledge: list[str] = field(default_factory=list)
    customer: dict[str, Any] = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)   # OpenAI tool schema list
    greeting: dict[str, str] = field(default_factory=dict)  # per language
    max_call_seconds: int = 600
    recording_consent_required: bool = True


class BusinessGateway(Protocol):
    async def start_session(self, request: dict[str, Any]) -> SessionContext: ...
    async def execute_tool(self, ctx: SessionContext, call_id: str, name: str, args: dict) -> dict: ...
    async def end_session(self, ctx: SessionContext, report: dict[str, Any]) -> None: ...
    async def call_event(self, event: dict[str, Any]) -> None: ...   # phone call status (requested..ended)
    async def close(self) -> None: ...


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over '<timestamp>.<payload>' (hex), where payload is the JSON string sent in the
    request's "payload" field. n8n's Crypto node computes the same value and compares."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, max_skew_s: int = 300,
           now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        if abs(now - int(timestamp)) > max_skew_s:
            return False
    except ValueError:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def _tool_names(ctx: SessionContext) -> set[str]:
    return {t.get("function", {}).get("name") for t in ctx.tools}


class StaticGateway:
    def __init__(self, cfg: BusinessCfg, path: Path, log_dir: Path):
        self.cfg = cfg
        self.data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.log_dir = log_dir
        log_dir.mkdir(parents=True, exist_ok=True)

    async def start_session(self, request: dict[str, Any]) -> SessionContext:
        b = self.data["business"]
        outbound = request.get("direction") == "outbound"
        return SessionContext(
            session_id=request.get("session_id") or str(uuid.uuid4()),
            business_id=b["id"], business_name=b["name"],
            agent_instructions=b["agent_instructions"], language=b.get("language", "auto"),
            knowledge=b.get("knowledge", []), customer=self.data.get("customer", {}),
            tools=self.data.get("tools", []),
            greeting=b.get("greeting_outbound", b.get("greeting", {})) if outbound else b.get("greeting", {}),
            max_call_seconds=int(b.get("max_call_seconds", 600)),
        )

    async def call_event(self, event: dict[str, Any]) -> None:
        with (self.log_dir / "call_events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **event}, ensure_ascii=False) + "\n")

    async def execute_tool(self, ctx: SessionContext, call_id: str, name: str, args: dict) -> dict:
        if name not in _tool_names(ctx):
            return {"ok": False, "error": f"tool {name} is not enabled for this business"}
        fixture = self.data.get("tool_fixtures", {}).get(name)
        if fixture is None:
            return {"ok": False, "error": "no dev fixture for this tool", "_fixture": True}
        return {"ok": True, "_fixture": True, "result": fixture, "echo_args": args}

    async def end_session(self, ctx: SessionContext, report: dict[str, Any]) -> None:
        out = self.log_dir / f"session_{ctx.session_id}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    async def close(self) -> None:
        return None


class N8nGateway:
    """HMAC-signed webhook client. Endpoints are defined by the Phase 6 workflows:

    POST {base}/voice/session-start   -> SessionContext JSON
    POST {base}/voice/tool            -> {"ok": bool, "result"|"error": ...}
    POST {base}/voice/session-end     -> {"ok": true}
    POST {base}/voice/call-event      -> {"ok": true}   (phone call status changes)
    """

    def __init__(self, cfg: BusinessCfg, secret_value: str):
        if not secret_value:
            raise RuntimeError("N8N webhook HMAC secret is empty")
        self.cfg = cfg
        self.secret = secret_value
        self._http: aiohttp.ClientSession | None = None

    async def _post(self, path: str, payload: dict, idempotency_key: str) -> dict:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg.timeout_s))
        # The signed JSON travels as ONE string field, so n8n verifies exactly the bytes that were signed
        # (n8n re-serialises parsed bodies, which would change e.g. 1.0 -> 1 and break the signature).
        inner = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        body = json.dumps({"payload": inner}, ensure_ascii=False).encode()
        last: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            ts = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "X-VA-Timestamp": ts,
                "X-VA-Signature": sign(self.secret, ts, inner.encode()),
                "X-VA-Idempotency-Key": idempotency_key,   # same key on every retry
            }
            try:
                async with self._http.post(f"{self.cfg.n8n_base_url}{path}", data=body, headers=headers) as r:
                    text = await r.text()
                    if r.status >= 500:
                        raise RuntimeError(f"n8n {path} HTTP {r.status}")
                    if r.status != 200:
                        return {"ok": False, "error": f"n8n {path} HTTP {r.status}: {text[:200]}"}
                    return json.loads(text) if text else {"ok": True}
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                last = e
                await asyncio.sleep(0.3 * (2 ** attempt))
        raise RuntimeError(f"n8n {path} failed after retries: {last}")

    async def start_session(self, request: dict[str, Any]) -> SessionContext:
        res = await self._post("/voice/session-start", request, f"start:{request['session_id']}")
        if not res.get("ok", True) or "business_id" not in res:
            raise RuntimeError(f"session-start rejected: {res.get('error', 'missing business_id')}")
        allowed = {f for f in SessionContext.__dataclass_fields__}
        return SessionContext(**{k: v for k, v in res.items() if k in allowed})

    async def execute_tool(self, ctx: SessionContext, call_id: str, name: str, args: dict) -> dict:
        if name not in _tool_names(ctx):
            return {"ok": False, "error": f"tool {name} is not enabled for this business"}
        payload = {"session_id": ctx.session_id, "business_id": ctx.business_id,
                   "call_id": call_id, "tool": name, "arguments": args}
        try:
            return await self._post("/voice/tool", payload, f"tool:{ctx.session_id}:{call_id}")
        except RuntimeError as e:
            return {"ok": False, "error": "business system unavailable", "detail": str(e)}

    async def end_session(self, ctx: SessionContext, report: dict[str, Any]) -> None:
        await self._post("/voice/session-end", report, f"end:{ctx.session_id}")

    async def call_event(self, event: dict[str, Any]) -> None:
        key = f"call:{event.get('call_id')}:{event.get('status')}"     # one event per call state
        await self._post("/voice/call-event", event, key)

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
