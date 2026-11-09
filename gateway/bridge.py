"""Call lifecycle: oFono events + voice-engine control link -> per-call media sessions.

Outbound: engine "dial" -> policy -> modem.dial -> dialing/alerting -> active -> media session.
Inbound : modem "incoming" -> policy (allowlist, ring delay, engine reachable) -> answer -> media.
A call answered on the phone by you (not by the gateway) is never given to the AI.
Any failure of the media path hangs up rather than leaving the remote party in silence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp

from .config import GatewayCfg
from .ofono import CallEvent
from .policy import Policy

log = logging.getLogger("gateway.bridge")


@dataclass
class Call:
    path: str
    call_id: str
    direction: str
    number: str
    ours: bool                       # placed or answered by the gateway
    state: str = ""
    reported: bool = False           # only calls the agent may handle are reported (privacy of your own calls)
    timed_out: bool = False
    active_at: float | None = None
    media: asyncio.Task | None = None
    timers: list = field(default_factory=list)


class Bridge:
    def __init__(self, cfg: GatewayCfg, modem, audio_factory: Callable[[], Any], policy: Policy,
                 http: aiohttp.ClientSession | None = None):
        self.cfg, self.modem, self.audio_factory, self.policy = cfg, modem, audio_factory, policy
        self.http = http
        self.control: aiohttp.ClientWebSocketResponse | None = None
        self.calls: dict[str, Call] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ status to engine
    async def status(self, call: Call | None, status: str, reason: str = "", call_id: str = "",
                     number: str = "", direction: str = "outbound") -> None:
        msg = {"type": "call_status", "call_id": call.call_id if call else call_id, "status": status,
               "reason": reason, "direction": call.direction if call else direction,
               "remote_number": call.number if call else number}
        log.info("call %s -> %s %s", msg["call_id"], status, reason)
        if self.control is not None and not self.control.closed:
            try:
                await self.control.send_str(json.dumps(msg))
            except (aiohttp.ClientError, ConnectionResetError) as e:
                log.warning("could not report status: %s", e)

    def busy(self) -> bool:
        return any(c.state not in ("disconnected", "") or c.media for c in self.calls.values())

    # ------------------------------------------------------------------ engine commands
    async def handle_dial(self, call_id: str, number: str) -> None:
        if not self.modem.ready():
            return await self.status(None, "rejected", "phone_not_connected", call_id, number)
        if self.busy():
            return await self.status(None, "rejected", "line_busy", call_id, number)
        n, reason = self.policy.check_outbound(number)
        if n is None:
            return await self.status(None, "rejected", reason, call_id, number)
        self.policy.record_dial()
        try:
            path = await self.modem.dial(n)
        except Exception as e:
            return await self.status(None, "failed", f"dial_error: {e}"[:120], call_id, n)
        prev = self.calls.get(path)        # oFono may announce the call before Dial() returns
        call = Call(path=path, call_id=call_id, direction="outbound", number=n, ours=True,
                    state=prev.state if prev else "dialing", reported=True)
        self.calls[path] = call
        call.timers.append(asyncio.create_task(self._dial_timeout(call)))
        await self.status(call, "dialing")

    async def handle_hangup(self, call_id: str) -> None:
        for c in self.calls.values():
            if c.call_id == call_id:
                await self.modem.hangup(c.path)

    async def _dial_timeout(self, call: Call) -> None:
        await asyncio.sleep(self.cfg.policy.dial_timeout_s)
        if call.active_at is None and call.path in self.calls:
            log.info("call %s not answered in %ss", call.call_id, self.cfg.policy.dial_timeout_s)
            call.timed_out = True
            await self.modem.hangup(call.path)

    async def _answer_later(self, call: Call) -> None:
        await asyncio.sleep(self.cfg.policy.answer_after_s)
        if call.state in ("incoming", "waiting") and call.path in self.calls:
            if self.control is None or self.control.closed:
                log.info("not answering %s: voice engine not connected", call.number)
                call.ours = False
                return
            call.ours = True
            await self.modem.answer(call.path)

    async def _max_duration(self, call: Call) -> None:
        await asyncio.sleep(self.cfg.policy.max_call_seconds)
        log.info("call %s reached max_call_seconds", call.call_id)
        await self.modem.hangup(call.path)

    # ------------------------------------------------------------------ modem events
    async def on_event(self, ev: CallEvent) -> None:
        if ev.kind == "added":
            if ev.path in self.calls:                      # our own dial
                self.calls[ev.path].state = ev.state
                return
            if ev.state in ("incoming", "waiting"):
                call = Call(path=ev.path, call_id=str(uuid.uuid4()), direction="inbound",
                            number=ev.number, ours=False, state=ev.state)
                self.calls[ev.path] = call
                ok, reason = self.policy.should_answer(ev.number)
                if not ok:                                 # a personal call: never leaves the gateway
                    log.info("incoming call left to the handset (%s)", reason)
                    return
                call.reported = True
                await self.status(call, "incoming")
                if not self.busy_other(call):
                    call.timers.append(asyncio.create_task(self._answer_later(call)))
            else:                                          # you dialled on the handset: not ours
                self.calls[ev.path] = Call(ev.path, str(uuid.uuid4()), "outbound", ev.number, False, ev.state)
        elif ev.kind == "state":
            call = self.calls.get(ev.path)
            if call is None:
                return
            call.state = ev.state
            if ev.state == "alerting" and call.ours:
                await self.status(call, "alerting")
            elif ev.state == "active" and call.active_at is None:
                call.active_at = time.monotonic()
                if call.ours:
                    await self.status(call, "active")
                    call.timers.append(asyncio.create_task(self._max_duration(call)))
                    call.media = asyncio.create_task(self._media(call))
                else:
                    log.info("call with %s answered on the handset: not bridging to the AI", call.number)
        elif ev.kind == "removed":
            call = self.calls.pop(ev.path, None)
            if call is not None:
                await self._finish(call)
        elif ev.kind == "modem_lost":
            for call in list(self.calls.values()):
                self.calls.pop(call.path, None)
                await self._finish(call, reason="phone_disconnected")

    def busy_other(self, call: Call) -> bool:
        return any(c is not call and c.media for c in self.calls.values())

    async def _finish(self, call: Call, reason: str = "") -> None:
        for t in call.timers:
            t.cancel()
        if call.media:
            call.media.cancel()
            try:
                await call.media
            except (asyncio.CancelledError, Exception):
                pass
        if not call.ours:
            if call.reported:
                await self.status(call, "ended", reason or "not_answered_by_agent")
            return
        if call.active_at is not None:
            dur = time.monotonic() - call.active_at
            if call.direction == "outbound":
                self.policy.record_outbound_seconds(dur)
            await self.status(call, "ended", reason or f"duration_s={int(dur)}")
        else:
            await self.status(call, "failed", reason or ("no_answer" if call.timed_out
                                                         else "not_answered_or_rejected"))

    # ------------------------------------------------------------------ media
    async def _media(self, call: Call) -> None:
        audio = self.audio_factory()
        try:
            await audio.open()
            async with self.http.ws_connect(self.cfg.ws_url("/ws"), heartbeat=20) as ws:
                await ws.send_str(json.dumps({"type": "start", "token": self.cfg.token, "channel": "gsm",
                                              "call_id": call.call_id, "direction": call.direction,
                                              "caller": {"number": call.number}}))

                async def uplink():
                    async for frame in audio.frames():
                        await ws.send_bytes(frame)

                up = asyncio.create_task(uplink())
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            audio.play(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("type") == "clear":
                                audio.clear()
                            elif data.get("type") == "bye":
                                break
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    up.cancel()
            log.info("media session for %s ended by the engine", call.call_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("media path failed for %s: %s", call.call_id, e)
        finally:
            await audio.close()
        if call.path in self.calls:                        # engine side ended: end the phone call too
            await self.modem.hangup(call.path)

    # ------------------------------------------------------------------ main loops
    async def control_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with self.http.ws_connect(self.cfg.ws_url("/ws/gateway"), heartbeat=20) as ws:
                    await ws.send_str(json.dumps({"type": "hello", "token": self.cfg.token,
                                                  "line": {"name": self.cfg.line_name, "rate": self.cfg.rate}}))
                    first = await ws.receive(timeout=10)
                    if first.type != aiohttp.WSMsgType.TEXT or json.loads(first.data).get("type") != "welcome":
                        raise RuntimeError(f"engine refused gateway: {first.type} {getattr(ws, 'close_code', '')}")
                    self.control, backoff = ws, 1.0
                    log.info("connected to voice engine control link")
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        if data.get("type") == "dial":
                            asyncio.create_task(self.handle_dial(str(data["call_id"]), str(data["number"])))
                        elif data.get("type") == "hangup":
                            asyncio.create_task(self.handle_hangup(str(data["call_id"])))
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, OSError) as e:
                log.warning("control link down: %s (retry in %.0fs)", e, backoff)
            self.control = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def event_loop(self) -> None:
        while not self._stop.is_set():
            ev = await self.modem.events.get()
            try:
                await self.on_event(ev)
            except Exception:
                log.exception("error handling %s", ev)

    async def run(self) -> None:
        own_http = self.http is None
        if own_http:
            self.http = aiohttp.ClientSession()
        try:
            await self.modem.start()
            await asyncio.gather(self.control_loop(), self.event_loop())
        finally:
            if own_http:
                await self.http.close()

    def stop(self) -> None:
        self._stop.set()
