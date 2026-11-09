"""aiohttp server: /healthz, test client page, and the browser audio WebSocket.

WebSocket protocol (v1)
  client -> server
    text   {"type":"start","token":"<VOICE_ENGINE_WS_TOKEN>","caller":{...}}   (must be first, within 5 s)
    binary PCM16 little-endian, mono, 16 kHz, any frame size (20 ms recommended)
    text   {"type":"stop"}
  server -> client
    text   {"type":"ready"|"vad"|"transcript"|"tool"|"clear"|"error"|"bye", ...}
    binary PCM16 little-endian, mono, at `output_rate` from the ready message
Phone calls (telephony.enabled): the HFP gateway opens /ws per live call with
    {"type":"start","token":"<VOICE_ENGINE_GATEWAY_TOKEN>","channel":"gsm","call_id":...,
     "direction":"inbound|outbound","caller":{"number":...}}
  and the same binary framing at telephony.phone_rate in both directions.
  Control link and n8n call API: see voice_engine/telephony.py.
Close codes: 4401 auth failed, 4408 start timeout, 4429 too many sessions/flood, 4413 frame too large.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from .business import N8nGateway, StaticGateway
from .config import Config, secret
from .llm import LlmClient
from .metrics import JsonlLogger
from .session import Models, VoiceSession
from .stt import SttEngine
from .telephony import GatewayHub
from .tts import TtsRouter, build_engine
from .vad import SileroVad

log = logging.getLogger("voice_engine.server")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def load_models(cfg: Config) -> Models:
    t0 = time.perf_counter()
    stt = SttEngine(cfg.stt, cfg.path(cfg.stt.model_path))
    log.info("STT loaded in %.1fs", time.perf_counter() - t0)
    engines = {}
    loaded: dict[str, object] = {}
    azure_key = secret(cfg.tts.azure_key_env, required=False)
    for lang, name in cfg.tts.engine_by_language.items():
        key = name if name == "kokoro" else f"{name}:{lang}"     # mms/azure: a different model per language
        if key not in loaded:
            t1 = time.perf_counter()
            try:
                loaded[key] = build_engine(name, cfg.tts, cfg.path, lang, azure_key)
                log.info("TTS %s loaded in %.1fs (%s)", name, time.perf_counter() - t1, loaded[key].license_note)
            except Exception as e:
                log.error("TTS engine %s for %s failed to load: %s", name, lang, e)
                continue
        engines[lang] = loaded[key]
    llm = LlmClient(cfg.llm, secret(cfg.llm.api_key_env, required=False))
    SileroVad()  # load once at startup so a broken install fails fast
    return Models(stt=stt, tts=TtsRouter(engines), llm=llm, vad_factory=SileroVad)


def build_gateway(cfg: Config):
    if cfg.business.gateway == "n8n":
        return N8nGateway(cfg.business, secret(cfg.business.hmac_secret_env))
    if cfg.business.gateway == "static":
        return StaticGateway(cfg.business, cfg.code_path(cfg.business.static_file), cfg.path(cfg.log.dir) / "sessions")
    raise ValueError(f"unknown business.gateway {cfg.business.gateway!r}")


class WsTransport:
    def __init__(self, ws: web.WebSocketResponse, output_rate: int):
        self.ws = ws
        self.output_rate = output_rate

    async def send_json(self, obj: dict) -> None:
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def send_audio(self, pcm16: bytes) -> None:
        if not self.ws.closed:
            await self.ws.send_bytes(pcm16)


class App:
    def __init__(self, cfg: Config, models: Models | None = None, gateway=None):
        self.cfg = cfg
        self.models = models
        self.gateway = gateway
        self.token = secret(cfg.server.ws_token_env)
        if len(self.token) < 24:
            raise RuntimeError(f"{cfg.server.ws_token_env} must be at least 24 characters")
        self.sessions: dict[str, VoiceSession] = {}
        self.metrics = JsonlLogger(cfg.path(cfg.log.dir) / "turns.jsonl")
        self.hub: GatewayHub | None = None
        if cfg.telephony.enabled:
            tokens = {}
            for env in (cfg.telephony.gateway_token_env, cfg.telephony.calls_token_env):
                tokens[env] = secret(env)
                if len(tokens[env]) < 24:
                    raise RuntimeError(f"{env} must be at least 24 characters")
            if len(set(tokens.values()) | {self.token}) != 3:
                raise RuntimeError("browser, gateway and calls tokens must all be different")
            self.hub = GatewayHub(tokens[cfg.telephony.gateway_token_env], tokens[cfg.telephony.calls_token_env])

    async def on_startup(self, _app: web.Application) -> None:
        if self.models is None:
            self.models = await asyncio.get_running_loop().run_in_executor(None, load_models, self.cfg)
        if self.gateway is None:
            self.gateway = build_gateway(self.cfg)
        if self.hub is not None:
            self.hub.business = self.gateway

    async def on_cleanup(self, _app: web.Application) -> None:
        for s in list(self.sessions.values()):
            await s.close("server_shutdown")
        if self.models:
            await self.models.llm.close()
        if self.gateway:
            await self.gateway.close()

    async def healthz(self, _req: web.Request) -> web.Response:
        llm_ok = await self.models.llm.health() if self.models else False
        body = {"status": "ok" if (self.models and llm_ok) else "degraded",
                "models_loaded": self.models is not None, "llm_reachable": llm_ok,
                "tts_languages": sorted(self.models.tts.engines) if self.models else [],
                "sessions": len(self.sessions), "max_sessions": self.cfg.server.max_sessions,
                "business_gateway": self.cfg.business.gateway,
                "telephony": self.hub.health() if self.hub else {"enabled": False}}
        return web.json_response(body, status=200 if body["status"] == "ok" else 503)

    async def index(self, _req: web.Request) -> web.Response:
        return web.FileResponse(WEB_DIR / "client.html")

    async def ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=self.cfg.server.max_frame_bytes, heartbeat=20)
        await ws.prepare(req)
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=5)
        except asyncio.TimeoutError:
            await ws.close(code=4408, message=b"start timeout")
            return ws
        if first.type != WSMsgType.TEXT:
            await ws.close(code=4401, message=b"auth required")
            return ws
        try:
            start = json.loads(first.data)
        except json.JSONDecodeError:
            await ws.close(code=4401, message=b"bad start")
            return ws
        token = str(start.get("token", ""))
        is_browser = hmac.compare_digest(token, self.token)
        is_phone = (self.hub is not None and start.get("channel") == "gsm"
                    and hmac.compare_digest(token, self.hub.gateway_token))
        if start.get("type") != "start" or not (is_browser or is_phone):
            log.warning("WS auth failure from %s", req.remote)
            await ws.close(code=4401, message=b"auth failed")
            return ws
        if len(self.sessions) >= self.cfg.server.max_sessions:
            await ws.close(code=4429, message=b"too many sessions")
            return ws

        caller = start.get("caller") if isinstance(start.get("caller"), dict) else {}
        caller = {k: str(v)[:64] for k, v in caller.items()}
        if is_phone:
            call_id = str(start.get("call_id", ""))[:64]
            direction = "outbound" if start.get("direction") == "outbound" else "inbound"
            request = {"channel": "gsm", "call_id": call_id, "direction": direction, "caller": caller,
                       "line": self.hub.line.get("name"), "context": self.hub.context_for(call_id)}
            out_rate = self.cfg.telephony.phone_rate
        else:
            request = {"channel": "browser_test", "caller": caller}
            out_rate = self.cfg.output.browser_rate
        session = VoiceSession(self.cfg, self.models, self.gateway, WsTransport(ws, out_rate),
                               self.metrics, request)
        self.sessions[session.session_id] = session
        reason = "client_closed"
        try:
            await session.start()
            window_start, frames = time.monotonic(), 0
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    now = time.monotonic()
                    if now - window_start >= 1.0:
                        window_start, frames = now, 0
                    frames += 1
                    if frames > self.cfg.server.max_frames_per_second:
                        reason = "flood"
                        await ws.close(code=4429, message=b"frame rate exceeded")
                        break
                    if session.expired():
                        reason = "max_duration"
                        await ws.send_str(json.dumps({"type": "bye", "reason": reason}))
                        await ws.close()
                        break
                    await session.on_audio(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "stop":
                        reason = "client_stop"
                        break
                elif msg.type == WSMsgType.ERROR:
                    reason = "ws_error"
                    break
        except Exception as e:
            reason = f"error:{type(e).__name__}"
            log.exception("session %s crashed", session.session_id)
            await ws.close(code=1011, message=b"internal error")
        finally:
            await session.close(reason)
            self.sessions.pop(session.session_id, None)
            if not ws.closed:
                await ws.close()
        return ws


def make_app(cfg: Config, models: Models | None = None, gateway=None) -> web.Application:
    a = App(cfg, models, gateway)
    app = web.Application(client_max_size=64 * 1024)
    app.on_startup.append(a.on_startup)
    app.on_cleanup.append(a.on_cleanup)
    app.router.add_get("/healthz", a.healthz)
    app.router.add_get("/ws", a.ws)
    if cfg.server.serve_test_client:
        app.router.add_get("/", a.index)
    if a.hub is not None:
        a.hub.add_routes(app)
    app[VOICE_APP] = a
    return app


VOICE_APP = web.AppKey("voice_app", App)
