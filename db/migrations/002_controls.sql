ALTER TABLE transfers ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE transfers ADD COLUMN IF NOT EXISTS source_account_id BIGINT REFERENCES accounts(id);
ALTER TABLE transfers ADD COLUMN IF NOT EXISTS target_account_id BIGINT REFERENCES accounts(id);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed', 'skipped')),
  problem_count INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reconciliation_issues (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id BIGINT NOT NULL REFERENCES reconciliation_runs(id),
  check_name TEXT NOT NULL,
  reference_id BIGINT,
  expected BIGINT,
  actual BIGINT,
  detail JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS reconciliation_issues_run_idx
  ON reconciliation_issues (run_id);

CREATE TABLE IF NOT EXISTS job_locks (
  job_name TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO job_locks (job_name) VALUES ('reconciliation') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS risk_rules (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  threshold BIGINT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('hold', 'flag')),
  enabled BOOLEAN NOT NULL DEFAULT TRUE
);
INSERT INTO risk_rules (name, threshold, action) VALUES
  ('velocity_10m', 5, 'hold'),
  ('amount_spike_multiplier', 5, 'hold'),
  ('new_payee_amount', 1000000, 'flag')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS risk_flags (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  transfer_id BIGINT NOT NULL REFERENCES transfers(id),
  rule_name TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('hold', 'flag')),
  explanation TEXT,
  label TEXT CHECK (label IN ('likely_ok', 'review', 'likely_fraud')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (transfer_id, rule_name)
);
CREATE INDEX IF NOT EXISTS risk_flags_queue_idx
  ON risk_flags (action, created_at DESC);
CREATE INDEX IF NOT EXISTS transfers_created_by_time_idx
  ON transfers (created_by, created_at DESC);
CREATE INDEX IF NOT EXISTS transfers_status_idx
  ON transfers (status, created_at DESC);
