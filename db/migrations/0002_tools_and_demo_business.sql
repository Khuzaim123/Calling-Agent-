-- 0002: agent tool catalogue + the demo business (Karachi Pizza House) with a real menu.
-- Idempotent: safe to re-run.

INSERT INTO va.tool_definitions (name, description, parameters) VALUES
('get_menu', 'List the items the business actually sells, with SKU and price in PKR. Call this before offering, recommending or ordering anything. Never mention items that are not returned here.',
 '{"type":"object","properties":{"category":{"type":"string","description":"Optional category filter, e.g. pizza, sides, drinks"}}}'),
('lookup_order', 'Get the status, items and ETA of an existing order by its order number.',
 '{"type":"object","properties":{"order_number":{"type":"string","description":"Order number, digits only"}},"required":["order_number"]}'),
('create_order', 'Place a NEW delivery order. Only after the caller has agreed to the items, the total and the delivery address that you read back to them.',
 '{"type":"object","properties":{"items":{"type":"array","minItems":1,"items":{"type":"object","properties":{"sku":{"type":"string","description":"SKU from get_menu"},"quantity":{"type":"integer","minimum":1,"maximum":20}},"required":["sku","quantity"]}},"delivery_address":{"type":"string"},"customer_name":{"type":"string"},"customer_phone":{"type":"string","description":"Caller phone number if not already known"}},"required":["items","delivery_address"]}'),
('confirm_order', 'Confirm or cancel an existing pending order.',
 '{"type":"object","properties":{"order_number":{"type":"string"},"decision":{"type":"string","enum":["confirm","cancel"]}},"required":["order_number","decision"]}'),
('check_availability', 'Check whether a table is free for a party at a date and time.',
 '{"type":"object","properties":{"date":{"type":"string","description":"YYYY-MM-DD"},"time":{"type":"string","description":"HH:MM, 24h"},"party_size":{"type":"integer","minimum":1,"maximum":50}},"required":["date","time","party_size"]}'),
('create_booking', 'Reserve a table after availability was confirmed and the caller agreed.',
 '{"type":"object","properties":{"date":{"type":"string"},"time":{"type":"string"},"party_size":{"type":"integer","minimum":1,"maximum":50},"name":{"type":"string"},"customer_phone":{"type":"string"}},"required":["date","time","party_size","name"]}'),
('remember_customer_preference', 'Save something the caller asked you to remember for next time (e.g. "extra spicy", "deliver to the back gate").',
 '{"type":"object","properties":{"note":{"type":"string"}},"required":["note"]}'),
('request_human_handoff', 'Hand the call to a human when the caller asks for one, is upset, or the request is outside your instructions.',
 '{"type":"object","properties":{"reason":{"type":"string"}},"required":["reason"]}')
ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description, parameters = EXCLUDED.parameters;

INSERT INTO va.businesses (slug, name) VALUES ('karachi-pizza', 'Karachi Pizza House')
ON CONFLICT (slug) DO NOTHING;

WITH b AS (SELECT id FROM va.businesses WHERE slug = 'karachi-pizza')
INSERT INTO va.business_phone_numbers (business_id, channel, line_name, is_primary)
SELECT b.id, ch, ln, prim FROM b,
  (VALUES ('browser_test', 'default', false), ('gsm', 'redmi9c-ufone', true)) AS v(ch, ln, prim)
ON CONFLICT (channel, line_name) DO NOTHING;

WITH b AS (SELECT id FROM va.businesses WHERE slug = 'karachi-pizza')
INSERT INTO va.agent_configs (business_id, language, instructions, greeting, greeting_outbound, enabled_tools,
                              delivery_eta_minutes, booking_capacity_per_slot)
SELECT b.id, 'auto',
$$You answer calls for Karachi Pizza House, a pizza delivery and dine-in restaurant in Karachi.
- The menu comes ONLY from the get_menu tool. Never offer, confirm or invent items that get_menu does not return (we do not sell burgers).
- To place an order: get the items, read back items, total price and delivery address, get a clear yes, then call create_order. Tell the caller the order number it returns.
- Delivery is only within Karachi. Opening hours are 12:00 to 23:30 every day. Never offer discounts.
- Tables can be booked with check_availability then create_booking.$$,
'{"en": "Hello, this is Karachi Pizza House. This call may be recorded for quality. How can I help you?",
  "ur": "السلام علیکم، کراچی پیزا ہاؤس سے بات ہو رہی ہے۔ معیار کے لیے یہ کال ریکارڈ کی جا سکتی ہے۔ میں آپ کی کیا مدد کروں؟"}',
'{"en": "Hello, this is the automated assistant from Karachi Pizza House calling about your order. This call may be recorded. Is now a good time?",
  "ur": "السلام علیکم، میں کراچی پیزا ہاؤس کی خودکار اسسٹنٹ ہوں اور آپ کے آرڈر کے سلسلے میں کال کر رہی ہوں۔ یہ کال ریکارڈ کی جا سکتی ہے۔ کیا ابھی بات ہو سکتی ہے؟"}',
ARRAY['get_menu', 'lookup_order', 'create_order', 'confirm_order', 'check_availability', 'create_booking',
      'remember_customer_preference', 'request_human_handoff'],
40, 20
FROM b
ON CONFLICT (business_id) DO NOTHING;

WITH b AS (SELECT id FROM va.businesses WHERE slug = 'karachi-pizza')
INSERT INTO va.knowledge_items (business_id, title, content)
SELECT b.id, t, c FROM b, (VALUES
  ('Delivery time', 'Delivery usually takes 35 to 45 minutes.'),
  ('Payment', 'Payment options: cash on delivery, JazzCash, Easypaisa, card on delivery.'),
  ('Sizes', 'Small serves 1, medium serves 2, large serves 3 to 4 people.'),
  ('Delivery area', 'We deliver only within Karachi.')) AS v(t, c)
ON CONFLICT (business_id, title) DO NOTHING;

WITH b AS (SELECT id FROM va.businesses WHERE slug = 'karachi-pizza')
INSERT INTO va.products (business_id, sku, name_en, name_ur, category, price_pkr, sort_order)
SELECT b.id, s, ne, nu, cat, p, o FROM b, (VALUES
  ('PZ-TIKKA-M',  'Chicken Tikka Pizza (medium)',  'چکن تکہ پیزا (میڈیم)',   'pizza',  1450, 10),
  ('PZ-TIKKA-L',  'Chicken Tikka Pizza (large)',   'چکن تکہ پیزا (لارج)',    'pizza',  1950, 11),
  ('PZ-FAJITA-M', 'Chicken Fajita Pizza (medium)', 'چکن فاجیٹا پیزا (میڈیم)', 'pizza',  1450, 20),
  ('PZ-FAJITA-L', 'Chicken Fajita Pizza (large)',  'چکن فاجیٹا پیزا (لارج)',  'pizza',  1950, 21),
  ('PZ-VEG-M',    'Veggie Pizza (medium)',         'ویجی پیزا (میڈیم)',       'pizza',  1250, 30),
  ('PZ-VEG-L',    'Veggie Pizza (large)',          'ویجی پیزا (لارج)',        'pizza',  1750, 31),
  ('SD-GARLIC',   'Garlic Bread',                  'گارلک بریڈ',              'sides',   450, 50),
  ('SD-WINGS-6',  'Chicken Wings (6 pieces)',      'چکن ونگز (چھ پیس)',       'sides',   750, 51),
  ('DR-COLA-500', 'Cola 500 ml',                   'کولا (500 ملی لیٹر)',     'drinks',  150, 70),
  ('DR-COLA-1500','Cola 1.5 litre',                'کولا (ڈیڑھ لیٹر)',        'drinks',  280, 71)) AS v(s, ne, nu, cat, p, o)
ON CONFLICT (business_id, sku) DO NOTHING;

-- a demo customer and an order to look up ("order 4821")
WITH b AS (SELECT id FROM va.businesses WHERE slug = 'karachi-pizza')
INSERT INTO va.customers (business_id, phone_e164, name, preferred_language)
SELECT b.id, '+923001234567', 'Ahmed', 'ur' FROM b
ON CONFLICT (business_id, phone_e164) DO NOTHING;

WITH b AS (SELECT id FROM va.businesses WHERE slug = 'karachi-pizza'),
     c AS (SELECT c.id, c.business_id FROM va.customers c JOIN b ON c.business_id = b.id WHERE c.phone_e164 = '+923001234567'),
     o AS (
       INSERT INTO va.orders (business_id, order_number, customer_id, status, delivery_address, total_pkr, eta_minutes)
       SELECT c.business_id, '4821', c.id, 'out_for_delivery', 'House 9, MDA Chowk, Karachi', 1950, 15 FROM c
       ON CONFLICT (business_id, order_number) DO NOTHING
       RETURNING id, business_id)
INSERT INTO va.order_items (business_id, order_id, product_id, quantity, unit_price_pkr)
SELECT o.business_id, o.id, p.id, 1, p.price_pkr
FROM o JOIN va.products p ON p.business_id = o.business_id AND p.sku = 'PZ-FAJITA-L';
