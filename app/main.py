import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .db import pool
from .jobs import reconcile
from .llm import OpenAICompatibleStatementParser, RuleBasedStatementParser

CASH_IN, CASH_OUT, FEE = 1, 2, 3


def statement_parser():
    endpoint = os.getenv("LLM_ENDPOINT")
    api_key = os.getenv("LLM_API_KEY")
    if endpoint and api_key:
        return OpenAICompatibleStatementParser(
            endpoint=endpoint,
            api_key=api_key,
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        )
    return RuleBasedStatementParser()


class AppError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


@asynccontextmanager
async def lifespan(app):
    pool.open()
    pool.wait()
    yield
    pool.close()


app = FastAPI(lifespan=lifespan)


def err(status, code, message):
    return JSONResponse(status_code=status, content={
        "code": code, "message": message, "request_id": str(uuid.uuid4())})


@app.exception_handler(AppError)
async def app_error_handler(request, exc):
    return err(exc.status, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    return err(422, "VALIDATION_ERROR", str(exc.errors()))


def idem_key(idempotency_key: str | None = Header(default=None)):
    if not idempotency_key:
        raise AppError(400, "MISSING_IDEMPOTENCY_KEY", "Idempotency-Key header is required")
    try:
        return uuid.UUID(idempotency_key)
    except ValueError:
        raise AppError(400, "BAD_IDEMPOTENCY_KEY", "Idempotency-Key must be a UUID")


# ---------- models ----------
class UserIn(BaseModel):
    name: str
    email: str
    phone: str

class WalletIn(BaseModel):
    name: str = "main"

class TopupIn(BaseModel):
    wallet_id: int
    amount: int = Field(gt=0)

class TransferIn(BaseModel):
    from_wallet_id: int
    to_wallet_id: int
    amount: int = Field(gt=0)
    note: str | None = None

class WithdrawIn(BaseModel):
    wallet_id: int
    amount: int = Field(gt=0)

class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


# ---------- the money path (everything visible here) ----------
def move_money(cur, ttype, from_id, to_id, amount, actor, note=None):
    if from_id == to_id:
        raise AppError(422, "SAME_ACCOUNT", "Cannot transfer to the same account")

    # 1. lock BOTH rows, always in ascending id order -> no deadlocks
    cur.execute(
        "SELECT id, balance, user_id FROM accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
        ([from_id, to_id],))
    rows = {r["id"]: r for r in cur.fetchall()}
    if len(rows) != 2:
        raise AppError(404, "ACCOUNT_NOT_FOUND", "Account not found")

    # 2. re-check balance while holding the lock (system accounts may go negative)
    src = rows[from_id]
    if src["user_id"] is not None and src["balance"] < amount:
        raise AppError(422, "INSUFFICIENT_FUNDS", "Not enough balance")

    # 3. transfer + two ledger entries + stored balances
    cur.execute(
        "INSERT INTO transfers (type, status, amount, created_by, note, source_account_id, target_account_id) "
        "VALUES (%s,'completed',%s,%s,%s,%s,%s) RETURNING id",
        (ttype, amount, actor, note, from_id, to_id))
    tid = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) "
        "VALUES (%s,%s,'debit',%s), (%s,%s,'credit',%s)",
        (tid, from_id, amount, tid, to_id, amount))
    cur.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s", (amount, from_id))
    cur.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (amount, to_id))
    cur.execute(
        "INSERT INTO transfer_status_log (transfer_id, old_status, new_status, changed_by) "
        "VALUES (%s, NULL, 'completed', %s)", (tid, actor))
    return {"transfer_id": tid, "status": "completed", "amount": amount}
    # 4. commit happens when the caller's `with pool.connection()` block exits


def owner_of(wallet_id):
    with pool.connection() as conn:
        r = conn.execute("SELECT user_id FROM accounts WHERE id = %s", (wallet_id,)).fetchone()
    if not r or r["user_id"] is None:
        raise AppError(404, "WALLET_NOT_FOUND", "Wallet not found")
    return r["user_id"]


def risk_decision(cur, from_id, to_id, amount, actor):
    rules = {r["name"]: r for r in cur.execute(
        "SELECT name, threshold, action FROM risk_rules WHERE enabled"
    )}
    velocity = cur.execute(
        "SELECT COUNT(*) AS count FROM transfers WHERE created_by=%s AND type='peer' "
        "AND created_at >= now() - interval '10 minutes'",
        (actor,),
    ).fetchone()["count"]
    if "velocity_10m" in rules and velocity >= rules["velocity_10m"]["threshold"]:
        return rules["velocity_10m"]

    average = cur.execute(
        "SELECT AVG(amount) AS average FROM transfers WHERE created_by=%s AND type='peer' "
        "AND status='completed' AND created_at >= now() - interval '30 days'",
        (actor,),
    ).fetchone()["average"]
    multiplier = rules.get("amount_spike_multiplier")
    if multiplier and average and amount > average * multiplier["threshold"]:
        return multiplier

    payee = cur.execute(
        "SELECT 1 FROM transfers t "
        "JOIN ledger_entries source ON source.transfer_id=t.id AND source.account_id=%s AND source.direction='debit' "
        "JOIN ledger_entries target ON target.transfer_id=t.id AND target.account_id=%s AND target.direction='credit' "
        "WHERE t.type='peer' AND t.status='completed' LIMIT 1",
        (from_id, to_id),
    ).fetchone()
    new_payee = rules.get("new_payee_amount")
    if new_payee and not payee and amount > new_payee["threshold"]:
        return new_payee
    return None


def record_risk(cur, transfer_id, rule):
    cur.execute(
        "INSERT INTO risk_flags (transfer_id, rule_name, action) VALUES (%s,%s,%s) "
        "ON CONFLICT (transfer_id, rule_name) DO NOTHING",
        (transfer_id, rule["name"], rule["action"]),
    )


def transfer_with_risk(cur, body, user_id):
    actor = f"user:{user_id}"
    rule = risk_decision(cur, body.from_wallet_id, body.to_wallet_id, body.amount, actor)
    if rule and rule["action"] == "hold":
        cur.execute(
            "INSERT INTO transfers (type, status, amount, created_by, note, source_account_id, target_account_id) "
            "VALUES ('peer','held',%s,%s,%s,%s,%s) RETURNING id",
            (body.amount, actor, body.note, body.from_wallet_id, body.to_wallet_id),
        )
        transfer_id = cur.fetchone()["id"]
        record_risk(cur, transfer_id, rule)
        cur.execute(
            "INSERT INTO transfer_status_log (transfer_id, old_status, new_status, changed_by) VALUES (%s,NULL,'held',%s)",
            (transfer_id, actor),
        )
        return {"transfer_id": transfer_id, "status": "held", "amount": body.amount, "_response_code": 202}

    result = move_money(cur, "peer", body.from_wallet_id, body.to_wallet_id, body.amount, actor, body.note)
    if rule:
        record_risk(cur, result["transfer_id"], rule)
    return result


def reverse_money(cur, original_id, actor):
    original = cur.execute(
        "SELECT id, amount, status FROM transfers WHERE id=%s FOR UPDATE", (original_id,)
    ).fetchone()
    if not original:
        raise AppError(404, "TRANSFER_NOT_FOUND", "Transfer not found")
    if original["status"] != "completed":
        raise AppError(422, "TRANSFER_NOT_REVERSIBLE", "Only completed transfers can be reversed")
    if cur.execute("SELECT 1 FROM transfers WHERE reverses_transfer_id=%s", (original_id,)).fetchone():
        raise AppError(422, "ALREADY_REVERSED", "Transfer has already been reversed")
    entries = cur.execute(
        "SELECT account_id, direction, amount FROM ledger_entries WHERE transfer_id=%s ORDER BY account_id FOR UPDATE",
        (original_id,),
    ).fetchall()
    account_ids = sorted({entry["account_id"] for entry in entries})
    accounts = {row["id"]: row for row in cur.execute(
        "SELECT id, balance, user_id FROM accounts WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (account_ids,)
    )}
    for entry in entries:
        if entry["direction"] == "credit" and accounts[entry["account_id"]]["user_id"] is not None and accounts[entry["account_id"]]["balance"] < entry["amount"]:
            raise AppError(422, "INSUFFICIENT_FUNDS", "Reversal would overdraw a wallet")
    cur.execute(
        "INSERT INTO transfers (type,status,amount,reverses_transfer_id,created_by) VALUES ('reversal','completed',%s,%s,%s) RETURNING id",
        (original["amount"], original_id, actor),
    )
    reversal_id = cur.fetchone()["id"]
    for entry in entries:
        direction = "credit" if entry["direction"] == "debit" else "debit"
        cur.execute(
            "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) VALUES (%s,%s,%s,%s)",
            (reversal_id, entry["account_id"], direction, entry["amount"]),
        )
        delta = entry["amount"] if direction == "credit" else -entry["amount"]
        cur.execute("UPDATE accounts SET balance=balance+%s WHERE id=%s", (delta, entry["account_id"]))
    cur.execute(
        "INSERT INTO transfer_status_log (transfer_id, old_status, new_status, changed_by) VALUES (%s,NULL,'completed',%s)",
        (reversal_id, actor),
    )
    return {"transfer_id": reversal_id, "status": "completed", "amount": original["amount"]}


def run_idempotent(user_id, key, body, work):
    req_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with pool.connection() as conn:          # commits on success, rolls back on exception
        cur = conn.cursor()
        # insert the key FIRST; the unique (user_id, key) constraint picks one winner
        cur.execute(
            "INSERT INTO idempotency_keys (user_id, key, request_hash, status) "
            "VALUES (%s,%s,%s,'in_progress') ON CONFLICT (user_id, key) DO NOTHING RETURNING id",
            (user_id, key, req_hash))
        row = cur.fetchone()
        if row is None:                       # seen this key before
            cur.execute(
                "SELECT request_hash, status, response_code, response_body "
                "FROM idempotency_keys WHERE user_id=%s AND key=%s", (user_id, key))
            old = cur.fetchone()
            if old["request_hash"] != req_hash:
                raise AppError(422, "IDEMPOTENCY_KEY_REUSED", "Same key used with a different body")
            if old["status"] != "completed":
                raise AppError(409, "REQUEST_IN_PROGRESS", "Retry shortly")
            return JSONResponse(status_code=old["response_code"], content=old["response_body"])
        result = work(cur)
        response_code = result.pop("_response_code", 201)
        result = {name: result[name] for name in sorted(result)}
        cur.execute(
            "UPDATE idempotency_keys SET status='completed', response_code=%s, response_body=%s WHERE id=%s",
            (response_code, Jsonb(result), row["id"]))
    return JSONResponse(status_code=response_code, content=result)


def run_public_idempotent(key, body, work):
    req_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO public_idempotency_keys (key, request_hash, status) VALUES (%s,%s,'in_progress') "
            "ON CONFLICT (key) DO NOTHING RETURNING id",
            (key, req_hash),
        )
        row = cur.fetchone()
        if row is None:
            old = cur.execute(
                "SELECT request_hash, status, response_code, response_body FROM public_idempotency_keys WHERE key=%s",
                (key,),
            ).fetchone()
            if old["request_hash"] != req_hash:
                raise AppError(422, "IDEMPOTENCY_KEY_REUSED", "Same key used with a different body")
            if old["status"] != "completed":
                raise AppError(409, "REQUEST_IN_PROGRESS", "Retry shortly")
            return JSONResponse(status_code=old["response_code"], content=old["response_body"])
        result = work(cur)
        response_code = result.pop("_response_code", 201)
        result = {name: result[name] for name in sorted(result)}
        cur.execute(
            "UPDATE public_idempotency_keys SET status='completed', response_code=%s, response_body=%s WHERE id=%s",
            (response_code, Jsonb(result), row["id"]),
        )
    return JSONResponse(status_code=response_code, content=result)


# ---------- endpoints ----------
@app.post("/users", status_code=201)
def create_user(body: UserIn, key=Depends(idem_key)):
    def create(cur):
        return dict(cur.execute(
            "INSERT INTO users (name, email, phone) VALUES (%s,%s,%s) RETURNING id, name, email, phone, kyc_status",
            (body.name, body.email, body.phone),
        ).fetchone())

    return run_public_idempotent(key, body.model_dump(), create)


@app.post("/users/{user_id}/wallets", status_code=201)
def open_wallet(user_id: int, body: WalletIn, key=Depends(idem_key)):
    with pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=%s", (user_id,)).fetchone():
            raise AppError(404, "USER_NOT_FOUND", "User not found")

    def create(cur):
        return dict(cur.execute(
            "INSERT INTO accounts (user_id, name) VALUES (%s,%s) "
            "RETURNING id, user_id, name, currency, balance",
            (user_id, body.name),
        ).fetchone())

    return run_idempotent(user_id, key, body.model_dump(), create)


@app.get("/wallets/{wallet_id}/balance")
def balance(wallet_id: int):
    with pool.connection() as conn:
        r = conn.execute("SELECT id, balance FROM accounts WHERE id=%s", (wallet_id,)).fetchone()
    if not r:
        raise AppError(404, "WALLET_NOT_FOUND", "Wallet not found")
    return r


@app.get("/wallets/{wallet_id}/statement")
def statement(wallet_id: int, limit: int = 20, offset: int = 0):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, transfer_id, direction, amount, created_at FROM ledger_entries "
            "WHERE account_id=%s ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (wallet_id, limit, offset)).fetchall()
    return rows


@app.post("/transfers/topup")
def topup(body: TopupIn, key=Depends(idem_key)):
    uid = owner_of(body.wallet_id)
    return run_idempotent(uid, key, body.model_dump(),
                          lambda cur: move_money(cur, "topup", CASH_IN, body.wallet_id, body.amount, f"user:{uid}"))


@app.post("/transfers")
def transfer(body: TransferIn, key=Depends(idem_key)):
    uid = owner_of(body.from_wallet_id)
    return run_idempotent(uid, key, body.model_dump(),
                          lambda cur: transfer_with_risk(cur, body, uid))


@app.post("/transfers/withdraw")
def withdraw(body: WithdrawIn, key=Depends(idem_key)):
    uid = owner_of(body.wallet_id)
    return run_idempotent(uid, key, body.model_dump(),
                          lambda cur: move_money(cur, "withdrawal", body.wallet_id, CASH_OUT, body.amount, f"user:{uid}"))


@app.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int):
    with pool.connection() as conn:
        t = conn.execute("SELECT * FROM transfers WHERE id=%s", (transfer_id,)).fetchone()
        if not t:
            raise AppError(404, "TRANSFER_NOT_FOUND", "Transfer not found")
        entries = conn.execute("SELECT * FROM ledger_entries WHERE transfer_id=%s ORDER BY id", (transfer_id,)).fetchall()
        history = conn.execute("SELECT * FROM transfer_status_log WHERE transfer_id=%s ORDER BY id", (transfer_id,)).fetchall()
    return {"transfer": t, "entries": entries, "status_history": history}


@app.post("/transfers/{transfer_id}/reverse", status_code=201)
def reverse_transfer(transfer_id: int, key=Depends(idem_key)):
    with pool.connection() as conn:
        entry = conn.execute(
            "SELECT a.user_id FROM ledger_entries l JOIN accounts a ON a.id=l.account_id "
            "WHERE l.transfer_id=%s AND l.direction='debit' LIMIT 1", (transfer_id,)
        ).fetchone()
    if not entry or entry["user_id"] is None:
        raise AppError(404, "TRANSFER_NOT_FOUND", "Transfer not found")
    return run_idempotent(entry["user_id"], key, {"transfer_id": transfer_id},
                          lambda cur: reverse_money(cur, transfer_id, f"user:{entry['user_id']}"))


@app.post("/admin/reconciliation/run", status_code=202)
def run_reconciliation(key=Depends(idem_key)):
    return run_public_idempotent(
        key,
        {"operation": "reconciliation"},
        lambda cur: {**reconcile(), "_response_code": 202},
    )


@app.get("/admin/reconciliation/runs/{run_id}")
def reconciliation_report(run_id: int):
    with pool.connection() as conn:
        run = conn.execute("SELECT * FROM reconciliation_runs WHERE id=%s", (run_id,)).fetchone()
        if not run:
            raise AppError(404, "RUN_NOT_FOUND", "Reconciliation run not found")
        issues = conn.execute(
            "SELECT * FROM reconciliation_issues WHERE run_id=%s ORDER BY id", (run_id,)
        ).fetchall()
    return {"run": run, "issues": issues}


@app.get("/admin/risk/queue")
def risk_queue():
    with pool.connection() as conn:
        return conn.execute(
            "SELECT f.*, t.amount, t.status FROM risk_flags f JOIN transfers t ON t.id=f.transfer_id "
            "WHERE t.status='held' OR f.action='flag' ORDER BY f.created_at DESC"
        ).fetchall()


@app.post("/admin/risk/{transfer_id}/decision")
def risk_decision_endpoint(transfer_id: int, decision: str, key=Depends(idem_key)):
    if decision not in {"release", "reject"}:
        raise AppError(422, "BAD_DECISION", "Decision must be release or reject")
    with pool.connection() as conn:
        transfer_row = conn.execute(
            "SELECT * FROM transfers WHERE id=%s FOR UPDATE", (transfer_id,)
        ).fetchone()
        if not transfer_row:
            raise AppError(404, "TRANSFER_NOT_FOUND", "Transfer not found")
        if transfer_row["status"] != "held":
            raise AppError(422, "TRANSFER_NOT_HELD", "Transfer is not held")
        if decision == "reject":
            conn.execute("UPDATE transfers SET status='rejected' WHERE id=%s", (transfer_id,))
            new_status = "rejected"
        else:
            source = transfer_row["source_account_id"]
            target = transfer_row["target_account_id"]
            rows = conn.execute(
                "SELECT id, balance, user_id FROM accounts WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
                ([source, target],),
            ).fetchall()
            if len(rows) != 2:
                raise AppError(404, "ACCOUNT_NOT_FOUND", "Held transfer account not found")
            accounts = {row["id"]: row for row in rows}
            if accounts[source]["user_id"] is not None and accounts[source]["balance"] < transfer_row["amount"]:
                raise AppError(422, "INSUFFICIENT_FUNDS", "Not enough balance to release transfer")
            conn.execute(
                "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount) VALUES "
                "(%s,%s,'debit',%s),(%s,%s,'credit',%s)",
                (transfer_id, source, transfer_row["amount"], transfer_id, target, transfer_row["amount"]),
            )
            conn.execute("UPDATE accounts SET balance=balance-%s WHERE id=%s", (transfer_row["amount"], source))
            conn.execute("UPDATE accounts SET balance=balance+%s WHERE id=%s", (transfer_row["amount"], target))
            conn.execute("UPDATE transfers SET status='completed' WHERE id=%s", (transfer_id,))
            new_status = "completed"
        conn.execute(
            "INSERT INTO transfer_status_log (transfer_id, old_status, new_status, changed_by) VALUES (%s,'held',%s,'admin')",
            (transfer_id, new_status),
        )
    return {"transfer_id": transfer_id, "status": new_status}


def statement_query(user_id, parsed):
    start, end = parsed["from"], parsed["to"]
    params = [user_id, start, end]
    counterparty = parsed.get("counterparty")
    counterparty_clause = ""
    if counterparty:
        counterparty_clause = " AND recipient.name ILIKE %s"
        params.append(counterparty)
    with pool.connection() as conn:
        if parsed["type"] == 1:
            row = conn.execute(
                "SELECT COALESCE(SUM(t.amount),0) AS total, COALESCE(array_agg(t.id ORDER BY t.id), ARRAY[]::bigint[]) AS ids "
                "FROM transfers t JOIN ledger_entries source ON source.transfer_id=t.id AND source.direction='debit' "
                "JOIN accounts source_account ON source_account.id=source.account_id "
                "JOIN ledger_entries target ON target.transfer_id=t.id AND target.direction='credit' "
                "JOIN accounts recipient_account ON recipient_account.id=target.account_id "
                "JOIN users recipient ON recipient.id=recipient_account.user_id "
                "WHERE source_account.user_id=%s AND t.created_at >= %s AND t.created_at < %s AND t.type='peer'" + counterparty_clause,
                params,
            ).fetchone()
            return {"value": int(row["total"]), "transfer_ids": row["ids"]}
        if parsed["type"] == 2:
            row = conn.execute(
                "SELECT COALESCE(SUM(t.amount),0) AS total, COALESCE(array_agg(t.id ORDER BY t.id), ARRAY[]::bigint[]) AS ids "
                "FROM transfers t JOIN ledger_entries target ON target.transfer_id=t.id AND target.direction='credit' "
                "JOIN accounts target_account ON target_account.id=target.account_id "
                "WHERE target_account.user_id=%s AND t.created_at >= %s AND t.created_at < %s AND t.type='peer'", params[:3]
            ).fetchone()
            return {"value": int(row["total"]), "transfer_ids": row["ids"]}
        row = conn.execute(
            "SELECT t.id, t.amount FROM transfers t JOIN ledger_entries e ON e.transfer_id=t.id "
            "JOIN accounts a ON a.id=e.account_id WHERE a.user_id=%s AND t.created_at >= %s AND t.created_at < %s "
            "AND t.type='peer' ORDER BY t.amount DESC, t.id DESC", params[:3]
        ).fetchall()
    if parsed["type"] == 3:
        return {"value": int(row[0]["amount"]) if row else 0, "transfer_ids": [row[0]["id"]] if row else []}
    return {"value": len(row), "transfer_ids": [item["id"] for item in row]}


@app.post("/users/{user_id}/statement/ask")
def ask_statement(user_id: int, body: AskIn, key=Depends(idem_key)):
    with pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=%s", (user_id,)).fetchone():
            raise AppError(404, "USER_NOT_FOUND", "User not found")
    try:
        parsed = statement_parser().parse(body.question)
        if parsed.get("type") not in {1, 2, 3, 4}:
            raise ValueError("unsupported type")
        if not isinstance(parsed.get("from"), str) or not isinstance(parsed.get("to"), str):
            raise ValueError("invalid date range")
    except (ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        raise AppError(422, "UNSUPPORTED_QUESTION", "I can answer sent, received, largest, or count questions") from exc
    return run_idempotent(
        user_id,
        key,
        body.model_dump(),
        lambda cur: {"question": body.question, **statement_query(user_id, parsed)},
    )


# ---------- Milestone 2 Step 2: the BROKEN version (for your README comparison only) ----------
@app.post("/debug/naive-transfer")
def naive_transfer(body: TransferIn):
    with pool.connection() as conn:
        bal = conn.execute("SELECT balance FROM accounts WHERE id=%s", (body.from_wallet_id,)).fetchone()["balance"]
        if bal < body.amount:
            raise AppError(422, "INSUFFICIENT_FUNDS", "Not enough balance")
        conn.execute("UPDATE accounts SET balance = %s WHERE id=%s", (bal - body.amount, body.from_wallet_id))
        conn.execute("UPDATE accounts SET balance = balance + %s WHERE id=%s", (body.amount, body.to_wallet_id))
    return {"ok": True}
