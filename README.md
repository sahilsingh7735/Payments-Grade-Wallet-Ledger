# Payments-Grade Wallet Ledger

A FastAPI and PostgreSQL wallet ledger using whole paise, double-entry accounting, explicit transactions, row locks, idempotency keys, reconciliation, reversals, and database-backed risk rules.

## Run

```bash
docker compose up --build
```

Open http://localhost:8000/docs for the API. Run tests in another terminal:

```bash
python -m pip install -r requirements.txt
pytest -q
```

Stop with `docker compose down`. Use `docker compose down -v` only when you want to delete the database and rerun all migrations.

## Design decisions

Balances are stored and maintained transactionally for fast reads, while the immutable ledger remains the source used by reconciliation. This costs one account update per money movement and requires a nightly consistency check, but avoids scanning the entire ledger for every balance request.

User wallets and system accounts share `accounts` because transfers and ledger entries then use one foreign-key target and one locking algorithm. A nullable `user_id` identifies system accounts; application authorization only exposes user-owned wallets.

Money uses positive `BIGINT` paise plus a direction column. Ledger rows cannot be updated or deleted. Corrections use a new reversal transfer.

The transfer path uses Read Committed plus ordered row locks. The source balance is checked after the lock is acquired. This gives high throughput for independent wallets and prevents overdrafts without serializing unrelated accounts.

Idempotency hashes the canonical request body and stores the response. A repeated key with the same body replays the saved response; a different body returns 422.

Reconciliation runs all checks from one Repeatable Read snapshot and uses `job_locks ... FOR UPDATE NOWAIT`, so overlapping runs exit without changing data. It reports problems rather than repairing them.

The fraud rules are database rows and are evaluated inside the transfer transaction. The velocity rule is intentionally tested in a separate fraud fixture from the concurrency stampede: the stampede acceptance test measures row-lock correctness and expects 50 successful transfers, while the fraud fixture enables the configured five-transfers-in-ten-minutes hold rule.

## Acceptance coverage

The live suite covers the 100-request stampede, sequential and parallel idempotency, fraud hold and admin release, reversals, statement Q&A isolation, and prompt-injection safety. The offline parser and risk-explainer tests run without a network or API key. Run the complete suite with `source .venv/Scripts/activate && python -m pytest -q` while Compose is running; the current live result is 11 passing tests.

The remaining submission measurements are the reconciliation corruption fixture, seed-count verification for 1,000 users, 2,000 wallets, and 100,000 transfers, and recorded `EXPLAIN ANALYZE` output for the performance notes. The optional OpenAI-compatible parser is enabled only when `LLM_ENDPOINT` and `LLM_API_KEY` are configured; otherwise the deterministic parser keeps tests offline.
