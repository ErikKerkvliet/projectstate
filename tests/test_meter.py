import time

import pytest

from projectstate.config import Settings, usd_to_micro
from projectstate.meter import Meter, MeterError


@pytest.fixture
def meter(db, monkeypatch):
    s = Settings()
    m = Meter(db, s)
    db.ensure_tenant("u_a", "user")
    db.ensure_tenant("u_b", "user")
    return m


def test_prices_editable_without_restart(meter):
    assert meter.price("recall") == 5000
    meter.set_price("recall", "0.01")
    assert meter.price("recall") == 10000
    with pytest.raises(ValueError):
        meter.set_price("nope", "1")


def test_credit_is_idempotent_and_ledger_matches(meter):
    assert meter.credit("u_a", "x402", usd_to_micro("10"), ref="x402:NONCE1") == 10_000_000
    assert meter.credit("u_a", "x402", usd_to_micro("10"), ref="x402:NONCE1") is None
    assert meter.balance("u_a") == 10_000_000
    cid = meter.log_call("u_a", "prepaid", "recall", 3.2, True, None, None, 5000, "h:x", None)
    meter.charge("u_a", "prepaid", "recall", cid, 5000, "h:x")
    assert meter.balance("u_a") == 9_995_000
    assert meter.ledger_matches_wallet("u_a")
    rows = meter.db.q("SELECT kind, amount, balance_after, call_id FROM ledger WHERE tenant_id='u_a' ORDER BY id")
    assert [tuple(r) for r in rows] == [("topup", 10_000_000, 10_000_000, None), ("charge", -5000, 9_995_000, cid)]


def test_zero_balance_message_is_actionable(meter):
    with pytest.raises(MeterError) as ei:
        meter.check_balance("u_a", 5000)
    assert ei.value.kind == "insufficient_balance"
    msg = ei.value.message
    assert "$0.0050" in msg and "$0.0000" in msg and "x402" in msg
    meter.check_balance("u_a", 0)  # free tools never fail


def test_daily_call_cap(meter):
    meter.credit("u_a", "admin", 1_000_000, ref="c1", kind="credit")
    meter.set_caps("u_a", 3, None, 1000)
    for i in range(3):
        meter.check_caps("u_a")
        meter.log_call("u_a", "prepaid", "recall", 1, True, None, None, 0, f"k{i}", None)
    with pytest.raises(MeterError) as ei:
        meter.check_caps("u_a")
    assert ei.value.kind == "cap_daily_calls" and "cap 3" in ei.value.message and "00:00 UTC" in ei.value.message
    # other tenant unaffected
    meter.check_caps("u_b")


def test_daily_spend_cap_and_per_minute(meter):
    meter.credit("u_a", "admin", 1_000_000, ref="c1", kind="credit")
    meter.set_caps("u_a", 1000, 9000, 1000)
    cid = meter.log_call("u_a", "prepaid", "recall", 1, True, None, None, 5000, "k", None)
    meter.charge("u_a", "prepaid", "recall", cid, 5000, "k")
    meter.check_caps("u_a")
    cid = meter.log_call("u_a", "prepaid", "recall", 1, True, None, None, 5000, "k2", None)
    meter.charge("u_a", "prepaid", "recall", cid, 5000, "k2")
    with pytest.raises(MeterError) as ei:
        meter.check_caps("u_a")
    assert ei.value.kind == "cap_daily_spend"
    meter.set_caps("u_b", None, None, 2)
    meter.log_call("u_b", "prepaid", "recall", 1, True, None, None, 0, "a", None)
    meter.log_call("u_b", "prepaid", "recall", 1, True, None, None, 0, "b", None)
    with pytest.raises(MeterError) as ei:
        meter.check_caps("u_b")
    assert ei.value.kind == "cap_per_minute"


def test_blocked(meter):
    meter.set_caps("u_a", None, None, None, blocked=True)
    with pytest.raises(MeterError) as ei:
        meter.check_caps("u_a")
    assert ei.value.kind == "blocked"


def test_dedup_keys_and_window(meter):
    k1 = meter.request_key("u_a", "recall", {"project": "p", "query": "x"}, None)
    k2 = meter.request_key("u_a", "recall", {"query": "x", "project": "p"}, None)
    k3 = meter.request_key("u_b", "recall", {"project": "p", "query": "x"}, None)
    assert k1 == k2 != k3 and k1.startswith("h:")
    ke = meter.request_key("u_a", "remember", {"title": "a"}, "abc")
    assert ke.startswith("k:")
    assert meter.dedup_get("u_a", k1) is None
    meter.dedup_put("u_a", k1, 7, {"content": []})
    assert meter.dedup_get("u_a", k1) == (7, {"content": []})
    assert meter.dedup_get("u_b", k1) is None  # tenant-scoped
    meter.db.set_setting("dedup_window", "0")
    time.sleep(0.01)
    assert meter.dedup_get("u_a", k1) is None  # hash keys expire
    meter.dedup_put("u_a", ke, 8, {"content": []})
    assert meter.dedup_get("u_a", ke) is not None  # explicit keys do not
    assert meter.dedup_cleanup() == 0


def test_volume_alarm(meter):
    meter.db.set_setting("alarm.calls_per_hour", "100")
    for i in range(100):
        meter.log_call("u_a", "prepaid", "recall", 1, True, None, None, 0, None, None)
    meter.check_volume_alarm("u_a")
    alarms = meter.db.q("SELECT * FROM alarms")
    assert len(alarms) == 1 and alarms[0]["tenant_id"] == "u_a"
    meter.check_volume_alarm("u_a")
    assert meter.db.val("SELECT COUNT(*) FROM alarms") == 1
