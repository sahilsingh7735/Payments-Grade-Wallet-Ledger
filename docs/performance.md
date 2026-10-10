# Performance notes

The ledger is append-only, so nightly reconciliation intentionally scans the ledger for global balance, per-transfer balance, stored-balance, and overdraft checks. These checks need a consistent full snapshot and are suitable for a scheduled batch job.

The statement query uses `ledger_statement_idx (account_id, created_at DESC, id DESC)`, which supports the wallet filter and newest-first ordering. `ledger_transfer_idx (transfer_id)` supports transfer detail and per-transfer reconciliation. `transfers_created_by_time_idx` supports velocity and rolling-average risk rules. `transfers_status_idx` supports the held-risk queue.

Offset pagination must walk and discard the first 100,000 matching rows, so latency grows with the offset. The current API implements the required `LIMIT/OFFSET` contract. A production extension should add a cursor containing `(created_at, id)` and query:

```sql
WHERE account_id = $1
  AND (created_at, id) < ($2, $3)
ORDER BY created_at DESC, id DESC
LIMIT $4;
```

For a production run, capture `EXPLAIN (ANALYZE, BUFFERS)` for every reconciliation check and for statement offset 100000 against a 1-million-entry fixture. The nightly full scans trade batch latency for simpler, auditable consistency checks; the indexes keep interactive wallet and fraud queries bounded.
