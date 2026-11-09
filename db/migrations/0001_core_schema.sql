-- 0001 core schema for the voice agent (PostgreSQL 16).
-- Applied by ops/migrate.py as va_owner inside one transaction.
-- Tenant isolation: every table carries business_id, and every reference between tenant tables is a
-- composite foreign key (business_id, x_id) -> parent (business_id, id), so a row can never point at
-- another business's data even if a query is wrong. n8n connects as va_app (DML only, no DDL).

CREATE SCHEMA IF NOT EXISTS va;

CREATE OR REPLACE FUNCTION va.touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------- businesses and configuration
CREATE TABLE va.businesses (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  timezone    text NOT NULL DEFAULT 'Asia/Karachi',
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE va.business_phone_numbers (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  e164        text CHECK (e164 ~ '^\+[1-9][0-9]{6,14}$'),
  channel     text NOT NULL CHECK (channel IN ('gsm', 'sip', 'browser_test')),
  line_name   text CHECK (length(line_name) <= 64),          -- e.g. the gateway line "redmi9c-ufone"
  is_primary  boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (e164),
  UNIQUE (channel, line_name)
);

CREATE TABLE va.tool_definitions (                             -- global catalogue of agent tools
  name        text PRIMARY KEY CHECK (name ~ '^[a-z][a-z_]{2,63}$'),
  description text NOT NULL,
  parameters  jsonb NOT NULL CHECK (jsonb_typeof(parameters) = 'object')
);

CREATE TABLE va.agent_configs (
  business_id               uuid PRIMARY KEY REFERENCES va.businesses (id) ON DELETE CASCADE,
  language                  text NOT NULL DEFAULT 'auto' CHECK (language IN ('auto', 'en', 'ur')),
  instructions              text NOT NULL CHECK (length(instructions) BETWEEN 1 AND 8000),
  greeting                  jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(greeting) = 'object'),
  greeting_outbound         jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(greeting_outbound) = 'object'),
  max_call_seconds          integer NOT NULL DEFAULT 600 CHECK (max_call_seconds BETWEEN 30 AND 3600),
  recording_notice          boolean NOT NULL DEFAULT true,
  enabled_tools             text[] NOT NULL DEFAULT '{}',
  delivery_eta_minutes      integer NOT NULL DEFAULT 40 CHECK (delivery_eta_minutes BETWEEN 5 AND 300),
  booking_capacity_per_slot integer NOT NULL DEFAULT 20 CHECK (booking_capacity_per_slot > 0),
  booking_slot_minutes      integer NOT NULL DEFAULT 90 CHECK (booking_slot_minutes BETWEEN 15 AND 480),
  opening_time              time NOT NULL DEFAULT '12:00',
  closing_time              time NOT NULL DEFAULT '23:30',
  updated_at                timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE va.knowledge_items (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  title       text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  content     text NOT NULL CHECK (length(content) BETWEEN 1 AND 2000),
  active      boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, title)
);

CREATE TABLE va.products (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  sku         text NOT NULL CHECK (sku ~ '^[A-Z0-9][A-Z0-9-]{1,31}$'),
  name_en     text NOT NULL CHECK (length(name_en) BETWEEN 1 AND 120),
  name_ur     text CHECK (length(name_ur) <= 120),
  category    text NOT NULL CHECK (length(category) BETWEEN 1 AND 60),
  price_pkr   numeric(10, 2) NOT NULL CHECK (price_pkr >= 0),
  available   boolean NOT NULL DEFAULT true,
  sort_order  integer NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, sku),
  UNIQUE (business_id, id)
);

-- ---------------------------------------------------------------- customers and memory
CREATE TABLE va.customers (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id         uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  phone_e164          text NOT NULL CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  name                text CHECK (length(name) <= 120),
  preferred_language  text CHECK (preferred_language IN ('en', 'ur')),
  consent_outbound    boolean NOT NULL DEFAULT false,      -- may we call them? (outbound scheduler)
  consent_recorded_at timestamptz,
  opted_out_at        timestamptz,                         -- "don't call me again" always wins
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, phone_e164),
  UNIQUE (business_id, id)
);

CREATE TABLE va.customer_memory (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id    uuid NOT NULL,
  customer_id    uuid NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('preference', 'fact', 'summary')),
  content        text NOT NULL CHECK (length(content) BETWEEN 1 AND 1000),
  source_call_id text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (business_id, customer_id) REFERENCES va.customers (business_id, id) ON DELETE CASCADE
);
CREATE INDEX customer_memory_recent ON va.customer_memory (business_id, customer_id, created_at DESC);

-- ---------------------------------------------------------------- calls and conversations
CREATE TABLE va.calls (
  id             text PRIMARY KEY CHECK (length(id) BETWEEN 8 AND 64),   -- voice engine session_id
  business_id    uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  customer_id    uuid,
  channel        text NOT NULL CHECK (channel IN ('browser_test', 'gsm', 'sip')),
  direction      text NOT NULL DEFAULT 'inbound' CHECK (direction IN ('inbound', 'outbound')),
  remote_number  text CHECK (length(remote_number) <= 32),
  gsm_call_id    text UNIQUE,                                            -- telephony call id (gateway)
  status         text NOT NULL DEFAULT 'in_progress'
                 CHECK (status IN ('in_progress', 'ended', 'failed')),
  end_reason     text CHECK (length(end_reason) <= 120),
  turns          integer CHECK (turns >= 0),
  started_at     timestamptz NOT NULL DEFAULT now(),
  ended_at       timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, id),
  FOREIGN KEY (business_id, customer_id) REFERENCES va.customers (business_id, id) ON DELETE SET NULL (customer_id)
);
CREATE INDEX calls_recent ON va.calls (business_id, started_at DESC);

CREATE TABLE va.call_events (                     -- phone line status changes from the gateway
  id            bigserial PRIMARY KEY,
  business_id   uuid REFERENCES va.businesses (id) ON DELETE CASCADE,
  gsm_call_id   text NOT NULL,
  status        text NOT NULL CHECK (status IN ('requested', 'dialing', 'alerting', 'incoming', 'active',
                                                 'ended', 'failed', 'rejected')),
  reason        text CHECK (length(reason) <= 120),
  direction     text CHECK (direction IN ('inbound', 'outbound')),
  remote_number text CHECK (length(remote_number) <= 32),
  received_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (gsm_call_id, status)                    -- each state recorded once (duplicate event protection)
);

CREATE TABLE va.transcripts (
  business_id uuid NOT NULL,
  call_id     text NOT NULL,
  seq         integer NOT NULL CHECK (seq >= 0),
  role        text NOT NULL CHECK (role IN ('user', 'assistant')),
  text        text NOT NULL CHECK (length(text) <= 4000),
  language    text CHECK (language IN ('en', 'ur')),
  interrupted boolean NOT NULL DEFAULT false,
  spoken_at   timestamptz,
  PRIMARY KEY (call_id, seq),
  FOREIGN KEY (business_id, call_id) REFERENCES va.calls (business_id, id) ON DELETE CASCADE
);

CREATE TABLE va.conversation_summaries (
  call_id     text PRIMARY KEY,
  business_id uuid NOT NULL,
  summary     text NOT NULL CHECK (length(summary) <= 4000),
  outcome     text CHECK (length(outcome) <= 200),
  created_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (business_id, call_id) REFERENCES va.calls (business_id, id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------- orders and bookings
CREATE TABLE va.orders (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id         uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  order_number        text NOT NULL CHECK (order_number ~ '^[0-9]{4,10}$'),
  customer_id         uuid,
  status              text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'confirmed', 'preparing', 'out_for_delivery', 'delivered', 'cancelled')),
  delivery_address    text CHECK (length(delivery_address) <= 300),
  total_pkr           numeric(10, 2) NOT NULL DEFAULT 0 CHECK (total_pkr >= 0),
  eta_minutes         integer CHECK (eta_minutes BETWEEN 0 AND 600),
  notes               text CHECK (length(notes) <= 500),
  created_via_call_id text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, order_number),
  UNIQUE (business_id, id),
  FOREIGN KEY (business_id, customer_id) REFERENCES va.customers (business_id, id) ON DELETE SET NULL (customer_id)
);
CREATE INDEX orders_recent ON va.orders (business_id, created_at DESC);
CREATE INDEX orders_customer ON va.orders (business_id, customer_id, created_at DESC);

CREATE TABLE va.order_items (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id    uuid NOT NULL,
  order_id       uuid NOT NULL,
  product_id     uuid NOT NULL,
  quantity       integer NOT NULL CHECK (quantity BETWEEN 1 AND 50),
  unit_price_pkr numeric(10, 2) NOT NULL CHECK (unit_price_pkr >= 0),
  line_total_pkr numeric(12, 2) GENERATED ALWAYS AS (quantity * unit_price_pkr) STORED,
  FOREIGN KEY (business_id, order_id) REFERENCES va.orders (business_id, id) ON DELETE CASCADE,
  FOREIGN KEY (business_id, product_id) REFERENCES va.products (business_id, id)
);

CREATE TABLE va.bookings (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id         uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  booking_ref         text NOT NULL CHECK (booking_ref ~ '^BK-[0-9]{4,10}$'),
  customer_id         uuid,
  name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  starts_at           timestamptz NOT NULL,
  party_size          integer NOT NULL CHECK (party_size BETWEEN 1 AND 50),
  status              text NOT NULL DEFAULT 'confirmed'
                      CHECK (status IN ('confirmed', 'cancelled', 'completed', 'no_show')),
  notes               text CHECK (length(notes) <= 500),
  created_via_call_id text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, booking_ref),
  FOREIGN KEY (business_id, customer_id) REFERENCES va.customers (business_id, id) ON DELETE SET NULL (customer_id)
);
CREATE INDEX bookings_time ON va.bookings (business_id, starts_at) WHERE status = 'confirmed';

-- ---------------------------------------------------------------- operations
CREATE TABLE va.business_integrations (           -- settings only; secrets live in n8n credentials
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id     uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (length(kind) BETWEEN 1 AND 60),
  config          jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(config) = 'object'),
  credential_name text CHECK (length(credential_name) <= 120),   -- name of the n8n credential to use
  active          boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, kind)
);

CREATE TABLE va.handoff_requests (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  call_id     text,
  customer_id uuid,
  reason      text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'closed')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (business_id, customer_id) REFERENCES va.customers (business_id, id) ON DELETE SET NULL (customer_id)
);
CREATE INDEX handoff_open ON va.handoff_requests (business_id, created_at) WHERE status = 'open';

CREATE TABLE va.outbound_jobs (                   -- calls n8n should place through the phone line
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id  uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL,
  purpose      text NOT NULL CHECK (purpose IN ('order_confirmation', 'booking_reminder', 'follow_up', 'manual_test')),
  context      jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(context) = 'object'),
  scheduled_at timestamptz NOT NULL DEFAULT now(),
  status       text NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'requested', 'completed', 'failed', 'cancelled', 'skipped')),
  attempts     integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL DEFAULT 2 CHECK (max_attempts BETWEEN 1 AND 5),
  last_error   text CHECK (length(last_error) <= 500),
  gsm_call_id  text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (business_id, customer_id) REFERENCES va.customers (business_id, id) ON DELETE CASCADE
);
CREATE INDEX outbound_due ON va.outbound_jobs (scheduled_at) WHERE status = 'pending';

CREATE TABLE va.usage_records (
  id          bigserial PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES va.businesses (id) ON DELETE CASCADE,
  call_id     text,
  kind        text NOT NULL CHECK (kind IN ('call_seconds', 'turns', 'tool_calls')),
  quantity    numeric(14, 3) NOT NULL CHECK (quantity >= 0),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (call_id, kind)                          -- one record per call and kind (retry-safe)
);

CREATE TABLE va.notifications (
  id          bigserial PRIMARY KEY,
  business_id uuid REFERENCES va.businesses (id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (length(kind) BETWEEN 1 AND 60),
  payload     jsonb NOT NULL DEFAULT '{}',
  delivered   boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE va.idempotency_keys (                -- replayed webhooks return the stored response
  key        text PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 200),
  endpoint   text NOT NULL,
  response   jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE va.workflow_errors (
  id            bigserial PRIMARY KEY,
  workflow_name text,
  node_name     text,
  message       text,
  execution_id  text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- updated_at maintenance
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['businesses', 'products', 'customers', 'calls', 'orders', 'bookings',
                           'handoff_requests', 'outbound_jobs', 'agent_configs']
  LOOP
    EXECUTE format('CREATE TRIGGER %I_touch BEFORE UPDATE ON va.%I FOR EACH ROW EXECUTE FUNCTION va.touch_updated_at()', t, t);
  END LOOP;
END $$;

-- ---------------------------------------------------------------- privileges for n8n (va_app)
GRANT USAGE ON SCHEMA va TO va_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA va TO va_app;
GRANT DELETE ON va.idempotency_keys TO va_app;           -- cleanup of old keys only
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA va TO va_app;
REVOKE ALL ON va.tool_definitions FROM va_app;
GRANT SELECT ON va.tool_definitions TO va_app;
