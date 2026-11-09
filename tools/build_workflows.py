"""Generate the n8n workflows (n8n 2.45.x) as importable JSON files in n8n/workflows/.

  python tools/build_workflows.py

Node versions were checked against n8n-nodes-base 2.45.2 (the version bundled with n8n 2.45.3):
webhook 2, crypto 2, code 2, switch 3 (expression mode), postgres 2.8, respondToWebhook 1.1,
httpRequest 4.2, scheduleTrigger 1.2, errorTrigger 1.

Responsibilities (n8n owns the business logic; SQL only reads/writes, always scoped by business_id):
  VA Session Start  (Call Router, Business Configuration, Customer Lookup, Customer Memory)
  VA Tool           (AI Tool Execution, Order Management, Booking & Reservations, Human Handoff)
  VA Session End    (Post-Call Processing, usage tracking, summaries, memory)
  VA Call Event     (call status tracking for the phone line)
  VA Outbound Scheduler (Outbound Call Scheduler: consent, opt-out, calling hours)
  VA Notifications  (Notifications: delivers queued events to a configured webhook)
  VA Error Handler  (Error Handling and Monitoring)
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "n8n" / "workflows"
NS = uuid.UUID("6f1c2b1e-6f3a-4c55-9e0f-6b1d3d2f7a10")

CRED_PG = {"postgres": {"id": "vaPgApp000000001", "name": "VA Postgres (va_app)"}}
CRED_HMAC = {"crypto": {"id": "vaHmacSecret0001", "name": "VA webhook HMAC"}}
CRED_LLM = {"httpHeaderAuth": {"id": "vaLlmApiKey00001", "name": "VA LLM key"}}
CRED_CALLS = {"httpHeaderAuth": {"id": "vaCallsToken0001", "name": "VA voice engine calls token"}}
ERROR_WF_ID = "vaWfErrors000001"

# ------------------------------------------------------------------ shared JavaScript
JS_PHONE = r"""
function normPhone(p) {
  let n = String(p || '').replace(/[\s\-()]/g, '');
  if (!n) return null;
  if (n.startsWith('00')) n = '+' + n.slice(2);
  else if (n.startsWith('0')) n = '+92' + n.slice(1);
  else if (n.startsWith('92')) n = '+' + n;
  return /^\+[1-9][0-9]{6,14}$/.test(n) ? n : null;
}
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
"""

JS_VERIFY = r"""
// Verify the voice engine's HMAC (computed by the HMAC node) and timestamp, then parse the payload.
const it = $input.first().json;
const h = it.headers || {};
const sig = String(h['x-va-signature'] || '');
const exp = String(it.expected_sig || '');
let diff = sig.length === exp.length && sig.length === 64 ? 0 : 1;
for (let i = 0; i < Math.min(sig.length, exp.length); i++) diff |= sig.charCodeAt(i) ^ exp.charCodeAt(i);
const ts = Number(h['x-va-timestamp']);
const fresh = Number.isFinite(ts) && Math.abs(Date.now() / 1000 - ts) <= 300;
let req = null;
if (diff === 0 && fresh) {
  try { req = JSON.parse(String((it.body || {}).payload || '')); } catch (e) { req = null; }
}
const ok = diff === 0 && fresh && req !== null && typeof req === 'object' && !Array.isArray(req);
return [{ json: {
  ok,
  req: ok ? req : null,
  idem: String(h['x-va-idempotency-key'] || '').slice(0, 200),
  reason: ok ? '' : (diff !== 0 ? 'bad signature' : !fresh ? 'stale or missing timestamp' : 'payload is not a JSON object'),
} }];
"""


# ------------------------------------------------------------------ builders
class Wf:
    def __init__(self, wid: str, name: str, error_wf: bool = True):
        self.wid, self.name = wid, name
        self.nodes: list[dict] = []
        self.connections: dict[str, dict] = {}
        self.error_wf = error_wf
        self._x = 0

    def add(self, name: str, type_: str, version: float, params: dict, *, creds: dict | None = None,
            pos: tuple[int, int] | None = None, **extra) -> str:
        if any(n["name"] == name for n in self.nodes):
            raise ValueError(f"duplicate node name {name!r} in {self.name}")
        if pos is None:
            self._x += 240
            pos = (self._x, 300)
        n = {"parameters": params, "id": str(uuid.uuid5(NS, f"{self.wid}:{name}")), "name": name,
             "type": type_, "typeVersion": version, "position": list(pos)}
        if creds:
            n["credentials"] = creds
        n.update(extra)
        self.nodes.append(n)
        return name

    def link(self, src: str, dst: str, out: int = 0) -> None:
        main = self.connections.setdefault(src, {"main": []})["main"]
        while len(main) <= out:
            main.append([])
        main[out].append({"node": dst, "type": "main", "index": 0})

    def chain(self, *names: str) -> None:
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    def json(self) -> dict:
        settings = {"executionOrder": "v1", "saveManualExecutions": True}
        if self.error_wf:
            settings["errorWorkflow"] = ERROR_WF_ID
        return {"id": self.wid, "name": self.name, "active": False, "nodes": self.nodes,
                "connections": self.connections, "settings": settings, "pinData": {},
                "meta": {"templateCredsSetupCompleted": True}, "tags": []}

    # -- node helpers
    def code(self, name: str, js: str, **kw) -> str:
        return self.add(name, "n8n-nodes-base.code", 2, {"jsCode": js.strip() + "\n"}, **kw)

    def pg(self, name: str, sql: str, params_expr: str = "={{ [ JSON.stringify($json) ] }}", **kw) -> str:
        return self.add(name, "n8n-nodes-base.postgres", 2.8,
                        {"operation": "executeQuery", "query": sql.strip(),
                         "options": {"queryReplacement": params_expr}}, creds=CRED_PG, **kw)

    def switch(self, name: str, n_out: int, expr: str, **kw) -> str:
        return self.add(name, "n8n-nodes-base.switch", 3,
                        {"mode": "expression", "numberOutputs": n_out, "output": expr}, **kw)

    def respond(self, name: str, body_expr: str = "={{ JSON.stringify($json.response) }}", code: int = 200,
                **kw) -> str:
        return self.add(name, "n8n-nodes-base.respondToWebhook", 1.1,
                        {"respondWith": "json", "responseBody": body_expr,
                         "options": {"responseCode": code}}, **kw)

    def webhook_in(self, path: str) -> str:
        """Webhook -> HMAC -> Verify -> Switch(ok?) with a 401 branch. Returns the switch name (output 0 = ok)."""
        wh = self.add("Webhook", "n8n-nodes-base.webhook", 2,
                      {"httpMethod": "POST", "path": path, "responseMode": "responseNode", "options": {}},
                      webhookId=str(uuid.uuid5(NS, f"webhook:{path}")))
        hm = self.add("HMAC", "n8n-nodes-base.crypto", 2,
                      {"action": "hmac", "type": "SHA256",
                       "value": "={{ String($json.headers['x-va-timestamp'] || '') + '.' + String(($json.body || {}).payload || '') }}",
                       "dataPropertyName": "expected_sig", "encoding": "hex"}, creds=CRED_HMAC)
        vf = self.code("Verify request", JS_VERIFY)
        sw = self.switch("Authorized?", 2, "={{ $json.ok ? 0 : 1 }}")
        rj = self.respond("Reject", "={{ JSON.stringify({ ok: false, error: $json.reason }) }}", 401,
                          pos=(self._x + 240, 520))
        self.chain(wh, hm, vf, sw)
        self.link(sw, rj, 1)
        return sw


# ================================================================== 1. session start
def session_start() -> Wf:
    w = Wf("vaWfSessStart001", "VA Session Start")
    ok = w.webhook_in("voice/session-start")
    prep = w.code("Prepare session", JS_PHONE + r"""
const r = $input.first().json.req;
const sid = String(r.session_id || '');
const channel = ['browser_test', 'gsm', 'sip'].includes(r.channel) ? r.channel : null;
if (sid.length < 8 || sid.length > 64 || !channel) {
  return [{ json: { valid: false, response: { ok: false, error: 'invalid session_id or channel' } } }];
}
const ctx = (r.context && typeof r.context === 'object') ? r.context : {};
return [{ json: {
  valid: true,
  session_id: sid,
  channel,
  line: String(r.line || 'default').slice(0, 64),
  direction: r.direction === 'outbound' ? 'outbound' : 'inbound',
  phone: normPhone((r.caller || {}).number),
  gsm_call_id: String(r.call_id || '').slice(0, 64),
  context_business_id: UUID_RE.test(String(ctx.business_id || '')) ? ctx.business_id : null,
} }];
""")
    valid = w.switch("Valid?", 2, "={{ $json.valid ? 0 : 1 }}")
    load = w.pg("Load session", r"""
WITH r AS (SELECT $1::jsonb AS j),
biz AS (
  SELECT b.id, b.name
  FROM va.businesses b, r
  WHERE b.status = 'active'
    AND (b.id::text = r.j->>'context_business_id'
         OR b.id IN (SELECT n.business_id FROM va.business_phone_numbers n
                     WHERE n.channel = r.j->>'channel' AND n.line_name = r.j->>'line'))
  ORDER BY (b.id::text = COALESCE(r.j->>'context_business_id', '')) DESC
  LIMIT 1),
cust AS (
  INSERT INTO va.customers (business_id, phone_e164)
  SELECT biz.id, r.j->>'phone' FROM biz, r WHERE r.j->>'phone' IS NOT NULL
  ON CONFLICT (business_id, phone_e164) DO UPDATE SET updated_at = now()
  RETURNING id, name, preferred_language, opted_out_at),
call AS (
  INSERT INTO va.calls (id, business_id, customer_id, channel, direction, remote_number, gsm_call_id)
  SELECT r.j->>'session_id', biz.id, (SELECT id FROM cust), r.j->>'channel', r.j->>'direction',
         r.j->>'phone', NULLIF(r.j->>'gsm_call_id', '')
  FROM biz, r
  ON CONFLICT (id) DO NOTHING
  RETURNING id)
SELECT
  (SELECT to_jsonb(biz) FROM biz) AS business,
  (SELECT to_jsonb(a) - 'business_id' FROM va.agent_configs a JOIN biz ON a.business_id = biz.id) AS config,
  (SELECT COALESCE(jsonb_agg(k.title || ': ' || k.content ORDER BY k.title), '[]'::jsonb)
     FROM va.knowledge_items k JOIN biz ON k.business_id = biz.id WHERE k.active) AS knowledge,
  (SELECT COALESCE(jsonb_agg(jsonb_build_object('type', 'function', 'function',
            jsonb_build_object('name', t.name, 'description', t.description, 'parameters', t.parameters))
            ORDER BY t.name), '[]'::jsonb)
     FROM va.tool_definitions t
     JOIN va.agent_configs a ON t.name = ANY (a.enabled_tools)
     JOIN biz ON a.business_id = biz.id) AS tools,
  (SELECT to_jsonb(cust) FROM cust) AS customer,
  (SELECT COALESCE(jsonb_agg(m.content), '[]'::jsonb) FROM (
     SELECT content FROM va.customer_memory
     WHERE customer_id = (SELECT id FROM cust) ORDER BY created_at DESC LIMIT 5) m) AS memory,
  (SELECT COALESCE(jsonb_agg(jsonb_build_object('order_number', o.order_number, 'status', o.status)), '[]'::jsonb) FROM (
     SELECT order_number, status FROM va.orders
     WHERE customer_id = (SELECT id FROM cust) ORDER BY created_at DESC LIMIT 3) o) AS recent_orders,
  (SELECT count(*) FROM call) AS call_created
""")
    build = w.code("Build context", r"""
const row = $input.first().json;
const req = $('Prepare session').first().json;
if (!row.business || !row.config) {
  return [{ json: { response: { ok: false, error: `no active business configured for ${req.channel} line "${req.line}"` } } }];
}
const cfg = row.config;
const c = row.customer;
const customer = c ? {
  known: Boolean(c.name), name: c.name || null, phone: req.phone,
  preferred_language: c.preferred_language || null,
  remembered: row.memory || [], recent_orders: row.recent_orders || [],
} : {};
return [{ json: { response: {
  ok: true,
  session_id: req.session_id,
  business_id: row.business.id,
  business_name: row.business.name,
  agent_instructions: cfg.instructions,
  language: cfg.language,
  knowledge: row.knowledge || [],
  customer,
  tools: row.tools || [],
  greeting: (req.direction === 'outbound' ? cfg.greeting_outbound : cfg.greeting) || {},
  max_call_seconds: cfg.max_call_seconds,
  recording_consent_required: cfg.recording_notice,
} } }];
""")
    out = w.respond("Respond")
    bad = w.respond("Respond invalid", code=400, pos=(w._x, 520))
    w.link(ok, prep)
    w.chain(prep, valid, load, build, out)
    w.link(valid, bad, 1)
    return w


# ================================================================== 2. tools
TOOLS = ["get_menu", "lookup_order", "create_order", "confirm_order", "check_availability", "create_booking",
         "remember_customer_preference", "request_human_handoff"]


def tool_workflow() -> Wf:
    w = Wf("vaWfTool00000001", "VA Tool")
    ok = w.webhook_in("voice/tool")
    prep = w.code("Prepare tool call", JS_PHONE + r"""
// Validate the tool call (business rules on inputs) before touching the database.
const v = $input.first().json;
const r = v.req;
const tool = String(r.tool || '');
const a = (r.arguments && typeof r.arguments === 'object' && !Array.isArray(r.arguments)) ? r.arguments : {};
const fail = (msg) => [{ json: { route: 'respond', idem: v.idem, response: { ok: false, error: msg } } }];
const TOOLS = %TOOLS%;
if (!UUID_RE.test(String(r.business_id || '')) || !r.session_id) return fail('bad request: business_id and session_id are required');
if (!TOOLS.includes(tool)) return fail(`unknown tool ${tool}`);
const intIn = (x, lo, hi) => Number.isInteger(Number(x)) && Number(x) >= lo && Number(x) <= hi;
const args = {};
if (tool === 'get_menu') {
  args.category = String(a.category || '').toLowerCase().trim().slice(0, 40);
} else if (tool === 'lookup_order' || tool === 'confirm_order') {
  args.order_number = String(a.order_number || '').replace(/\D/g, '');
  if (!/^\d{4,10}$/.test(args.order_number)) return fail('order_number must be 4 to 10 digits; ask the caller to repeat it');
  if (tool === 'confirm_order') {
    if (!['confirm', 'cancel'].includes(a.decision)) return fail('decision must be confirm or cancel');
    args.decision = a.decision;
  }
} else if (tool === 'create_order') {
  const items = Array.isArray(a.items) ? a.items : [];
  if (items.length < 1 || items.length > 20) return fail('an order needs 1 to 20 items');
  args.items = [];
  for (const x of items) {
    const sku = String((x || {}).sku || '').toUpperCase().trim();
    if (!/^[A-Z0-9][A-Z0-9-]{1,31}$/.test(sku)) return fail(`invalid sku "${sku}"; use SKUs from get_menu`);
    if (!intIn((x || {}).quantity, 1, 20)) return fail(`quantity for ${sku} must be 1 to 20`);
    args.items.push({ sku, quantity: Number(x.quantity) });
  }
  args.delivery_address = String(a.delivery_address || '').trim().slice(0, 300);
  if (args.delivery_address.length < 5) return fail('a delivery address is required; ask the caller for it');
  args.customer_name = String(a.customer_name || '').trim().slice(0, 120) || null;
  args.customer_phone = a.customer_phone ? normPhone(a.customer_phone) : null;
  if (a.customer_phone && !args.customer_phone) return fail('the phone number is not valid; ask the caller to repeat it');
} else if (tool === 'check_availability' || tool === 'create_booking') {
  const date = String(a.date || ''), time = String(a.time || '');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || isNaN(Date.parse(date + 'T00:00:00Z'))) return fail('date must be YYYY-MM-DD');
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) return fail('time must be HH:MM (24 hour)');
  if (!intIn(a.party_size, 1, 50)) return fail('party_size must be 1 to 50');
  Object.assign(args, { date, time, party_size: Number(a.party_size) });
  if (tool === 'create_booking') {
    args.name = String(a.name || '').trim().slice(0, 120);
    if (!args.name) return fail('a name for the booking is required');
    args.customer_phone = a.customer_phone ? normPhone(a.customer_phone) : null;
  }
} else if (tool === 'remember_customer_preference') {
  args.note = String(a.note || '').trim().slice(0, 500);
  if (!args.note) return fail('note is empty');
} else if (tool === 'request_human_handoff') {
  args.reason = String(a.reason || '').trim().slice(0, 500) || 'caller asked for a human';
}
return [{ json: { route: 'db', tool, args, business_id: r.business_id, session_id: String(r.session_id), idem: v.idem } }];
""".replace("%TOOLS%", json.dumps(TOOLS)))
    valid = w.switch("Valid?", 2, "={{ $json.route === 'db' ? 0 : 1 }}")
    auth = w.pg("Authorize session", r"""
WITH r AS (SELECT $1::jsonb AS j)
SELECT
  EXISTS (SELECT 1 FROM va.calls c, r WHERE c.id = r.j->>'session_id' AND c.business_id::text = r.j->>'business_id') AS authorized,
  (SELECT k.response FROM va.idempotency_keys k, r WHERE k.key = r.j->>'idem' AND r.j->>'idem' <> '') AS replay,
  (SELECT c.customer_id FROM va.calls c, r WHERE c.id = r.j->>'session_id' AND c.business_id::text = r.j->>'business_id') AS customer_id
""")
    route = w.code("Route tool", r"""
const p = $('Prepare tool call').first().json;
const row = $input.first().json;
if (row.replay) return [{ json: { route: 'respond', replay: true, response: row.replay } }];
if (!row.authorized) return [{ json: { route: 'respond', response: { ok: false, error: 'session is not active for this business' } } }];
return [{ json: { ...p, customer_id: row.customer_id || null } }];
""")
    tsw = w.switch("Tool", len(TOOLS) + 1,
                   "={{ " + json.dumps(TOOLS) + ".indexOf($json.tool) >= 0 && $json.route !== 'respond' ? "
                   + json.dumps(TOOLS) + ".indexOf($json.tool) : " + str(len(TOOLS)) + " }}")
    store = w.pg("Store response", r"""
WITH r AS (SELECT $1::jsonb AS j),
ins AS (
  INSERT INTO va.idempotency_keys (key, endpoint, response)
  SELECT r.j->>'idem', 'tool', r.j->'response' FROM r
  WHERE COALESCE(r.j->>'idem', '') <> '' AND NOT COALESCE((r.j->>'replay')::boolean, false)
  ON CONFLICT (key) DO NOTHING
  RETURNING 1)
SELECT (SELECT r.j->'response' FROM r) AS response, (SELECT count(*) FROM ins) AS stored
""", "={{ [ JSON.stringify({ idem: $('Prepare tool call').first().json.idem, replay: Boolean($json.replay), response: $json.response }) ] }}",
                 pos=(3600, 300))
    respond = w.respond("Respond", pos=(3840, 300))
    w.link(ok, prep)
    w.chain(prep, valid, auth, route, tsw)
    w.link(valid, store, 1)
    w.chain(store, respond)

    y = 0

    def lane(*names: str, out: int) -> None:
        w.link(tsw, names[0], out)
        w.chain(*names)
        w.link(names[-1], store)

    def at(col: int) -> tuple[int, int]:
        return (2000 + col * 240, -400 + y * 170)

    # 0 get_menu
    y = 0
    lane(w.pg("Menu", r"""
WITH r AS (SELECT $1::jsonb AS j)
SELECT COALESCE(jsonb_agg(jsonb_build_object('sku', p.sku, 'name', p.name_en, 'name_ur', p.name_ur,
                                              'category', p.category, 'price_pkr', p.price_pkr) ORDER BY p.sort_order),
                '[]'::jsonb) AS items
FROM va.products p, r
WHERE p.business_id::text = r.j->>'business_id' AND p.available
  AND (COALESCE(r.j->'args'->>'category', '') = '' OR p.category = r.j->'args'->>'category')
""", pos=at(0)), w.code("Menu result", r"""
const items = $input.first().json.items || [];
return [{ json: { response: { ok: true, result: {
  currency: 'PKR', items,
  note: items.length ? 'Offer only these items.' : 'Nothing in that category. Offer get_menu without a category.',
} } } }];
""", pos=at(1)), out=0)

    # 1 lookup_order
    y = 1
    lane(w.pg("Find order", r"""
WITH r AS (SELECT $1::jsonb AS j)
SELECT (
  SELECT jsonb_build_object(
    'order_number', o.order_number, 'status', o.status, 'eta_minutes', o.eta_minutes,
    'total_pkr', o.total_pkr, 'delivery_address', o.delivery_address,
    'items', (SELECT COALESCE(jsonb_agg(jsonb_build_object('name', p.name_en, 'quantity', i.quantity)), '[]'::jsonb)
              FROM va.order_items i JOIN va.products p ON p.id = i.product_id WHERE i.order_id = o.id))
  FROM va.orders o, r
  WHERE o.business_id::text = r.j->>'business_id' AND o.order_number = r.j->'args'->>'order_number'
) AS found_order
""", pos=at(0)), w.code("Order result", r"""
const o = $input.first().json.found_order;
return [{ json: { response: o ? { ok: true, result: { found: true, ...o } }
                              : { ok: true, result: { found: false, note: 'No order with that number. Ask the caller to check it.' } } } }];
""", pos=at(1)), out=1)

    # 2 create_order: price -> check (rules) -> insert
    y = 2
    price = w.pg("Price items", r"""
WITH r AS (SELECT $1::jsonb AS j),
req AS (SELECT x->>'sku' AS sku, (x->>'quantity')::int AS quantity
        FROM r, jsonb_array_elements(r.j->'args'->'items') x)
SELECT
  (SELECT COALESCE(jsonb_agg(jsonb_build_object('sku', req.sku, 'quantity', req.quantity, 'product_id', p.id,
                                                'name', p.name_en, 'price_pkr', p.price_pkr, 'available', p.available)),
                   '[]'::jsonb)
     FROM req LEFT JOIN va.products p ON p.sku = req.sku AND p.business_id::text = (SELECT j->>'business_id' FROM r)) AS lines,
  (SELECT a.delivery_eta_minutes FROM va.agent_configs a, r WHERE a.business_id::text = r.j->>'business_id') AS eta
""", pos=at(0))
    check = w.code("Check order", r"""
// Business rules for a new order: every item must exist and be available; total computed here.
const p = $('Route tool').first().json;
const row = $input.first().json;
const lines = row.lines || [];
const unknown = lines.filter(l => !l.product_id).map(l => l.sku);
const unavailable = lines.filter(l => l.product_id && !l.available).map(l => l.name);
if (unknown.length) return [{ json: { ok: false, response: { ok: false, error: `not on the menu: ${unknown.join(', ')}. Call get_menu and use its SKUs.` } } }];
if (unavailable.length) return [{ json: { ok: false, response: { ok: false, error: `currently unavailable: ${unavailable.join(', ')}` } } }];
const phone = p.args.customer_phone || null;
if (!p.customer_id && !phone) return [{ json: { ok: false, response: { ok: false, error: 'the caller is not identified; ask for their phone number and pass customer_phone' } } }];
const total = lines.reduce((s, l) => s + Number(l.price_pkr) * Number(l.quantity), 0);
return [{ json: { ok: true, business_id: p.business_id, session_id: p.session_id, customer_id: p.customer_id,
  phone, name: p.args.customer_name, address: p.args.delivery_address, total: Math.round(total * 100) / 100,
  eta: row.eta || 40, lines } }];
""", pos=at(1))
    okord = w.switch("Order ok?", 2, "={{ $json.ok ? 0 : 1 }}", pos=at(2))
    ins = w.pg("Insert order", r"""
WITH r AS (SELECT $1::jsonb AS j),
cust AS (
  INSERT INTO va.customers (business_id, phone_e164, name)
  SELECT (r.j->>'business_id')::uuid, r.j->>'phone', NULLIF(r.j->>'name', '') FROM r
  WHERE r.j->>'phone' IS NOT NULL
  ON CONFLICT (business_id, phone_e164) DO UPDATE SET name = COALESCE(EXCLUDED.name, va.customers.name)
  RETURNING id),
num AS (
  SELECT (COALESCE(max(o.order_number::bigint), 1000) + 1)::text AS n
  FROM va.orders o, r WHERE o.business_id::text = r.j->>'business_id'),
o AS (
  INSERT INTO va.orders (business_id, order_number, customer_id, status, delivery_address, total_pkr,
                         eta_minutes, created_via_call_id)
  SELECT (r.j->>'business_id')::uuid, num.n,
         COALESCE((SELECT id FROM cust), NULLIF(r.j->>'customer_id', '')::uuid),
         'confirmed', r.j->>'address', (r.j->>'total')::numeric, (r.j->>'eta')::int, r.j->>'session_id'
  FROM r, num
  RETURNING id, business_id, order_number, total_pkr, eta_minutes, customer_id),
items AS (
  INSERT INTO va.order_items (business_id, order_id, product_id, quantity, unit_price_pkr)
  SELECT o.business_id, o.id, (l->>'product_id')::uuid, (l->>'quantity')::int, (l->>'price_pkr')::numeric
  FROM o, r, jsonb_array_elements(r.j->'lines') l
  RETURNING 1),
link AS (
  UPDATE va.calls c SET customer_id = o.customer_id
  FROM o, r WHERE c.id = r.j->>'session_id' AND c.business_id = o.business_id AND c.customer_id IS NULL
  RETURNING 1)
SELECT o.order_number, o.total_pkr, o.eta_minutes, (SELECT count(*) FROM items) AS item_lines,
       (SELECT count(*) FROM link) AS call_linked
FROM o
""", pos=at(3))
    done = w.code("Order created", r"""
const o = $input.first().json;
return [{ json: { response: { ok: true, result: {
  order_number: o.order_number, total_pkr: Number(o.total_pkr), eta_minutes: o.eta_minutes, status: 'confirmed',
  note: 'Tell the caller the order number, the total and the delivery time.',
} } } }];
""", pos=at(4))
    w.link(tsw, price, 2)
    w.chain(price, check, okord, ins, done)
    w.link(okord, store, 1)
    w.link(done, store)

    # 3 confirm_order
    y = 3
    lane(w.pg("Update order", r"""
WITH r AS (SELECT $1::jsonb AS j),
u AS (
  UPDATE va.orders o
  SET status = CASE WHEN r.j->'args'->>'decision' = 'cancel' THEN 'cancelled' ELSE 'confirmed' END
  FROM r
  WHERE o.business_id::text = r.j->>'business_id' AND o.order_number = r.j->'args'->>'order_number'
    AND o.status = 'pending'
  RETURNING o.order_number, o.status)
SELECT (SELECT to_jsonb(u) FROM u) AS updated,
       (SELECT o.status FROM va.orders o, r
        WHERE o.business_id::text = r.j->>'business_id' AND o.order_number = r.j->'args'->>'order_number') AS previous_status
""", pos=at(0)), w.code("Confirm result", r"""
// Rule: only pending orders can be confirmed or cancelled by the agent.
const row = $input.first().json;
if (row.updated) return [{ json: { response: { ok: true, result: row.updated } } }];
if (!row.previous_status) return [{ json: { response: { ok: true, result: { found: false, note: 'No order with that number.' } } } }];
return [{ json: { response: { ok: false, error: `order is already ${row.previous_status}; it cannot be changed by phone. Offer a human if needed.` } } }];
""", pos=at(1)), out=3)

    # 4/5 availability + booking
    y = 4
    load = w.pg("Booking load", r"""
WITH r AS (SELECT $1::jsonb AS j),
b AS (SELECT bz.id, bz.timezone FROM va.businesses bz, r WHERE bz.id::text = r.j->>'business_id'),
a AS (SELECT ac.* FROM va.agent_configs ac JOIN b ON ac.business_id = b.id),
t AS (SELECT ((r.j->'args'->>'date') || ' ' || (r.j->'args'->>'time'))::timestamp AT TIME ZONE b.timezone AS starts_at
      FROM r, b)
SELECT
  (SELECT starts_at FROM t) AS starts_at,
  (SELECT now()) AS now,
  (SELECT jsonb_build_object('capacity', booking_capacity_per_slot, 'slot_minutes', booking_slot_minutes,
                             'opening_time', to_char(opening_time, 'HH24:MI'), 'closing_time', to_char(closing_time, 'HH24:MI'))
     FROM a) AS cfg,
  (SELECT COALESCE(sum(bk.party_size), 0) FROM va.bookings bk, a, t
    WHERE bk.business_id = a.business_id AND bk.status = 'confirmed'
      AND bk.starts_at > t.starts_at - make_interval(mins => a.booking_slot_minutes)
      AND bk.starts_at < t.starts_at + make_interval(mins => a.booking_slot_minutes)) AS seats_taken
""", pos=at(0))
    decide = w.code("Decide booking", r"""
// Business rules: opening hours, not in the past, seat capacity per time slot.
const p = $('Route tool').first().json;
const row = $input.first().json;
const cfg = row.cfg || {};
const a = p.args;
let reason = null;
if (!cfg.capacity) reason = 'bookings are not configured for this business';
else if (new Date(row.starts_at) <= new Date(row.now)) reason = 'that time is in the past';
else if (a.time < cfg.opening_time || a.time > cfg.closing_time) reason = `we are open ${cfg.opening_time} to ${cfg.closing_time}`;
else if (Number(row.seats_taken) + a.party_size > cfg.capacity) reason = 'fully booked at that time; suggest a time at least ' + cfg.slot_minutes + ' minutes earlier or later';
const available = reason === null;
if (p.tool === 'check_availability' || !available) {
  return [{ json: { book: false, response: { ok: true, result: { available, date: a.date, time: a.time, party_size: a.party_size, ...(reason ? { reason } : {}) } } } }];
}
return [{ json: { book: true, business_id: p.business_id, session_id: p.session_id, customer_id: p.customer_id,
  phone: a.customer_phone, name: a.name, party_size: a.party_size, starts_at: row.starts_at } }];
""", pos=at(1))
    bsw = w.switch("Book?", 2, "={{ $json.book ? 0 : 1 }}", pos=at(2))
    binsert = w.pg("Insert booking", r"""
WITH r AS (SELECT $1::jsonb AS j),
cust AS (
  INSERT INTO va.customers (business_id, phone_e164, name)
  SELECT (r.j->>'business_id')::uuid, r.j->>'phone', NULLIF(r.j->>'name', '') FROM r
  WHERE r.j->>'phone' IS NOT NULL
  ON CONFLICT (business_id, phone_e164) DO UPDATE SET name = COALESCE(va.customers.name, EXCLUDED.name)
  RETURNING id),
ref AS (
  SELECT 'BK-' || (COALESCE(max(substring(bk.booking_ref FROM 4)::bigint), 1000) + 1) AS v
  FROM va.bookings bk, r WHERE bk.business_id::text = r.j->>'business_id'),
bk AS (
  INSERT INTO va.bookings (business_id, booking_ref, customer_id, name, starts_at, party_size, created_via_call_id)
  SELECT (r.j->>'business_id')::uuid, ref.v, COALESCE((SELECT id FROM cust), NULLIF(r.j->>'customer_id', '')::uuid),
         r.j->>'name', (r.j->>'starts_at')::timestamptz, (r.j->>'party_size')::int, r.j->>'session_id'
  FROM r, ref
  RETURNING booking_ref, party_size, starts_at, business_id)
SELECT bk.booking_ref, bk.party_size,
       to_char(bk.starts_at AT TIME ZONE (SELECT timezone FROM va.businesses WHERE id = bk.business_id), 'YYYY-MM-DD HH24:MI') AS local_time
FROM bk
""", pos=at(3))
    bdone = w.code("Booking created", r"""
const b = $input.first().json;
return [{ json: { response: { ok: true, result: { booked: true, booking_ref: b.booking_ref, time: b.local_time,
  party_size: b.party_size, note: 'Tell the caller the booking reference.' } } } }];
""", pos=at(4))
    w.link(tsw, load, 4)
    w.link(tsw, load, 5)
    w.chain(load, decide, bsw, binsert, bdone)
    w.link(bsw, store, 1)
    w.link(bdone, store)

    # 6 remember preference
    y = 6
    lane(w.pg("Save preference", r"""
WITH r AS (SELECT $1::jsonb AS j),
m AS (
  INSERT INTO va.customer_memory (business_id, customer_id, kind, content, source_call_id)
  SELECT (r.j->>'business_id')::uuid, (r.j->>'customer_id')::uuid, 'preference', r.j->'args'->>'note', r.j->>'session_id'
  FROM r WHERE NULLIF(r.j->>'customer_id', '') IS NOT NULL
  RETURNING 1)
SELECT (SELECT count(*) FROM m) AS saved
""", pos=at(0)), w.code("Preference result", r"""
const saved = Number($input.first().json.saved) > 0;
return [{ json: { response: saved ? { ok: true, result: { saved: true } }
  : { ok: false, error: 'the caller is not identified (no phone number), so nothing can be remembered' } } }];
""", pos=at(1)), out=6)

    # 7 human handoff
    y = 7
    lane(w.pg("Create handoff", r"""
WITH r AS (SELECT $1::jsonb AS j),
h AS (
  INSERT INTO va.handoff_requests (business_id, call_id, customer_id, reason)
  SELECT (r.j->>'business_id')::uuid, r.j->>'session_id', NULLIF(r.j->>'customer_id', '')::uuid, r.j->'args'->>'reason'
  FROM r
  RETURNING id, business_id, reason, call_id),
n AS (
  INSERT INTO va.notifications (business_id, kind, payload)
  SELECT h.business_id, 'human_handoff', jsonb_build_object('handoff_id', h.id, 'reason', h.reason, 'call_id', h.call_id)
  FROM h
  RETURNING 1)
SELECT (SELECT id FROM h) AS handoff_id, (SELECT count(*) FROM n) AS notified
""", pos=at(0)), w.code("Handoff result", r"""
return [{ json: { response: { ok: true, result: { queued: true, handoff_id: $input.first().json.handoff_id,
  note: 'A team member has been notified and will call the customer back. Live transfer is not available.' } } } }];
""", pos=at(1)), out=7)

    # 8 respond directly (validation errors, replays, unauthorized)
    w.link(tsw, store, len(TOOLS))
    return w


# ================================================================== 3. session end
def session_end() -> Wf:
    w = Wf("vaWfSessEnd00001", "VA Session End")
    ok = w.webhook_in("voice/session-end")
    prep = w.code("Prepare report", JS_PHONE + r"""
const r = $input.first().json.req;
if (!UUID_RE.test(String(r.business_id || '')) || !r.session_id) {
  return [{ json: { valid: false, response: { ok: false, error: 'business_id and session_id are required' } } }];
}
const transcript = (Array.isArray(r.transcript) ? r.transcript : []).slice(0, 500)
  .filter(t => t && (t.role === 'user' || t.role === 'assistant') && typeof t.text === 'string')
  .map(t => ({ role: t.role, text: t.text.slice(0, 4000), language: ['en', 'ur'].includes(t.language) ? t.language : null,
               interrupted: Boolean(t.interrupted), t: Number(t.t) || null }));
return [{ json: { valid: true, business_id: r.business_id, session_id: String(r.session_id),
  ended_at: Number(r.ended_at) || Date.now() / 1000, end_reason: String(r.end_reason || '').slice(0, 120),
  turns: Number.isInteger(r.turns) ? r.turns : 0, transcript } }];
""")
    valid = w.switch("Valid?", 2, "={{ $json.valid ? 0 : 1 }}")
    save = w.pg("Save call", r"""
WITH r AS (SELECT $1::jsonb AS j),
c AS (
  UPDATE va.calls SET status = 'ended', ended_at = to_timestamp((r.j->>'ended_at')::float8),
         end_reason = r.j->>'end_reason', turns = (r.j->>'turns')::int
  FROM r
  WHERE va.calls.id = r.j->>'session_id' AND va.calls.business_id::text = r.j->>'business_id'
  RETURNING va.calls.id, va.calls.business_id, va.calls.started_at, va.calls.ended_at, va.calls.customer_id),
t AS (
  INSERT INTO va.transcripts (business_id, call_id, seq, role, text, language, interrupted, spoken_at)
  SELECT c.business_id, c.id, (e.ord - 1)::int, e.v->>'role', e.v->>'text', e.v->>'language',
         COALESCE((e.v->>'interrupted')::boolean, false), to_timestamp(NULLIF(e.v->>'t', '')::float8)
  FROM c, r, jsonb_array_elements(r.j->'transcript') WITH ORDINALITY AS e(v, ord)
  ON CONFLICT (call_id, seq) DO NOTHING
  RETURNING 1),
u AS (
  INSERT INTO va.usage_records (business_id, call_id, kind, quantity)
  SELECT c.business_id, c.id, k.kind, k.q
  FROM c, r, LATERAL (VALUES ('call_seconds', GREATEST(0, extract(epoch FROM c.ended_at - c.started_at))::numeric),
                             ('turns', COALESCE((r.j->>'turns')::numeric, 0))) AS k(kind, q)
  ON CONFLICT (call_id, kind) DO NOTHING
  RETURNING 1)
SELECT (SELECT count(*) FROM c) AS calls_updated, (SELECT count(*) FROM t) AS lines,
       (SELECT customer_id FROM c) AS customer_id, (SELECT business_id FROM c) AS business_id
""")
    out = w.respond("Respond", "={{ JSON.stringify({ ok: Number($json.calls_updated) > 0, lines: Number($json.lines) }) }}")
    bad = w.respond("Respond invalid", code=400, pos=(w._x, 520))
    ask = w.code("Summary request", r"""
// Post-call processing: ask the local LLM for a short summary + anything worth remembering.
const saved = $('Save call').first().json;
const p = $('Prepare report').first().json;
if (!Number(saved.calls_updated) || !p.transcript.length) return [];   // counts arrive as text, e.g. "0"
const convo = p.transcript.map(t => `${t.role === 'user' ? 'Caller' : 'Agent'}: ${t.text}`).join('\n').slice(-12000);
return [{ json: { llm_request: {
  model: 'local', temperature: 0.2, max_tokens: 300,
  chat_template_kwargs: { enable_thinking: false },
  response_format: { type: 'json_object' },
  messages: [
    { role: 'system', content: 'You summarise phone calls between a business\'s AI agent and a caller. Reply ONLY with JSON: {"summary": "<at most 3 sentences, English>", "outcome": "<a few words, e.g. order placed, booking made, question answered, no resolution>", "remember": "<a lasting caller preference worth remembering, or empty string>"}' },
    { role: 'user', content: convo },
  ] } } }];
""")
    llm = w.add("Summarize", "n8n-nodes-base.httpRequest", 4.2,
                {"method": "POST", "url": "http://127.0.0.1:8081/v1/chat/completions",
                 "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
                 "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.llm_request) }}",
                 "options": {"timeout": 90000}},
                creds=CRED_LLM, onError="continueRegularOutput")
    parse = w.code("Parse summary", r"""
const p = $('Prepare report').first().json;
const saved = $('Save call').first().json;
let s = {};
try { s = JSON.parse(((($input.first().json.choices || [])[0] || {}).message || {}).content || '{}'); } catch (e) { s = {}; }
return [{ json: { business_id: p.business_id, session_id: p.session_id, customer_id: saved.customer_id || null,
  summary: String(s.summary || '').slice(0, 4000), outcome: String(s.outcome || '').slice(0, 200),
  remember: String(s.remember || '').slice(0, 1000) } }];
""")
    keep = w.pg("Save summary", r"""
WITH r AS (SELECT $1::jsonb AS j),
s AS (
  INSERT INTO va.conversation_summaries (call_id, business_id, summary, outcome)
  SELECT r.j->>'session_id', (r.j->>'business_id')::uuid, r.j->>'summary', NULLIF(r.j->>'outcome', '') FROM r
  WHERE COALESCE(r.j->>'summary', '') <> ''
  ON CONFLICT (call_id) DO NOTHING
  RETURNING 1),
m AS (
  INSERT INTO va.customer_memory (business_id, customer_id, kind, content, source_call_id)
  SELECT (r.j->>'business_id')::uuid, (r.j->>'customer_id')::uuid, k.kind, left(k.txt, 1000), r.j->>'session_id'
  FROM r, LATERAL (VALUES ('summary', r.j->>'summary'), ('preference', r.j->>'remember')) AS k(kind, txt)
  WHERE NULLIF(r.j->>'customer_id', '') IS NOT NULL AND COALESCE(k.txt, '') <> '' AND EXISTS (SELECT 1 FROM s)
  RETURNING 1)
SELECT (SELECT count(*) FROM s) AS summaries, (SELECT count(*) FROM m) AS memories
""")
    w.link(ok, prep)
    w.chain(prep, valid, save, out, ask, llm, parse, keep)
    w.link(valid, bad, 1)
    return w


# ================================================================== 4. call event
def call_event() -> Wf:
    w = Wf("vaWfCallEvent001", "VA Call Event")
    ok = w.webhook_in("voice/call-event")
    prep = w.code("Prepare event", JS_PHONE + r"""
const r = $input.first().json.req;
const STATUSES = ['requested', 'dialing', 'alerting', 'incoming', 'active', 'ended', 'failed', 'rejected'];
if (!r.call_id || !STATUSES.includes(r.status)) {
  return [{ json: { valid: false, response: { ok: false, error: 'call_id and a valid status are required' } } }];
}
const ctx = (r.context && typeof r.context === 'object') ? r.context : {};
return [{ json: { valid: true, call_id: String(r.call_id).slice(0, 64), status: r.status,
  reason: String(r.reason || '').slice(0, 120), direction: r.direction === 'outbound' ? 'outbound' : 'inbound',
  remote_number: String(r.remote_number || '').slice(0, 32),
  business_id: UUID_RE.test(String(ctx.business_id || '')) ? ctx.business_id : null,
  job_id: UUID_RE.test(String(ctx.job_id || '')) ? ctx.job_id : null } }];
""")
    valid = w.switch("Valid?", 2, "={{ $json.valid ? 0 : 1 }}")
    rec = w.pg("Record event", r"""
WITH r AS (SELECT $1::jsonb AS j),
e AS (
  INSERT INTO va.call_events (business_id, gsm_call_id, status, reason, direction, remote_number)
  SELECT NULLIF(r.j->>'business_id', '')::uuid, r.j->>'call_id', r.j->>'status', NULLIF(r.j->>'reason', ''),
         r.j->>'direction', NULLIF(r.j->>'remote_number', '')
  FROM r
  ON CONFLICT (gsm_call_id, status) DO NOTHING
  RETURNING 1),
jb AS (
  UPDATE va.outbound_jobs oj SET
    gsm_call_id = r.j->>'call_id',
    status = CASE
      WHEN r.j->>'status' = 'ended' THEN 'completed'
      WHEN r.j->>'status' IN ('failed', 'rejected') AND oj.attempts < oj.max_attempts THEN 'pending'
      WHEN r.j->>'status' IN ('failed', 'rejected') THEN 'failed'
      ELSE oj.status END,
    scheduled_at = CASE WHEN r.j->>'status' IN ('failed', 'rejected') THEN now() + interval '15 minutes' ELSE oj.scheduled_at END,
    last_error = CASE WHEN r.j->>'status' IN ('failed', 'rejected') THEN NULLIF(r.j->>'reason', '') ELSE oj.last_error END
  FROM r WHERE oj.id::text = r.j->>'job_id' AND oj.business_id::text = r.j->>'business_id'
  RETURNING 1)
SELECT (SELECT count(*) FROM e) AS recorded, (SELECT count(*) FROM jb) AS job_updated
""")
    out = w.respond("Respond", "={{ JSON.stringify({ ok: true, recorded: Number($json.recorded) > 0 }) }}")
    bad = w.respond("Respond invalid", code=400, pos=(w._x, 520))
    w.link(ok, prep)
    w.chain(prep, valid, rec, out)
    w.link(valid, bad, 1)
    return w


# ================================================================== 5. outbound scheduler
def outbound() -> Wf:
    w = Wf("vaWfOutbound0001", "VA Outbound Scheduler")
    trig = w.add("Every minute", "n8n-nodes-base.scheduleTrigger", 1.2,
                 {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}})
    claim = w.pg("Claim due job", r"""
WITH due AS (
  SELECT j.id FROM va.outbound_jobs j
  WHERE j.status = 'pending' AND j.scheduled_at <= now() AND j.attempts < j.max_attempts
  ORDER BY j.scheduled_at
  LIMIT 1
  FOR UPDATE SKIP LOCKED),
claimed AS (
  UPDATE va.outbound_jobs j SET status = 'requested', attempts = j.attempts + 1
  FROM due WHERE j.id = due.id
  RETURNING j.*)
SELECT COALESCE(jsonb_agg(jsonb_build_object(
         'job_id', c.id, 'business_id', c.business_id, 'customer_id', c.customer_id, 'purpose', c.purpose,
         'context', c.context, 'attempt', c.attempts, 'phone', cu.phone_e164,
         'consent', cu.consent_outbound, 'opted_out', cu.opted_out_at IS NOT NULL)), '[]'::jsonb) AS jobs
FROM claimed c JOIN va.customers cu ON cu.business_id = c.business_id AND cu.id = c.customer_id
""", params_expr="={{ [] }}")
    rules = w.code("Calling rules", r"""
// Business rules for outbound calls: opt-out always wins, consent required, polite calling hours.
const jobs = $input.first().json.jobs || [];
if (!jobs.length) return [];
const j = jobs[0];
const hour = Number(new Intl.DateTimeFormat('en-GB', { hour: '2-digit', hour12: false, timeZone: 'Asia/Karachi' }).format(new Date()));
const test = j.purpose === 'manual_test';
let action = 'call', reason = '';
if (j.opted_out) { action = 'skip'; reason = 'customer opted out'; }
else if (!j.consent && !test) { action = 'skip'; reason = 'no outbound consent recorded'; }
else if (!j.phone) { action = 'skip'; reason = 'customer has no phone number'; }
else if (!test && (hour < 10 || hour >= 21)) { action = 'defer'; reason = 'outside calling hours (10:00-21:00 PKT)'; }
return [{ json: { action, reason, job: j } }];
""")
    sw = w.switch("Call?", 2, "={{ $json.action === 'call' ? 0 : 1 }}")
    http = w.add("Request call", "n8n-nodes-base.httpRequest", 4.2,
                 {"method": "POST", "url": "http://127.0.0.1:8765/calls",
                  "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
                  "sendBody": True, "specifyBody": "json",
                  "jsonBody": "={{ JSON.stringify({ number: $json.job.phone, idempotency_key: $json.job.job_id + ':' + $json.job.attempt, context: Object.assign({}, $json.job.context, { business_id: $json.job.business_id, customer_id: $json.job.customer_id, job_id: $json.job.job_id, purpose: $json.job.purpose }) }) }}",
                  "options": {"timeout": 15000}},
                 creds=CRED_CALLS, onError="continueRegularOutput")
    res = w.code("Request result", r"""
const job = $('Calling rules').first().json.job;
const r = $input.first().json;
const err = r.error ? (typeof r.error === 'string' ? r.error : (r.error.message || JSON.stringify(r.error))) : (r.call_id ? null : 'no call_id in response');
return [{ json: { job_id: job.job_id, business_id: job.business_id, call_id: r.call_id || null,
  error: err ? String(err).slice(0, 400) : null } }];
""")
    rec = w.pg("Record request", r"""
WITH r AS (SELECT $1::jsonb AS j),
u AS (
  UPDATE va.outbound_jobs oj SET
    gsm_call_id = COALESCE(NULLIF(r.j->>'call_id', ''), oj.gsm_call_id),
    status = CASE WHEN r.j->>'error' IS NULL THEN oj.status
                  WHEN oj.attempts < oj.max_attempts THEN 'pending' ELSE 'failed' END,
    scheduled_at = CASE WHEN r.j->>'error' IS NULL THEN oj.scheduled_at ELSE now() + interval '10 minutes' END,
    last_error = r.j->>'error'
  FROM r WHERE oj.id::text = r.j->>'job_id' AND oj.business_id::text = r.j->>'business_id'
  RETURNING oj.status)
SELECT (SELECT status FROM u) AS status
""")
    skip = w.pg("Skip or defer", r"""
WITH r AS (SELECT $1::jsonb AS j),
u AS (
  UPDATE va.outbound_jobs oj SET
    status = CASE WHEN r.j->>'action' = 'defer' THEN 'pending' ELSE 'skipped' END,
    attempts = CASE WHEN r.j->>'action' = 'defer' THEN GREATEST(oj.attempts - 1, 0) ELSE oj.attempts END,
    scheduled_at = CASE WHEN r.j->>'action' = 'defer' THEN now() + interval '30 minutes' ELSE oj.scheduled_at END,
    last_error = r.j->>'reason'
  FROM r WHERE oj.id::text = r.j->'job'->>'job_id' AND oj.business_id::text = r.j->'job'->>'business_id'
  RETURNING oj.status)
SELECT (SELECT status FROM u) AS status
""", pos=(w._x, 520))
    w.chain(trig, claim, rules, sw, http, res, rec)
    w.link(sw, skip, 1)
    return w


# ================================================================== 6. notifications
def notifications() -> Wf:
    w = Wf("vaWfNotify000001", "VA Notifications")
    trig = w.add("Every minute", "n8n-nodes-base.scheduleTrigger", 1.2,
                 {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}})
    pend = w.pg("Pending notifications", r"""
SELECT COALESCE(jsonb_agg(jsonb_build_object('id', n.id, 'kind', n.kind, 'payload', n.payload,
                                             'business', b.name, 'url', i.config->>'url', 'created_at', n.created_at)),
                '[]'::jsonb) AS items
FROM (SELECT * FROM va.notifications WHERE NOT delivered ORDER BY id LIMIT 20) n
JOIN va.businesses b ON b.id = n.business_id
JOIN va.business_integrations i ON i.business_id = n.business_id AND i.kind = 'notify_webhook' AND i.active
WHERE (i.config->>'url') ~ '^https://'
""", params_expr="={{ [] }}")
    split = w.code("One per notification", r"""
return ($input.first().json.items || []).map(n => ({ json: n }));
""")
    send = w.add("Send", "n8n-nodes-base.httpRequest", 4.2,
                 {"method": "POST", "url": "={{ $json.url }}", "sendBody": True, "specifyBody": "json",
                  "jsonBody": "={{ JSON.stringify({ text: `[${$json.business}] ${$json.kind}: ${JSON.stringify($json.payload)}`, kind: $json.kind, payload: $json.payload, created_at: $json.created_at }) }}",
                  "options": {"timeout": 10000}},
                 onError="continueRegularOutput")
    collect = w.code("Delivered ids", r"""
const sent = $('One per notification').all();
const results = $input.all();
const ids = sent.filter((n, i) => results[i] && !results[i].json.error).map(n => n.json.id);
return ids.length ? [{ json: { ids } }] : [];
""")
    mark = w.pg("Mark delivered", r"""
WITH r AS (SELECT $1::jsonb AS j),
u AS (UPDATE va.notifications SET delivered = true
      WHERE id IN (SELECT (x)::bigint FROM r, jsonb_array_elements_text(r.j->'ids') AS x) RETURNING 1)
SELECT (SELECT count(*) FROM u) AS delivered
""")
    w.chain(trig, pend, split, send, collect, mark)
    return w


# ================================================================== 7. error handler
def errors() -> Wf:
    w = Wf(ERROR_WF_ID, "VA Error Handler", error_wf=False)
    trig = w.add("On workflow error", "n8n-nodes-base.errorTrigger", 1, {})
    log = w.pg("Log error", r"""
WITH r AS (SELECT $1::jsonb AS j)
INSERT INTO va.workflow_errors (workflow_name, node_name, message, execution_id)
SELECT left(r.j->>'workflow', 200), left(r.j->>'node', 200), left(r.j->>'message', 2000), left(r.j->>'execution', 100) FROM r
RETURNING id
""", "={{ [ JSON.stringify({ workflow: ($json.workflow || {}).name, node: ($json.execution || {}).lastNodeExecuted, message: (($json.execution || {}).error || {}).message, execution: ($json.execution || {}).id }) ] }}")
    w.chain(trig, log)
    return w


ALL = [session_start, tool_workflow, session_end, call_event, outbound, notifications, errors]


def build() -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    paths = []
    for make in ALL:
        wf = make()
        p = OUT / (wf.name.lower().replace(" ", "-") + ".json")
        p.write_text(json.dumps(wf.json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths.append(p)
    return paths


if __name__ == "__main__":
    for p in build():
        print("wrote", p.name)
