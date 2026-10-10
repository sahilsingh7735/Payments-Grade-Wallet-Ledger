from psycopg.errors import LockNotAvailable

from .db import pool
from .llm import MockRiskExplainer


def reconcile():
    """Run all consistency checks against one repeatable-read snapshot."""
    with pool.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        try:
            conn.execute("SELECT 1 FROM job_locks WHERE job_name='reconciliation' FOR UPDATE NOWAIT")
        except LockNotAvailable:
            return {"status": "skipped", "reason": "reconciliation already running"}

        run = conn.execute(
            "INSERT INTO reconciliation_runs (status) VALUES ('running') RETURNING id"
        ).fetchone()
        run_id = run["id"]
        issues = []

        total = conn.execute(
            "SELECT COALESCE(SUM(amount) FILTER (WHERE direction='debit'), 0) AS debits, "
            "COALESCE(SUM(amount) FILTER (WHERE direction='credit'), 0) AS credits "
            "FROM ledger_entries"
        ).fetchone()
        if total["debits"] != total["credits"]:
            issues.append(("global_balance", None, total["debits"], total["credits"]))

        for row in conn.execute(
            "SELECT transfer_id, COALESCE(SUM(amount) FILTER (WHERE direction='debit'), 0) AS debits, "
            "COALESCE(SUM(amount) FILTER (WHERE direction='credit'), 0) AS credits "
            "FROM ledger_entries GROUP BY transfer_id "
            "HAVING COALESCE(SUM(amount) FILTER (WHERE direction='debit'), 0) <> "
            "COALESCE(SUM(amount) FILTER (WHERE direction='credit'), 0)"
        ):
            issues.append(("transfer_balance", row["transfer_id"], row["debits"], row["credits"]))

        for row in conn.execute(
            "SELECT a.id, a.balance, COALESCE(SUM(CASE WHEN l.direction='credit' THEN l.amount ELSE -l.amount END), 0) AS computed "
            "FROM accounts a LEFT JOIN ledger_entries l ON l.account_id=a.id "
            "GROUP BY a.id, a.balance HAVING a.balance <> COALESCE(SUM(CASE WHEN l.direction='credit' THEN l.amount ELSE -l.amount END), 0)"
        ):
            issues.append(("stored_balance", row["id"], row["computed"], row["balance"]))

        for row in conn.execute(
            "SELECT a.id, COALESCE(SUM(CASE WHEN l.direction='credit' THEN l.amount ELSE -l.amount END), 0) AS computed "
            "FROM accounts a JOIN users u ON u.id=a.user_id LEFT JOIN ledger_entries l ON l.account_id=a.id "
            "GROUP BY a.id HAVING COALESCE(SUM(CASE WHEN l.direction='credit' THEN l.amount ELSE -l.amount END), 0) < 0"
        ):
            issues.append(("overdraft", row["id"], 0, row["computed"]))

        for row in conn.execute(
            "SELECT t.id FROM transfers t LEFT JOIN ledger_entries l ON l.transfer_id=t.id "
            "WHERE t.status <> 'held' GROUP BY t.id HAVING COUNT(l.id)=0"
        ):
            issues.append(("orphan_transfer", row["id"], 1, 0))

        explainer = MockRiskExplainer()
        for flag in conn.execute(
            "SELECT id, transfer_id, rule_name FROM risk_flags "
            "WHERE created_at >= current_date"
        ):
            recent = conn.execute(
                "SELECT t.id, t.type, t.status, t.amount, t.created_at "
                "FROM transfers t JOIN ledger_entries e ON e.transfer_id=t.id "
                "WHERE e.account_id=(SELECT source_account_id FROM transfers WHERE id=%s) "
                "ORDER BY t.created_at DESC, t.id DESC LIMIT 10",
                (flag["transfer_id"],),
            ).fetchall()
            explanation = explainer.explain(flag["rule_name"], recent)
            conn.execute(
                "UPDATE risk_flags SET explanation=%s, label=%s WHERE id=%s",
                (explanation["explanation"], explanation["label"], flag["id"]),
            )

        for check_name, reference_id, expected, actual in issues:
            conn.execute(
                "INSERT INTO reconciliation_issues (run_id, check_name, reference_id, expected, actual) "
                "VALUES (%s,%s,%s,%s,%s)",
                (run_id, check_name, reference_id, expected, actual),
            )
        status = "passed" if not issues else "failed"
        conn.execute(
            "UPDATE reconciliation_runs SET status=%s, finished_at=now(), problem_count=%s WHERE id=%s",
            (status, len(issues), run_id),
        )
        return {"id": run_id, "status": status, "problem_count": len(issues)}
