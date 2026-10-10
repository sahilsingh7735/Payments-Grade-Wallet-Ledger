# Isolation lab

Run two `psql` sessions against the Compose database:

```bash
psql postgresql://wallet:wallet@localhost:5432/wallet
```

Use an existing account ID in place of `:account_id`.

## Read Committed

Session A:

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT balance FROM accounts WHERE id = :account_id;
```

Session B:

```sql
BEGIN;
UPDATE accounts SET balance = balance + 100 WHERE id = :account_id;
COMMIT;
```

Session A runs the select again and sees the committed new value. Each statement gets a fresh snapshot.

## Repeatable Read

Session A starts `BEGIN ISOLATION LEVEL REPEATABLE READ;` and runs the same first select. After Session B commits its update, Session A repeats the select and sees the original value because the transaction snapshot is fixed.

## Serializable

Start two serializable transactions. Have both read the same balance and then update based on that value. After one commits, PostgreSQL aborts the other with:

```text
ERROR: could not serialize access due to concurrent update
```

The application must retry the complete transaction after this error.

## Repeatable Read with a join

Use:

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT a.id, u.id, a.balance
FROM accounts a JOIN users u ON u.id = a.user_id
WHERE a.id = :account_id;
```

The join uses the same transaction snapshot. Changes committed by another session remain invisible until Session A commits and starts a new transaction.

## Transfer choice

Transfers use PostgreSQL Read Committed with `SELECT ... FOR UPDATE` on every account row in ascending ID order. The balance is re-read while locked, so concurrent spends serialize on the source account without the retry cost of Serializable for every request. Serializable remains appropriate for workflows that read several independent predicates.
