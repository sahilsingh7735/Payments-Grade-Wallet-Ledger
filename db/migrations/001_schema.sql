CREATE TABLE users (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  phone TEXT NOT NULL UNIQUE,
  kyc_status TEXT NOT NULL DEFAULT 'pending' CHECK (kyc_status IN ('pending','verified','rejected')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- user wallets AND system accounts live here (system accounts have user_id NULL)
CREATE TABLE accounts (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id BIGINT REFERENCES users(id),
  name TEXT NOT NULL,
  currency CHAR(3) NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
  balance BIGINT NOT NULL DEFAULT 0,           -- whole paise
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT no_negative_user_balance CHECK (user_id IS NULL OR balance >= 0)
);
CREATE UNIQUE INDEX accounts_user_name_uq ON accounts(user_id, name) WHERE user_id IS NOT NULL;

INSERT INTO accounts (name) VALUES ('cash_in'), ('cash_out'), ('fee_revenue');  -- ids 1, 2, 3

CREATE TABLE transfers (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('topup','peer','withdrawal','fee','reversal')),
  status TEXT NOT NULL CHECK (status IN ('pending','completed','held','rejected')),
  amount BIGINT NOT NULL CHECK (amount > 0),
  reverses_transfer_id BIGINT UNIQUE REFERENCES transfers(id),
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ledger_entries (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  transfer_id BIGINT NOT NULL REFERENCES transfers(id),
  account_id BIGINT NOT NULL REFERENCES accounts(id),
  direction TEXT NOT NULL CHECK (direction IN ('debit','credit')),
  amount BIGINT NOT NULL CHECK (amount > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ledger_statement_idx ON ledger_entries (account_id, created_at DESC, id DESC);
CREATE INDEX ledger_transfer_idx ON ledger_entries (transfer_id);

-- ledger entries can never be updated or deleted
CREATE FUNCTION forbid_change() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'ledger_entries is append-only'; END $$ LANGUAGE plpgsql;
CREATE TRIGGER ledger_append_only BEFORE UPDATE OR DELETE ON ledger_entries
  FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- every transfer's debits must equal its credits (checked at commit)
CREATE FUNCTION check_transfer_balanced() RETURNS trigger AS $$
DECLARE d BIGINT; c BIGINT;
BEGIN
  SELECT COALESCE(SUM(amount) FILTER (WHERE direction='debit'),0),
         COALESCE(SUM(amount) FILTER (WHERE direction='credit'),0)
    INTO d, c FROM ledger_entries WHERE transfer_id = NEW.transfer_id;
  IF d <> c THEN RAISE EXCEPTION 'transfer % is not balanced', NEW.transfer_id; END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER ledger_balanced AFTER INSERT ON ledger_entries
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_transfer_balanced();

CREATE TABLE idempotency_keys (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  key UUID NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('in_progress','completed')),
  response_code INT,
  response_body JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, key)
);

CREATE TABLE transfer_status_log (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  transfer_id BIGINT NOT NULL REFERENCES transfers(id),
  old_status TEXT,
  new_status TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
