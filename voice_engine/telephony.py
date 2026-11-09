"""Telephony session management for one GSM line (the Redmi 9C via the HFP gateway).

This is call *plumbing*, not business logic: n8n decides whom to call and why
(POST /calls carries an opaque `context` that is handed back to n8n at
session start); this module only tracks the line, relays dial/hangup to the
gateway and reports call status back to n8n.

Control link (gateway -> engine, outbound from the home network, so no inbound ports):
  WS /ws/gateway
    gateway -> {"type":"hello","token":..., "line":{"name":..., "codec":...}}
    engine  -> {"type":"dial","call_id":..., "number":...} | {"type":"hangup","call_id":...}
    gateway -> {"type":"call_status","call_id":..., "direction":..., "remote_number":...,
                "status":"rejected|dialing|alerting|incoming|active|ended|failed", "reason":...}
Media for a live call uses the existing /ws protocol with channel "gsm".

HTTP for n8n (Bearer VOICE_ENGINE_CALLS_TOKEN):
  POST /calls                  {"number": "...", "context": {...}, "idempotency_key": "..."} -> 202 {"call_id"}
  GET  /calls/{call_id}        -> call record
  POST /calls/{call_id}/hangup -> 202
State is in memory (single engine, single line); the durable record is written
by n8n from the call events it receives.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

from aiohttp import WSMsgType, web

log = logging.getLogger("voice_engine.telephony")

FINAL = {"ended", "failed", "rejected"}
NUMBER_RE = re.compile(r"^\+?\d{7,15}$")
IDEMPOTENCY_TTL_S = 600


@dataclass
class CallRecord:
    call_id: str
    direction: str                     # outbound | inbound
    remote_number: str
    status: str
    context: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    idempotency_key: str = ""


class GatewayHub:
    def __init__(self, gateway_token: str, calls_token: str, business_gateway=None, keep: int = 200):
        self.gateway_token = gateway_token
        self.calls_token = calls_token
        self.business = business_gateway
        self.ws: web.WebSocketResponse | None = None
        self.line: dict[str, Any] = {}
        self.calls: OrderedDict[str, CallRecord] = OrderedDict()
        self.keep = keep
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ state
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed

    def active_call(self) -> CallRecord | None:
        for c in reversed(self.calls.values()):
            if c.status not in FINAL:
                return c
        return None

    def context_for(self, call_id: str) -> dict[str, Any]:
        c = self.calls.get(call_id)
        return dict(c.context) if c else {}

    def health(self) -> dict[str, Any]:
        a = self.active_call()
        return {"connected": self.connected(), "line": self.line.get("name"),
                "active_call": a.call_id if a else None}

    def _store(self, rec: CallRecord) -> None:
        self.calls[rec.call_id] = rec
        while len(self.calls) > self.keep:
            self.calls.popitem(last=False)

    def _emit(self, rec: CallRecord) -> None:
        if self.business is None or not hasattr(self.business, "call_event"):
            return
        payload = {k: v for k, v in asdict(rec).items() if k != "idempotency_key"}

        async def send():
            try:
                await self.business.call_event(payload)
            except Exception as e:      # status delivery must not break the call
                log.error("call_event delivery failed for %s: %s", rec.call_id, e)
        asyncio.ensure_future(send())

    # ------------------------------------------------------------------ actions
    async def dial(self, number: str, context: dict, idempotency_key: str = "") -> CallRecord:
        async with self._lock:
            if idempotency_key:
                for c in self.calls.values():
                    if c.idempotency_key == idempotency_key and time.time() - c.created < IDEMPOTENCY_TTL_S:
                        return c                                   # duplicate request -> same call
            if not self.connected():
                raise web.HTTPServiceUnavailable(text=json.dumps({"error": "gateway_not_connected"}),
                                                 content_type="application/json")
            if self.active_call():
                raise web.HTTPConflict(text=json.dumps({"error": "line_busy"}), content_type="application/json")
            rec = CallRecord(call_id=str(uuid.uuid4()), direction="outbound", remote_number=number,
                             status="requested", context=context, idempotency_key=idempotency_key)
            self._store(rec)
            await self.ws.send_str(json.dumps({"type": "dial", "call_id": rec.call_id, "number": number}))
        self._emit(rec)
        return rec

    async def hangup(self, call_id: str) -> bool:
        if call_id not in self.calls or not self.connected():
            return False
        await self.ws.send_str(json.dumps({"type": "hangup", "call_id": call_id}))
        return True

    def on_status(self, msg: dict) -> CallRecord | None:
        call_id = str(msg.get("call_id", ""))
        status = str(msg.get("status", ""))
        if not call_id or status not in FINAL | {"dialing", "alerting", "incoming", "active"}:
            log.warning("ignoring malformed call_status: %s", msg)
            return None
        rec = self.calls.get(call_id)
        if rec is None:                                            # inbound call announced by the gateway
            rec = CallRecord(call_id=call_id, direction=str(msg.get("direction", "inbound")),
                             remote_number=str(msg.get("remote_number", ""))[:32], status=status)
            self._store(rec)
        rec.status, rec.reason, rec.updated = status, str(msg.get("reason", ""))[:120], time.time()
        self._emit(rec)
        return rec

    # ------------------------------------------------------------------ HTTP / WS handlers
    def _auth(self, req: web.Request) -> None:
        h = req.headers.get("Authorization", "")
        if not (h.startswith("Bearer ") and hmac.compare_digest(h[7:], self.calls_token)):
            raise web.HTTPUnauthorized()

    async def http_post_call(self, req: web.Request) -> web.Response:
        self._auth(req)
        try:
            body = await req.json()
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="invalid JSON")
        number = re.sub(r"[\s-]", "", str(body.get("number", "")))
        if not NUMBER_RE.match(number):
            raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_number"}), content_type="application/json")
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        if len(json.dumps(context)) > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(json.dumps(context)))
        rec = await self.dial(number, context, str(body.get("idempotency_key", ""))[:128])
        return web.json_response({"call_id": rec.call_id, "status": rec.status}, status=202)

    async def http_get_call(self, req: web.Request) -> web.Response:
        self._auth(req)
        rec = self.calls.get(req.match_info["call_id"])
        if rec is None:
            raise web.HTTPNotFound()
        return web.json_response({k: v for k, v in asdict(rec).items() if k != "idempotency_key"})

    async def http_hangup(self, req: web.Request) -> web.Response:
        self._auth(req)
        ok = await self.hangup(req.match_info["call_id"])
        if not ok:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True}, status=202)

    async def ws_control(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16384)
        await ws.prepare(req)
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=5)
            hello = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
        except (asyncio.TimeoutError, json.JSONDecodeError):
            hello = {}
        if hello.get("type") != "hello" or not hmac.compare_digest(str(hello.get("token", "")), self.gateway_token):
            log.warning("gateway control auth failure from %s", req.remote)
            await ws.close(code=4401, message=b"auth failed")
            return ws
        if self.connected():
            await self.ws.close(code=4409, message=b"replaced by new gateway connection")
        self.ws = ws
        self.line = hello.get("line") if isinstance(hello.get("line"), dict) else {}
        log.info("gateway connected: %s", self.line)
        await ws.send_str(json.dumps({"type": "welcome"}))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "call_status":
                    self.on_status(data)
        finally:
            if self.ws is ws:
                self.ws = None
                a = self.active_call()
                if a and a.status in ("requested",):
                    self.on_status({"call_id": a.call_id, "status": "failed", "reason": "gateway_disconnected"})
            log.info("gateway disconnected")
        return ws

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/ws/gateway", self.ws_control)
        app.router.add_post("/calls", self.http_post_call)
        app.router.add_get("/calls/{call_id}", self.http_get_call)
        app.router.add_post("/calls/{call_id}/hangup", self.http_hangup)
