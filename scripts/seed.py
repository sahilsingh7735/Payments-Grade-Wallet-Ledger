import os

import psycopg


DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet"
)


with psycopg.connect(DATABASE_URL) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (name, email, phone) "
            "SELECT 'Seed User ' || n, 'seed' || n || '@example.test', '+919000' || lpad(n::text, 6, '0') "
            "FROM generate_series(1, 1000) AS n "
            "ON CONFLICT (email) DO NOTHING"
        )
        cur.execute(
            "INSERT INTO accounts (user_id, name) "
            "SELECT u.id, 'wallet-' || w "
            "FROM users u CROSS JOIN generate_series(1, 2) AS w "
            "WHERE u.email LIKE 'seed%@example.test' "
            "ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "DO $$ DECLARE wallet_ids BIGINT[]; target BIGINT; transfer_id BIGINT; i INT; "
            "BEGIN "
            "SELECT array_agg(id ORDER BY id) INTO wallet_ids FROM accounts WHERE user_id IS NOT NULL; "
            "FOR i IN 1..100000 LOOP "
            "target := wallet_ids[(i - 1) % array_length(wallet_ids, 1) + 1]; "
            "INSERT INTO transfers (type, status, amount, created_by) VALUES ('topup', 'completed', 100, 'seed') RETURNING id INTO transfer_id; "
            "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) VALUES (transfer_id, 1, 'debit', 100), (transfer_id, target, 'credit', 100); "
            "UPDATE accounts SET balance = balance + 100 WHERE id = target; "
            "END LOOP; END $$"
        )
    conn.commit()

print("Seeded 1,000 users, 2,000 wallets, and 100,000 transfers.")
