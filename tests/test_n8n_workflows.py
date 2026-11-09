"""n8n workflow checks that run without n8n or PostgreSQL:
structure, SQL syntax (PostgreSQL's own parser via pglast), JavaScript syntax, and the business-rule
Code nodes executed in Node.js with test inputs. Running the workflows for real happens on Colab.
"""
import hashlib
import hmac
import json
import shutil
import subprocess
import time

import pytest

from conftest import ROOT

WF_DIR = ROOT / "n8n" / "workflows"
NODE = shutil.which("node")
# node type -> versions available in n8n-nodes-base 2.45.2 (checked 2026-11-09)
AVAILABLE = {
    "n8n-nodes-base.webhook": {1, 1.1, 2, 2.1, 2.2}, "n8n-nodes-base.respondToWebhook": {1, 1.1, 1.2, 1.3, 1.4, 1.5},
    "n8n-nodes-base.code": {1, 2}, "n8n-nodes-base.switch": {3, 3.1, 3.2, 3.3, 3.4, 3.5},
    "n8n-nodes-base.postgres": {2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8}, "n8n-nodes-base.crypto": {1, 2},
    "n8n-nodes-base.httpRequest": {4, 4.1, 4.2, 4.3, 4.4, 4.5}, "n8n-nodes-base.scheduleTrigger": {1, 1.1, 1.2, 1.3, 1.4},
    "n8n-nodes-base.errorTrigger": {1},
}
CRED_IDS = {"vaPgApp000000001", "vaHmacSecret0001", "vaLlmApiKey00001", "vaCallsToken0001"}
TRIGGERS = {"n8n-nodes-base.webhook", "n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.errorTrigger"}


def workflows():
    subprocess.run(["python", str(ROOT / "tools" / "build_workflows.py")], check=True, capture_output=True)
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(WF_DIR.glob("*.json"))}


WFS = workflows()


def node(wf, name):
    return next(n for n in WFS[wf]["nodes"] if n["name"] == name)


# ------------------------------------------------------------------ structure
@pytest.mark.parametrize("name", sorted(WFS))
def test_structure(name):
    wf = WFS[name]
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names)), "duplicate node names"
    assert len(wf["id"]) == 16
    for n in wf["nodes"]:
        assert n["typeVersion"] in AVAILABLE[n["type"]], f"{n['name']}: {n['type']} v{n['typeVersion']}"
        for c in (n.get("credentials") or {}).values():
            assert c["id"] in CRED_IDS
    for src, conn in wf["connections"].items():
        assert src in names
        src_node = node(name, src)
        for i, outs in enumerate(conn["main"]):
            if src_node["type"] == "n8n-nodes-base.switch":
                assert i < src_node["parameters"]["numberOutputs"], f"{src} output {i} out of range"
            for target in outs:
                assert target["node"] in names
    # every node reachable from the trigger
    seen, todo = set(), [n["name"] for n in wf["nodes"] if n["type"] in TRIGGERS]
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        todo += [t["node"] for outs in wf["connections"].get(cur, {}).get("main", []) for t in outs]
    assert seen == set(names), f"unreachable: {set(names) - seen}"
    if name != "va-error-handler":
        assert wf["settings"]["errorWorkflow"] == "vaWfErrors000001"


def test_webhook_paths_match_voice_engine():
    paths = {n["parameters"]["path"] for wf in WFS.values() for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"}
    assert paths == {"voice/session-start", "voice/tool", "voice/session-end", "voice/call-event"}   # see N8nGateway


def test_every_switch_output_is_wired():
    for wf_name, wf in WFS.items():
        for n in wf["nodes"]:
            if n["type"] == "n8n-nodes-base.switch":
                outs = wf["connections"][n["name"]]["main"]
                assert len(outs) == n["parameters"]["numberOutputs"] and all(outs), f"{wf_name}/{n['name']}"


# ------------------------------------------------------------------ SQL syntax
def all_sql():
    for f in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        yield f.name, f.read_text(encoding="utf-8")
    for wf_name, wf in WFS.items():
        for n in wf["nodes"]:
            if n["type"] == "n8n-nodes-base.postgres":
                yield f"{wf_name}/{n['name']}", n["parameters"]["query"]


@pytest.mark.parametrize("label,sql", list(all_sql()), ids=[x[0] for x in all_sql()])
def test_sql_parses(label, sql):
    pglast = pytest.importorskip("pglast")
    pglast.parse_sql(sql)


def test_queries_scope_by_business():
    """Every query touching tenant tables must filter by business_id (or claim jobs/notifications globally)."""
    global_ok = {"Claim due job", "Pending notifications", "Log error", "Mark delivered", "Store response"}
    for wf_name, wf in WFS.items():
        for n in wf["nodes"]:
            if n["type"] == "n8n-nodes-base.postgres" and n["name"] not in global_ok:
                assert "business_id" in n["parameters"]["query"], f"{wf_name}/{n['name']}"


# ------------------------------------------------------------------ JavaScript
HARNESS = r"""
const fs = require('fs');
const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const mk = (arr) => ({ first: () => ({ json: arr[0] }), all: () => arr.map(j => ({ json: j })) });
const $input = mk(spec.input);
const $ = (name) => mk(spec.nodes[name] || []);
const fn = new Function('$input', '$', `return (async () => {\n${spec.code}\n})();`);
fn($input, $).then(out => process.stdout.write(JSON.stringify(out)))
  .catch(e => process.stdout.write(JSON.stringify({ __error: String(e && e.stack || e) })));
"""


def run_code(wf, name, inp, nodes=None):
    if NODE is None:
        pytest.skip("node.js not available")
    spec = {"code": node(wf, name)["parameters"]["jsCode"], "input": inp, "nodes": nodes or {}}
    r = subprocess.run([NODE, "-e", HARNESS], input=json.dumps(spec), capture_output=True, text=True,
                       encoding="utf-8", timeout=30)
    out = json.loads(r.stdout)
    assert not (isinstance(out, dict) and "__error" in out), out
    return [o["json"] for o in out]


def code_nodes():
    return [(wf_name, n["name"]) for wf_name, wf in WFS.items() for n in wf["nodes"] if n["type"] == "n8n-nodes-base.code"]


@pytest.mark.parametrize("wf,name", code_nodes(), ids=[f"{w}/{n}" for w, n in code_nodes()])
def test_js_syntax(wf, name):
    if NODE is None:
        pytest.skip("node.js not available")
    js = "(async function(){\n" + node(wf, name)["parameters"]["jsCode"] + "\n})"
    r = subprocess.run([NODE, "--check", "-"], input=js, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr


def signed(payload: dict, secret="s3cret", ts=None, tamper=False):
    inner = json.dumps(payload, separators=(",", ":"))
    ts = str(int(time.time()) if ts is None else ts)
    sig = hmac.new(secret.encode(), (ts + "." + inner).encode(), hashlib.sha256).hexdigest()
    expected = hmac.new(secret.encode(), (ts + "." + inner).encode(), hashlib.sha256).hexdigest()  # what the HMAC node computes
    return {"headers": {"x-va-timestamp": ts, "x-va-signature": ("0" * 64 if tamper else sig),
                        "x-va-idempotency-key": "tool:s1:c1"}, "body": {"payload": inner}, "expected_sig": expected}


def test_verify_accepts_valid_and_rejects_forged_or_stale():
    good = run_code("va-tool", "Verify request", [signed({"tool": "get_menu"})])[0]
    assert good["ok"] and good["req"]["tool"] == "get_menu" and good["idem"] == "tool:s1:c1"
    assert run_code("va-tool", "Verify request", [signed({"a": 1}, tamper=True)])[0]["reason"] == "bad signature"
    assert run_code("va-tool", "Verify request", [signed({"a": 1}, ts=1000)])[0]["reason"].startswith("stale")


BIZ = "6b2f0a1c-0c3e-4a8b-9d55-1d2e3f4a5b6c"


def prep(tool, args):
    return run_code("va-tool", "Prepare tool call",
                    [{"req": {"tool": tool, "arguments": args, "business_id": BIZ, "session_id": "sess-12345"}, "idem": "k"}])[0]


def test_tool_input_rules():
    ok = prep("create_order", {"items": [{"sku": "pz-tikka-l", "quantity": 2}], "delivery_address": "House 9, MDA Chowk",
                               "customer_phone": "0300-1234567"})
    assert ok["route"] == "db" and ok["args"]["items"] == [{"sku": "PZ-TIKKA-L", "quantity": 2}]
    assert ok["args"]["customer_phone"] == "+923001234567"
    assert "invalid sku" in prep("create_order", {"items": [{"sku": "burger!", "quantity": 1}],
                                                  "delivery_address": "x street"})["response"]["error"]
    assert "address" in prep("create_order", {"items": [{"sku": "PZ-VEG-M", "quantity": 1}]})["response"]["error"]
    assert "YYYY-MM-DD" in prep("check_availability", {"date": "tomorrow", "time": "20:00", "party_size": 4})["response"]["error"]
    assert prep("lookup_order", {"order_number": "48-21"})["args"]["order_number"] == "4821"
    assert prep("drop_tables", {})["response"]["error"].startswith("unknown tool")


def test_order_rules_reject_unknown_items_and_compute_total():
    route = {"business_id": BIZ, "session_id": "s", "customer_id": None, "tool": "create_order",
             "args": {"delivery_address": "House 9", "customer_name": "Ali", "customer_phone": "+923001234567"}}
    lines = [{"sku": "PZ-TIKKA-L", "quantity": 2, "product_id": "p1", "name": "Tikka", "price_pkr": 1950, "available": True},
             {"sku": "DR-COLA-500", "quantity": 3, "product_id": "p2", "name": "Cola", "price_pkr": 150, "available": True}]
    out = run_code("va-tool", "Check order", [{"lines": lines, "eta": 40}], {"Route tool": [route]})[0]
    assert out["ok"] and out["total"] == 4350 and out["phone"] == "+923001234567"
    bad = run_code("va-tool", "Check order", [{"lines": [{"sku": "BURGER", "quantity": 1, "product_id": None}], "eta": 40}],
                   {"Route tool": [route]})[0]
    assert not bad["ok"] and "not on the menu: BURGER" in bad["response"]["error"]
    anon = dict(route, args={**route["args"], "customer_phone": None})
    assert "phone number" in run_code("va-tool", "Check order", [{"lines": lines, "eta": 40}], {"Route tool": [anon]})[0]["response"]["error"]


def test_booking_rules():
    def decide(tool, time_, party, taken, past=False):
        route = {"business_id": BIZ, "session_id": "s", "customer_id": None, "tool": tool,
                 "args": {"date": "2030-01-05", "time": time_, "party_size": party, "name": "Ali", "customer_phone": None}}
        row = {"starts_at": "2020-01-05T15:00:00Z" if past else "2030-01-05T15:00:00Z", "now": "2026-11-09T10:00:00Z",
               "cfg": {"capacity": 20, "slot_minutes": 90, "opening_time": "12:00", "closing_time": "23:30"},
               "seats_taken": str(taken)}
        return run_code("va-tool", "Decide booking", [row], {"Route tool": [route]})[0]
    assert decide("check_availability", "20:00", 4, 10)["response"]["result"]["available"] is True
    assert "fully booked" in decide("check_availability", "20:00", 4, 18)["response"]["result"]["reason"]
    assert "open 12:00" in decide("create_booking", "09:00", 2, 0)["response"]["result"]["reason"]
    assert "past" in decide("create_booking", "20:00", 2, 0, past=True)["response"]["result"]["reason"]
    assert decide("create_booking", "20:00", 2, 0)["book"] is True


def test_outbound_calling_rules():
    def rule(**job):
        base = {"job_id": "j1", "phone": "+923001234567", "consent": True, "opted_out": False, "purpose": "manual_test"}
        out = run_code("va-outbound-scheduler", "Calling rules", [{"jobs": [{**base, **job}]}])
        return out[0] if out else None
    assert rule()["action"] == "call"
    assert rule(opted_out=True)["action"] == "skip"
    assert rule(consent=False, purpose="follow_up")["reason"] == "no outbound consent recorded"
    assert run_code("va-outbound-scheduler", "Calling rules", [{"jobs": []}]) == []


def test_session_context_and_phone_normalisation():
    p = run_code("va-session-start", "Prepare session",
                 [{"req": {"session_id": "abcdef12-3456", "channel": "browser_test", "caller": {"number": "03001234567"}}}])[0]
    assert p["valid"] and p["phone"] == "+923001234567" and p["line"] == "default" and p["direction"] == "inbound"
    row = {"business": {"id": BIZ, "name": "Karachi Pizza House"},
           "config": {"instructions": "x", "language": "auto", "greeting": {"en": "Hello"}, "greeting_outbound": {"en": "Hi, calling about"},
                      "max_call_seconds": 600, "recording_notice": True},
           "knowledge": ["Delivery: 40 min"], "tools": [{"type": "function", "function": {"name": "get_menu"}}],
           "customer": {"id": "c", "name": None}, "memory": [], "recent_orders": []}
    ctx = run_code("va-session-start", "Build context", [row], {"Prepare session": [p]})[0]["response"]
    assert ctx["ok"] and ctx["business_id"] == BIZ and ctx["greeting"] == {"en": "Hello"} and ctx["tools"][0]["function"]["name"] == "get_menu"
    missing = run_code("va-session-start", "Build context", [{"business": None}], {"Prepare session": [p]})[0]["response"]
    assert missing["ok"] is False
