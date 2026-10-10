import uuid

import httpx

BASE = "http://localhost:8000"


def headers():
    return {"Idempotency-Key": str(uuid.uuid4())}


def create_wallet(name):
    tag = uuid.uuid4().hex
    user = httpx.post(
        f"{BASE}/users",
        headers=headers(),
        json={"name": name, "email": f"{tag}@example.test", "phone": tag},
    )
    assert user.status_code == 201, user.text
    wallet = httpx.post(
        f"{BASE}/users/{user.json()['id']}/wallets",
        headers=headers(),
        json={},
    )
    assert wallet.status_code == 201, wallet.text
    return user.json(), wallet.json()["id"]


def test_fraud_hold_and_admin_release():
    source_user, source = create_wallet("source")
    _, target = create_wallet("target")
    topup = httpx.post(
        f"{BASE}/transfers/topup",
        headers=headers(),
        json={"wallet_id": source, "amount": 10000},
    )
    assert topup.status_code == 201, topup.text

    for _ in range(5):
        response = httpx.post(
            f"{BASE}/transfers",
            headers=headers(),
            json={"from_wallet_id": source, "to_wallet_id": target, "amount": 100},
        )
        assert response.status_code == 201, response.text

    held = httpx.post(
        f"{BASE}/transfers",
        headers=headers(),
        json={"from_wallet_id": source, "to_wallet_id": target, "amount": 100},
    )
    assert held.status_code == 202, held.text
    transfer_id = held.json()["transfer_id"]
    release = httpx.post(
        f"{BASE}/admin/risk/{transfer_id}/decision",
        params={"decision": "release"},
        headers=headers(),
    )
    assert release.status_code == 200, release.text
    assert release.json()["status"] == "completed"


def test_reversal_restores_balances():
    _, source = create_wallet("sender")
    _, target = create_wallet("receiver")
    assert httpx.post(
        f"{BASE}/transfers/topup", headers=headers(),
        json={"wallet_id": source, "amount": 5000},
    ).status_code == 201
    transfer = httpx.post(
        f"{BASE}/transfers", headers=headers(),
        json={"from_wallet_id": source, "to_wallet_id": target, "amount": 1000},
    )
    assert transfer.status_code == 201, transfer.text
    reversal = httpx.post(
        f"{BASE}/transfers/{transfer.json()['transfer_id']}/reverse",
        headers=headers(),
    )
    assert reversal.status_code == 201, reversal.text
    assert httpx.get(f"{BASE}/wallets/{source}/balance").json()["balance"] == 5000
    assert httpx.get(f"{BASE}/wallets/{target}/balance").json()["balance"] == 0


def test_statement_qa_is_limited_to_calling_user():
    user, source = create_wallet("Rahul")
    _, target = create_wallet("Receiver")
    assert httpx.post(
        f"{BASE}/transfers/topup", headers=headers(),
        json={"wallet_id": source, "amount": 5000},
    ).status_code == 201
    transfer = httpx.post(
        f"{BASE}/transfers", headers=headers(),
        json={"from_wallet_id": source, "to_wallet_id": target, "amount": 700},
    )
    assert transfer.status_code == 201, transfer.text
    answer = httpx.post(
        f"{BASE}/users/{user['id']}/statement/ask",
        headers=headers(),
        json={"question": "How much did I send between 2026-01-01 and 2027-01-01?"},
    )
    assert answer.status_code == 201, answer.text
    assert answer.json()["value"] == 700
    assert transfer.json()["transfer_id"] in answer.json()["transfer_ids"]
