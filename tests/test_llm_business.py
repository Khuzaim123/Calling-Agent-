import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from voice_engine.business import N8nGateway, SessionContext, StaticGateway, sign, verify
from voice_engine.config import BusinessCfg, LlmCfg
from voice_engine.llm import LlmClient, ToolCall
from voice_engine.prompts import build_system_prompt, trim_history

from conftest import ROOT


async def collect(client, msgs, tools=None):
    return [e async for e in client.stream(msgs, tools)]


async def test_llm_stream_text_and_timings(fake_llm):
    fake_llm.scripts = [[{"text": "Hello "}, {"text": "<think>x</think>there."}]]
    c = LlmClient(LlmCfg(base_url=fake_llm.base_url), api_key="k")
    ev = await collect(c, [{"role": "user", "content": "hi"}])
    await c.close()
    assert "".join(e.text for e in ev if e.kind == "text") == "Hello there."
    assert ev[-1].kind == "done" and ev[-1].timings["predicted_per_second"] == 42.0
    assert ev[0].ttft_s is not None
    req = fake_llm.requests[0]
    assert req["stream"] is True and req["chat_template_kwargs"] == {"enable_thinking": False}


async def test_llm_stream_tool_call_fragments_merge(fake_llm):
    fake_llm.scripts = [[{"tool": ("lookup_order", json.dumps({"order_id": "4821"}))}]]
    c = LlmClient(LlmCfg(base_url=fake_llm.base_url))
    ev = await collect(c, [{"role": "user", "content": "order 4821"}], tools=[{"type": "function"}])
    await c.close()
    calls = next(e for e in ev if e.kind == "tool_calls").tool_calls
    assert calls[0].name == "lookup_order" and calls[0].arguments() == {"order_id": "4821"}
    assert fake_llm.requests[0]["tool_choice"] == "auto"


def test_tool_call_bad_arguments():
    with pytest.raises(ValueError):
        ToolCall("1", "x", "{not json").arguments()
    with pytest.raises(ValueError):
        ToolCall("1", "x", "[1,2]").arguments()


def test_hmac_sign_verify():
    body = b'{"a":1}'
    ts = str(int(time.time()))
    sig = sign("s3cret", ts, body)
    assert verify("s3cret", ts, body, sig)
    assert not verify("wrong", ts, body, sig)
    assert not verify("s3cret", ts, body + b" ", sig)
    assert not verify("s3cret", str(int(time.time()) - 3600), body, sign("s3cret", str(int(time.time()) - 3600), body))
    assert not verify("s3cret", "not-a-number", body, sig)


async def test_static_gateway_enforces_enabled_tools(tmp_path):
    gw = StaticGateway(BusinessCfg(), ROOT / "config/dev_business.yaml", tmp_path)
    ctx = await gw.start_session({"session_id": "s1"})
    assert ctx.business_id == "dev-0001" and len(ctx.tools) == 5
    ok = await gw.execute_tool(ctx, "c1", "lookup_order", {"order_id": "4821"})
    assert ok["ok"] and ok["_fixture"] is True
    denied = await gw.execute_tool(ctx, "c2", "delete_all_orders", {})
    assert not denied["ok"]
    await gw.end_session(ctx, {"session_id": "s1", "transcript": []})
    assert (tmp_path / "session_s1.json").exists()


async def test_n8n_gateway_signs_and_retries_with_same_idempotency_key():
    seen = []

    async def tool(req: web.Request):
        payload = (await req.json())["payload"]            # signed string envelope
        assert json.loads(payload)["tool"] == "lookup_order"
        seen.append((req.headers["X-VA-Idempotency-Key"], verify("hm", req.headers["X-VA-Timestamp"],
                                                                payload.encode(), req.headers["X-VA-Signature"])))
        if len(seen) == 1:
            return web.Response(status=502)
        return web.json_response({"ok": True, "result": {"status": "out_for_delivery"}})

    app = web.Application()
    app.router.add_post("/webhook/voice/tool", tool)
    srv = TestServer(app)
    await srv.start_server()
    gw = N8nGateway(BusinessCfg(n8n_base_url=str(srv.make_url("/webhook")), retries=2), "hm")
    ctx = SessionContext("s9", "biz-1", "B", "x",
                         tools=[{"type": "function", "function": {"name": "lookup_order"}}])
    res = await gw.execute_tool(ctx, "call_7", "lookup_order", {"order_id": "1"})
    await gw.close()
    await srv.close()
    assert res["ok"] is True
    assert [k for k, _ in seen] == ["tool:s9:call_7", "tool:s9:call_7"]
    assert all(ok for _, ok in seen)


async def test_n8n_gateway_unavailable_returns_error_not_exception():
    gw = N8nGateway(BusinessCfg(n8n_base_url="http://127.0.0.1:9/webhook", retries=1, timeout_s=1), "hm")
    ctx = SessionContext("s", "b", "B", "x", tools=[{"type": "function", "function": {"name": "t"}}])
    res = await gw.execute_tool(ctx, "c", "t", {})
    await gw.close()
    assert res["ok"] is False and res["error"] == "business system unavailable"


def test_system_prompt_contains_business_and_rules():
    ctx = SessionContext("s", "b", "Shop", "Sell shoes.", knowledge=["Open 9-5"], customer={"name": "Ali"})
    p = build_system_prompt(ctx)
    assert "Shop" in p and "Sell shoes." in p and "Open 9-5" in p and "Ali" in p and "Urdu" in p


def test_trim_history_keeps_tool_groups_intact():
    msgs = [{"role": "system", "content": "S"}]
    for i in range(20):
        msgs.append({"role": "user", "content": f"u{i} " + "x" * 50})
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    out = trim_history(msgs, 600)
    assert out[0]["role"] == "system" and len(out) < len(msgs)
    assert out[1]["role"] not in ("tool",) and not out[1].get("tool_calls")
    for i, m in enumerate(out):
        if m["role"] == "tool":
            assert out[i - 1].get("tool_calls") or out[i - 1]["role"] == "tool"
