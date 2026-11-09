"""HFP gateway tests: policy, PipeWire node selection, and full call flows against the REAL
voice engine (telephony enabled) with test doubles for the phone (oFono) and audio (PipeWire).

Proves: call control/state machine, policy enforcement, privacy of personal calls, the
engine<->gateway protocol and the n8n call API. Does NOT prove: Bluetooth, oFono or SCO
audio on the Redmi 9C (that is `python -m gateway dial-test/answer-test` on real hardware).
"""
import asyncio
import json

import numpy as np
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from gateway.audio import pick_nodes
from gateway.bridge import Bridge
from gateway.config import GatewayCfg
from gateway.ofono import CallEvent
from gateway.policy import Policy, PolicyCfg, normalize_pk
from voice_engine.audio import float_to_pcm16
from voice_engine.business import StaticGateway
from voice_engine.config import BusinessCfg, Config
from voice_engine.llm import LlmClient
from voice_engine.server import VOICE_APP, make_app
from voice_engine.session import Models
from voice_engine.tts import TtsRouter

from conftest import ROOT, FakeStt, FakeTts, FakeVad, speech

ALLOWED = "03001234567"


# ------------------------------------------------------------------ policy
def test_normalize_pk():
    assert normalize_pk("0300-1234567") == "+923001234567"
    assert normalize_pk("923001234567") == "+923001234567"
    assert normalize_pk("+92 21 3456 7890") == "+922134567890"
    assert normalize_pk("00923001234567") == "+923001234567"
    assert normalize_pk("0900123456") is None          # premium rate
    assert normalize_pk("+14155550100") is None        # not Pakistan
    assert normalize_pk("1234") is None


def test_policy_limits(tmp_path):
    p = Policy(PolicyCfg(outbound_mode="allowlist", outbound_allowlist=[ALLOWED], max_outbound_per_hour=2,
                         max_outbound_minutes_per_day=1), tmp_path / "s.json")
    assert p.check_outbound("03009999999") == (None, "number_not_in_allowlist")
    assert p.check_outbound("+923001234567")[0] == "+923001234567"
    p.record_dial(now=1000)
    p.record_dial(now=1001)
    assert p.check_outbound(ALLOWED, now=1002) == (None, "hourly_dial_limit")
    assert p.check_outbound(ALLOWED, now=1000 + 3700)[0]          # hour window rolled
    p.record_outbound_seconds(61, now=1000 + 3700)
    assert p.check_outbound(ALLOWED, now=1000 + 3701) == (None, "daily_minutes_limit")
    assert Policy(p.cfg, tmp_path / "s.json").state["day_seconds"] == 61   # persisted across restarts


def test_inbound_policy(tmp_path):
    p = Policy(PolicyCfg(inbound_mode="allowlist", inbound_allowlist=[ALLOWED]), tmp_path / "s.json")
    assert p.should_answer("+923001234567") == (True, "")
    assert p.should_answer("03211111111") == (False, "caller_not_in_allowlist")
    assert Policy(PolicyCfg(inbound_mode="off"), tmp_path / "x.json").should_answer(ALLOWED)[0] is False


def test_pick_nodes():
    sources = ("45\talsa_input.platform-mic\tPipeWire\ts16le 1ch 48000Hz\tSUSPENDED\n"
               "61\tbluez_input.AA_BB_CC_DD_EE_FF.0\tPipeWire\ts16le 1ch 16000Hz\tRUNNING\n"
               "62\tbluez_output.AA_BB_CC_DD_EE_FF.1.monitor\tPipeWire\ts16le 1ch 16000Hz\tIDLE\n")
    sinks = "62\tbluez_output.AA_BB_CC_DD_EE_FF.1\tPipeWire\ts16le 1ch 16000Hz\tRUNNING\n"
    assert pick_nodes(sources, sinks, "AA:BB:CC:DD:EE:FF") == ("bluez_input.AA_BB_CC_DD_EE_FF.0",
                                                             "bluez_output.AA_BB_CC_DD_EE_FF.1")
    assert pick_nodes(sources, sinks, "11:22:33:44:55:66") == (None, None)


# ------------------------------------------------------------------ test doubles
class FakeModem:
    def __init__(self):
        self.events = asyncio.Queue()
        self.dialed, self.answered, self.hung = [], [], []
        self._n = 0

    async def start(self):
        pass

    def ready(self):
        return True

    async def dial(self, number):
        self._n += 1
        path = f"/hfp/dev_AA/voicecall{self._n:02d}"
        self.dialed.append(number)
        self.events.put_nowait(CallEvent("added", path, "dialing", number))
        return path

    async def answer(self, path):
        self.answered.append(path)
        self.events.put_nowait(CallEvent("state", path, "active"))

    async def hangup(self, path):
        self.hung.append(path)
        self.events.put_nowait(CallEvent("state", path, "disconnected"))
        self.events.put_nowait(CallEvent("removed", path))


class FakeAudio:
    """Downlink = frames we push in; uplink = what the AI sends (collected)."""

    instances = []

    def __init__(self):
        self.uplink = bytearray()
        self.cleared = 0
        self.down = asyncio.Queue()
        self.closed = False
        self.nodes = ("bluez_input.fake", "bluez_output.fake")
        FakeAudio.instances.append(self)

    async def open(self):
        pass

    async def frames(self):
        while True:
            f = await self.down.get()
            if f is None:
                return
            yield f

    def say(self, audio):
        for i in range(0, len(audio), 320):
            self.down.put_nowait(float_to_pcm16(audio[i:i + 320]))

    def play(self, pcm):
        self.uplink.extend(pcm)

    def clear(self):
        self.cleared += 1

    async def close(self):
        self.closed = True
        self.down.put_nowait(None)


@pytest.fixture
async def stack(tmp_path, fake_llm, monkeypatch):
    for k in ("VOICE_ENGINE_WS_TOKEN", "VOICE_ENGINE_GATEWAY_TOKEN", "VOICE_ENGINE_CALLS_TOKEN"):
        monkeypatch.setenv(k, k.lower() + "-x" * 12)
    cfg = Config(project_root=str(ROOT))
    cfg.llm.base_url = fake_llm.base_url
    cfg.log.dir = str(tmp_path / "logs")
    cfg.vad.end_silence_ms = 320
    cfg.telephony.enabled = True
    models = Models(stt=FakeStt(), tts=TtsRouter({"en": FakeTts(), "ur": FakeTts()}), llm=LlmClient(cfg.llm),
                    vad_factory=FakeVad)
    biz = StaticGateway(BusinessCfg(), ROOT / "config/dev_business.yaml", tmp_path / "sessions")
    server = TestServer(make_app(cfg, models, biz))
    await server.start_server()
    gcfg = GatewayCfg(engine_url=str(server.make_url("")), phone_bt_address="AA:BB:CC:DD:EE:FF",
                      policy=PolicyCfg(outbound_allowlist=[ALLOWED], inbound_allowlist=[ALLOWED],
                                       answer_after_s=0.05, dial_timeout_s=2))
    modem = FakeModem()
    FakeAudio.instances = []
    http = ClientSession()
    bridge = Bridge(gcfg, modem, FakeAudio, Policy(gcfg.policy, tmp_path / "policy.json"), http)
    tasks = [asyncio.create_task(bridge.control_loop()), asyncio.create_task(bridge.event_loop())]
    hub = server.app[VOICE_APP].hub
    for _ in range(100):
        if hub.connected():
            break
        await asyncio.sleep(0.02)
    calls_auth = {"Authorization": "Bearer " + "voice_engine_calls_token" + "-x" * 12}
    yield dict(server=server, http=http, hub=hub, modem=modem, bridge=bridge, auth=calls_auth,
               tmp=tmp_path, llm=fake_llm)
    bridge.stop()
    for t in tasks:
        t.cancel()
    await http.close()
    await models.llm.close()
    await server.close()


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


# ------------------------------------------------------------------ flows
async def test_outbound_call_full_flow(stack):
    s = stack
    s["llm"].scripts = [[{"text": "Your order is confirmed. "}]]
    r = await s["http"].post(s["server"].make_url("/calls"), headers=s["auth"],
                             json={"number": ALLOWED, "context": {"purpose": "order_confirmation", "order_id": "4821"},
                                   "idempotency_key": "job-1"})
    assert r.status == 202
    call_id = (await r.json())["call_id"]
    dup = await s["http"].post(s["server"].make_url("/calls"), headers=s["auth"],
                               json={"number": ALLOWED, "idempotency_key": "job-1"})
    assert (await dup.json())["call_id"] == call_id                     # duplicate request -> same call
    await until(lambda: s["modem"].dialed)
    assert s["modem"].dialed == ["+923001234567"]
    path = next(iter(s["bridge"].calls))
    s["modem"].events.put_nowait(CallEvent("state", path, "alerting"))
    s["modem"].events.put_nowait(CallEvent("state", path, "active"))
    await until(lambda: FakeAudio.instances and len(FakeAudio.instances[0].uplink) > 0)   # greeting reached uplink
    audio = FakeAudio.instances[0]
    greeting_bytes = len(audio.uplink)
    assert s["hub"].context_for(call_id) == {"purpose": "order_confirmation", "order_id": "4821"}
    await asyncio.sleep(0.3)
    audio.say(np.concatenate([speech(0.8), np.zeros(16000, np.float32)]))   # callee speaks
    await until(lambda: len(audio.uplink) > greeting_bytes + 1000, timeout=8)   # AI reply audio
    s["modem"].events.put_nowait(CallEvent("state", path, "disconnected"))
    s["modem"].events.put_nowait(CallEvent("removed", path))
    await until(lambda: s["hub"].calls[call_id].status == "ended")
    statuses = [json.loads(line)["status"] for line in
                (s["tmp"] / "sessions" / "call_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert statuses[0] == "requested" and "dialing" in statuses and "active" in statuses and statuses[-1] == "ended"
    await until(lambda: audio.closed)
    report = json.loads(next((s["tmp"] / "sessions").glob("session_*.json")).read_text(encoding="utf-8"))
    roles = [t["role"] for t in report["transcript"]]
    assert roles[0] == "assistant" and "user" in roles             # agent greeted first, then the callee


async def test_outbound_rejected_by_policy(stack):
    s = stack
    r = await s["http"].post(s["server"].make_url("/calls"), headers=s["auth"], json={"number": "03119999999"})
    call_id = (await r.json())["call_id"]
    await until(lambda: s["hub"].calls[call_id].status == "rejected")
    assert s["hub"].calls[call_id].reason == "number_not_in_allowlist" and s["modem"].dialed == []


async def test_calls_api_requires_token_and_valid_number(stack):
    s = stack
    r = await s["http"].post(s["server"].make_url("/calls"), json={"number": ALLOWED})
    assert r.status == 401
    r = await s["http"].post(s["server"].make_url("/calls"), headers=s["auth"], json={"number": "abc"})
    assert r.status == 400


async def test_line_busy(stack):
    s = stack
    await s["http"].post(s["server"].make_url("/calls"), headers=s["auth"], json={"number": ALLOWED})
    r = await s["http"].post(s["server"].make_url("/calls"), headers=s["auth"], json={"number": ALLOWED})
    assert r.status == 409


async def test_no_answer_times_out(stack):
    s = stack
    r = await s["http"].post(s["server"].make_url("/calls"), headers=s["auth"], json={"number": ALLOWED})
    call_id = (await r.json())["call_id"]
    await until(lambda: s["hub"].calls[call_id].status == "failed", timeout=5)
    assert s["hub"].calls[call_id].reason == "no_answer" and s["modem"].hung


async def test_inbound_allowlisted_is_answered_by_agent(stack):
    s = stack
    s["modem"].events.put_nowait(CallEvent("added", "/hfp/dev_AA/voicecall09", "incoming", "+923001234567"))
    await until(lambda: s["modem"].answered == ["/hfp/dev_AA/voicecall09"])
    await until(lambda: FakeAudio.instances and len(FakeAudio.instances[0].uplink) > 0)   # inbound greeting
    inbound = [c for c in s["hub"].calls.values() if c.direction == "inbound"]
    assert inbound and inbound[0].status == "active"


async def test_personal_call_is_never_answered_or_reported(stack):
    s = stack
    s["modem"].events.put_nowait(CallEvent("added", "/hfp/dev_AA/voicecall10", "incoming", "+923219876543"))
    await asyncio.sleep(0.3)
    s["modem"].events.put_nowait(CallEvent("state", "/hfp/dev_AA/voicecall10", "active"))   # you answer it
    await asyncio.sleep(0.3)
    assert s["modem"].answered == [] and FakeAudio.instances == []
    assert s["hub"].calls == {}                                   # number never left the gateway


async def test_engine_hangup_ends_phone_call(stack):
    s = stack
    r = await s["http"].post(s["server"].make_url("/calls"), headers=s["auth"], json={"number": ALLOWED})
    call_id = (await r.json())["call_id"]
    await until(lambda: s["modem"].dialed)
    path = next(iter(s["bridge"].calls))
    s["modem"].events.put_nowait(CallEvent("state", path, "active"))
    await until(lambda: FakeAudio.instances)
    h = await s["http"].post(s["server"].make_url(f"/calls/{call_id}/hangup"), headers=s["auth"])
    assert h.status == 202
    await until(lambda: s["modem"].hung == [path])
    await until(lambda: s["hub"].calls[call_id].status == "ended")
