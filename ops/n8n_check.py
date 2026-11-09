"""End-to-end check of the n8n workflows + PostgreSQL, run in Colab after n8n starts (CPU is enough).

  python -m ops.n8n_check

Talks to n8n exactly like the voice engine does (voice_engine.business.N8nGateway, HMAC-signed) using a
temporary copy of the demo business, which is deleted afterwards. Prints PASS/FAIL per step.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp

from voice_engine.business import N8nGateway, sign
from voice_engine.config import BusinessCfg

from .migrate import colab_conn

BASE = "http://127.0.0.1:5678/webhook"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def owner_sql(sql: str) -> str:
    psql, c = colab_conn()
    r = subprocess.run([psql, "-h", c["host"], "-p", str(c["port"]), "-U", c["user"], "-d", c["db"], "-v",
                        "ON_ERROR_STOP=1", "-qAtX"], input=sql, capture_output=True, text=True,
                       env=dict(os.environ, PGPASSWORD=c["password"]))
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-800:])
    return r.stdout.strip()


SETUP = """
WITH src AS (SELECT id FROM va.businesses WHERE slug = 'karachi-pizza'),
b AS (INSERT INTO va.businesses (slug, name)
      VALUES ('selftest-' || to_char(clock_timestamp(), 'YYYYMMDDHH24MISSMS'), 'Selftest Pizza') RETURNING id),
c AS (INSERT INTO va.agent_configs (business_id, language, instructions, greeting, greeting_outbound, enabled_tools)
      SELECT b.id, a.language, a.instructions, a.greeting, a.greeting_outbound, a.enabled_tools
      FROM b, va.agent_configs a JOIN src ON a.business_id = src.id RETURNING 1),
p AS (INSERT INTO va.products (business_id, sku, name_en, name_ur, category, price_pkr, sort_order)
      SELECT b.id, p.sku, p.name_en, p.name_ur, p.category, p.price_pkr, p.sort_order
      FROM b, va.products p JOIN src ON p.business_id = src.id RETURNING 1)
SELECT id FROM b, (SELECT count(*) FROM c) cc, (SELECT count(*) FROM p) pp;
"""


async def run() -> int:
    biz = owner_sql(SETUP).splitlines()[-1]
    session = str(uuid.uuid4())
    gw = N8nGateway(BusinessCfg(n8n_base_url=BASE, timeout_s=30, retries=0), os.environ["N8N_WEBHOOK_HMAC_SECRET"])
    try:
        ctx = await gw.start_session({"session_id": session, "channel": "browser_test", "line": "default",
                                      "caller": {"number": "0300-9999999"}, "context": {"business_id": biz}})
        tools = sorted(t["function"]["name"] for t in ctx.tools)
        check("session-start returns the business context", ctx.business_id == biz and len(tools) == 8,
              f"{ctx.business_name}, tools={len(tools)}")
        check("caller identified as a customer", ctx.customer.get("phone") == "+923009999999", str(ctx.customer.get("phone")))

        menu = await gw.execute_tool(ctx, "t1", "get_menu", {})
        names = [i["name"] for i in menu.get("result", {}).get("items", [])]
        check("get_menu returns the real menu", menu.get("ok") and len(names) == 10, f"{len(names)} items")
        check("menu has no burgers", not any("burger" in n.lower() for n in names))

        other = await gw.execute_tool(ctx, "t2", "lookup_order", {"order_number": "4821"})
        check("tenant isolation: another business's order 4821 is invisible",
              other.get("ok") and other["result"].get("found") is False)

        order_args = {"items": [{"sku": "PZ-TIKKA-L", "quantity": 2}, {"sku": "DR-COLA-500", "quantity": 3}],
                      "delivery_address": "House 9, MDA Chowk, Karachi", "customer_name": "Selftest"}
        created = await gw.execute_tool(ctx, "t3", "create_order", order_args)
        res = created.get("result", {})
        check("create_order saves the order with the correct total",
              created.get("ok") and res.get("total_pkr") == 4350, json.dumps(created)[:160])
        again = await gw.execute_tool(ctx, "t3", "create_order", order_args)       # same call id = network retry
        n_orders = owner_sql(f"SELECT count(*) FROM va.orders WHERE business_id = '{biz}';")
        check("retried tool call does not create a second order", again == created and n_orders == "1",
              f"orders={n_orders}")
        found = await gw.execute_tool(ctx, "t4", "lookup_order", {"order_number": res.get("order_number", "0000")})
        check("lookup_order finds the new order", found.get("result", {}).get("found") is True,
              found.get("result", {}).get("status", ""))
        burger = await gw.execute_tool(ctx, "t5", "create_order",
                                       {"items": [{"sku": "BURGER-1", "quantity": 1}], "delivery_address": "Clifton, Karachi"})
        check("ordering an item not on the menu is refused", burger.get("ok") is False and "not on the menu" in burger.get("error", ""))

        day = (datetime.now(timezone(timedelta(hours=5))) + timedelta(days=1)).strftime("%Y-%m-%d")
        avail = await gw.execute_tool(ctx, "t6", "check_availability", {"date": day, "time": "20:00", "party_size": 4})
        check("check_availability", avail.get("result", {}).get("available") is True, json.dumps(avail.get("result"))[:120])
        booked = await gw.execute_tool(ctx, "t7", "create_booking", {"date": day, "time": "20:00", "party_size": 4, "name": "Selftest"})
        check("create_booking returns a booking reference",
              str(booked.get("result", {}).get("booking_ref", "")).startswith("BK-"), json.dumps(booked.get("result"))[:120])
        closed = await gw.execute_tool(ctx, "t8", "create_booking", {"date": day, "time": "09:00", "party_size": 2, "name": "x"})
        check("booking outside opening hours is refused", closed.get("result", {}).get("available") is False)

        pref = await gw.execute_tool(ctx, "t9", "remember_customer_preference", {"note": "extra spicy"})
        check("customer preference saved", pref.get("ok") is True)
        hand = await gw.execute_tool(ctx, "t10", "request_human_handoff", {"reason": "selftest"})
        check("human handoff queued", hand.get("result", {}).get("queued") is True)

        async with aiohttp.ClientSession() as s:                                  # forged request
            body = json.dumps({"payload": json.dumps({"tool": "get_menu", "business_id": biz})})
            ts = str(int(time.time()))
            async with s.post(f"{BASE}/voice/tool", data=body, headers={
                    "Content-Type": "application/json", "X-VA-Timestamp": ts,
                    "X-VA-Signature": sign("wrong-secret", ts, b"x")}) as r:
                check("request with a forged signature is rejected", r.status == 401, f"HTTP {r.status}")

        now = time.time()
        await gw.end_session(ctx, {"session_id": session, "business_id": biz, "started_at": now - 60, "ended_at": now,
                                   "end_reason": "selftest", "turns": 2, "transcript": [
                                       {"role": "user", "text": "I want two large tikka pizzas", "language": "en", "t": now - 50},
                                       {"role": "assistant", "text": "Your order is placed.", "t": now - 45}]})
        row = owner_sql(f"SELECT status || ',' || (SELECT count(*) FROM va.transcripts WHERE call_id = '{session}') "
                        f"FROM va.calls WHERE id = '{session}';")
        check("session-end closes the call and stores the transcript", row == "ended,2", row)
        await gw.call_event({"call_id": "selftest-" + session[:8], "status": "ended", "direction": "outbound",
                             "remote_number": "+923009999999", "context": {"business_id": biz}})
        ev = owner_sql(f"SELECT count(*) FROM va.call_events WHERE business_id = '{biz}';")
        check("call-event recorded", ev == "1", ev)

        llm_up = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:8081/health"],
                                capture_output=True, text=True).stdout == "200"
        if llm_up:
            summ = "0"
            for _ in range(30):
                summ = owner_sql(f"SELECT count(*) FROM va.conversation_summaries WHERE call_id = '{session}';")
                if summ == "1":
                    break
                await asyncio.sleep(2)
            check("post-call summary written by the LLM", summ == "1")
        else:
            print("SKIP  post-call summary (LLM server not running on this runtime)")
        errs = owner_sql("SELECT count(*) FROM va.workflow_errors WHERE created_at > now() - interval '10 minutes';")
        check("no workflow errors logged", errs == "0", f"{errs} recent errors (see va.workflow_errors)")
    except Exception as e:
        check("unexpected exception", False, f"{type(e).__name__}: {e}")
    finally:
        await gw.close()
        owner_sql(f"DELETE FROM va.idempotency_keys WHERE key LIKE '%{session}%'; DELETE FROM va.businesses WHERE id = '{biz}';")
        print("test business removed")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
