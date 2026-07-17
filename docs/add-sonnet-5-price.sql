-- Add claude-sonnet-5 to the Langfuse (self-host) model + price tables.
--
-- WHY: as of 2026-07-16 the `models` table had claude-opus-4-8/-4-7 and
-- claude-sonnet-4-6, but NOT claude-sonnet-5 — so every sonnet-5 observation
-- priced at $0. Sonnet-5's standard rate equals the sonnet-4-6 tier ($3/$15).
--
-- SCHEMA NOTES (learned the hard way):
--   * prices.pricing_tier_id is NOT NULL and FKs to pricing_tiers.
--   * Every model owns a default tier with id "<model_id>_tier_default"
--     (name 'Standard', is_default=true). Price rows point at that tier.
--   * This script mirrors the CLEAN opus-4-8 structure: one default tier, full
--     usage-type key coverage (canonical + legacy aliases the hook/Langfuse may
--     emit). It does NOT copy sonnet-4-6's rows — those carry a stray second
--     tier ("7830bfc2…") with DOUBLE rates (a pre-existing bad insert; separate
--     cleanup).
--
-- Prices are per-token USD. Standard rate (deliberate: conservative vs the $100
-- credit; exact after the 2026-08-31 intro discount ends; matches
-- ~/llm_wiki/scripts/usage-report.py). Cache: read=0.1x, 5m write=1.25x, 1h=2x.
--
-- CAVEAT: Langfuse computes cost at INGESTION. This prices only NEW sonnet-5
-- traces; historical rows stay $0 unless backfilled in ClickHouse.
--
-- RUN:
--   docker exec -i langfuse-self-host-postgres-1 psql -U postgres -d postgres \
--     < ~/source/claude-code-langfuse-template/docs/add-sonnet-5-price.sql

BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM models WHERE model_name = 'claude-sonnet-5') THEN
    RAISE EXCEPTION 'claude-sonnet-5 already present in models; aborting';
  END IF;
END $$;

WITH m AS (
  INSERT INTO models
    (id, created_at, updated_at, project_id, model_name, match_pattern,
     start_date, input_price, output_price, total_price, unit, tokenizer_config, tokenizer_id)
  VALUES
    (gen_random_uuid()::text, now(), now(), NULL, 'claude-sonnet-5',
     '(?i)^(anthropic/)?(claude-sonnet-5|(eu\.|us\.|apac\.|global\.)?anthropic\.claude-sonnet-5-v1(:0)?|claude-sonnet-5)$',
     NULL, 0.000003, 0.000015, NULL, NULL, NULL, 'claude')
  RETURNING id
),
t AS (
  INSERT INTO pricing_tiers
    (id, created_at, updated_at, model_id, name, is_default, priority, conditions)
  SELECT m.id || '_tier_default', now(), now(), m.id, 'Standard', true, 0, '[]'::jsonb
  FROM m
  RETURNING id AS tier_id, model_id
)
INSERT INTO prices (id, created_at, updated_at, model_id, usage_type, price, project_id, pricing_tier_id)
SELECT gen_random_uuid()::text, now(), now(), t.model_id, u.usage_type, u.price, NULL, t.tier_id
FROM t CROSS JOIN (VALUES
  ('input',                       0.000003::numeric),   -- $3.00 / Mtok
  ('input_tokens',                0.000003::numeric),
  ('output',                      0.000015::numeric),   -- $15.00 / Mtok
  ('output_tokens',               0.000015::numeric),
  ('cache_read_input_tokens',     0.0000003::numeric),  -- $0.30 / Mtok (0.1x)
  ('input_cache_read',            0.0000003::numeric),
  ('input_cached_tokens',         0.0000003::numeric),
  ('cache_creation_input_tokens', 0.00000375::numeric), -- $3.75 / Mtok (1.25x, 5m default)
  ('input_cache_creation',        0.00000375::numeric),
  ('input_cache_creation_5m',     0.00000375::numeric),
  ('input_cache_creation_1h',     0.000006::numeric)    -- $6.00 / Mtok (2x)
) AS u(usage_type, price);

COMMIT;

-- Verify:
--   SELECT m.model_name, p.usage_type, p.price, p.pricing_tier_id
--   FROM models m JOIN prices p ON p.model_id = m.id
--   WHERE m.model_name = 'claude-sonnet-5' ORDER BY p.usage_type;
