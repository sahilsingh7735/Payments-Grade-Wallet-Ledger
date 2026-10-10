import uuid
from concurrent.futures import ThreadPoolExecutor
import httpx

BASE = "http://localhost:8000"
H = lambda k=None: {"Idempotency-Key": str(k or uuid.uuid4())}


def setup_two_wallets():
    ids = []
    for i in range(2):
        tag = uuid.uuid4().hex[:8]
        u = httpx.post(f"{BASE}/users", headers=H(), json={"name": "t", "email": f"{tag}@x.com", "phone": tag}).json()
        w = httpx.post(f"{BASE}/users/{u['id']}/wallets", headers=H(), json={}).json()
        ids.append(w["id"])
    return ids


def bal(w):
    return httpx.get(f"{BASE}/wallets/{w}/balance").json()["balance"]


def test_stampede(core_concurrency_without_fraud):
    a, b = setup_two_wallets()
    assert httpx.post(f"{BASE}/transfers/topup", headers=H(), json={"wallet_id": a, "amount": 50000}).status_code == 201

    def pay(_):
        return httpx.post(f"{BASE}/transfers", headers=H(), timeout=60,
                          json={"from_wallet_id": a, "to_wallet_id": b, "amount": 1000}).status_code

    with ThreadPoolExecutor(100) as ex:
        codes = list(ex.map(pay, range(100)))
    assert codes.count(201) == 50
    assert bal(a) == 0 and bal(b) == 50000


def test_same_key_5_times_sequential():
    a, b = setup_two_wallets()
    httpx.post(f"{BASE}/transfers/topup", headers=H(), json={"wallet_id": a, "amount": 10000})
    k = uuid.uuid4()
    body = {"from_wallet_id": a, "to_wallet_id": b, "amount": 100}
    rs = [httpx.post(f"{BASE}/transfers", headers=H(k), json=body) for _ in range(5)]
    assert all(r.status_code == 201 for r in rs)
    assert len({r.text for r in rs}) == 1
    assert bal(b) == 100


def test_same_key_10_parallel():
    a, b = setup_two_wallets()
    httpx.post(f"{BASE}/transfers/topup", headers=H(), json={"wallet_id": a, "amount": 10000})
    k = uuid.uuid4()
    body = {"from_wallet_id": a, "to_wallet_id": b, "amount": 100}
    with ThreadPoolExecutor(10) as ex:
        rs = list(ex.map(lambda _: httpx.post(f"{BASE}/transfers", headers=H(k), json=body), range(10)))
    assert bal(b) == 100


def test_same_key_different_body_is_422():
    a, b = setup_two_wallets()
    httpx.post(f"{BASE}/transfers/topup", headers=H(), json={"wallet_id": a, "amount": 10000})
    k = uuid.uuid4()
    httpx.post(f"{BASE}/transfers", headers=H(k), json={"from_wallet_id": a, "to_wallet_id": b, "amount": 100})
    r = httpx.post(f"{BASE}/transfers", headers=H(k), json={"from_wallet_id": a, "to_wallet_id": b, "amount": 999})
    assert r.status_code == 422 and bal(b) == 100
