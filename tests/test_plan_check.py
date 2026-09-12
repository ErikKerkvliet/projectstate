import pytest

from projectstate.store import Store, StoreError


@pytest.fixture
def store(db):
    db.ensure_tenant("u_a", "user")
    db.ensure_tenant("u_b", "user")
    s = Store(db)
    s.open_project("u_a", "app")
    s.remember("u_a", "app", "attempt", "Tried Redis for session cache", "connection pool exhausted under load", status="failed", files=["src/cache.py"])
    s.remember("u_a", "app", "attempt", "Redis cluster mode", "sentinel failover took 30 seconds", status="failed")
    s.remember("u_a", "app", "attempt", "Memcached for sessions", "worked fine", status="worked")
    s.remember("u_a", "app", "decision", "Cache with Memcached instead of Redis", "cheaper to run", status="active")
    s.remember("u_a", "app", "decision", "Old decision to use Redis", "superseded later", status="superseded")
    s.remember("u_a", "app", "task", "Benchmark cache options", status="open")
    s.remember("u_a", "app", "task", "Ship the cache change", status="done")
    s.remember("u_a", "app", "note", "Unrelated note about invoices")
    return s


def test_surfaces_failures_decisions_and_tasks(store):
    r = store.plan_check("u_a", "app", "switch the session cache to Redis")
    t = r["text"]
    assert "Prior failures" in t and "Tried Redis for session cache" in t
    assert "Active decisions" in t and "Cache with Memcached instead of Redis" in t
    assert "Open tasks" in t and "Benchmark cache options" in t
    # only the states that matter: not the superseded decision, not the done task, not the successful attempt
    assert "Old decision to use Redis" not in t
    assert "Ship the cache change" not in t
    assert "invoices" not in t


def test_clean_plan_says_so(store):
    r = store.plan_check("u_a", "app", "add a healthcheck endpoint for uptime monitoring")
    assert r["n_found"] == 0 and "Nothing on record matches this plan" in r["text"]


def test_files_pull_in_entries_the_words_missed(store):
    r = store.plan_check("u_a", "app", "rework the storage layer", files=["src/cache.py"])
    assert "src/cache.py" in r["text"] or "Tried Redis" in r["text"]
    assert r["n_found"] >= 1


def test_no_duplicate_entries_across_sections(store):
    r = store.plan_check("u_a", "app", "redis cache sessions", files=["src/cache.py"])
    ids = [e["id"] for _, entries in r["sections"] for e in entries]
    assert len(ids) == len(set(ids))


def test_budget_and_validation(store):
    r = store.plan_check("u_a", "app", "redis cache", max_chars=300)
    assert len(r["text"]) <= 300
    with pytest.raises(StoreError, match="'intent' is required"):
        store.plan_check("u_a", "app", "   ")
    with pytest.raises(StoreError, match="Unknown project"):
        store.plan_check("u_a", "nope", "anything")


def test_is_per_tenant(store):
    store.open_project("u_b", "app")
    r = store.plan_check("u_b", "app", "switch the session cache to Redis")
    assert r["n_found"] == 0 and "Nothing on record" in r["text"]


def test_logs_for_the_retrieval_quality_meter(store):
    before = store.db.val("SELECT COUNT(*) FROM search_log WHERE mode='plan_check'", (), 0)
    store.plan_check("u_a", "app", "redis cache")
    assert store.db.val("SELECT COUNT(*) FROM search_log WHERE mode='plan_check'", (), 0) == before + 1


def test_files_never_hide_entries_without_a_path(store):
    """Regression: passing `files` used to filter the targeted searches, so a decision or task that
    carries no file path disappeared exactly when the agent gave the most context."""
    without = store.plan_check("u_a", "app", "switch the session cache to Redis")
    with_files = store.plan_check("u_a", "app", "switch the session cache to Redis", files=["src/cache.py"])
    labels = lambda r: {label.split(" ")[0] + " " + label.split(" ")[1] for label, _ in r["sections"]}
    assert "Cache with Memcached instead of Redis" in with_files["text"]
    assert "Benchmark cache options" in with_files["text"]
    assert with_files["n_found"] >= without["n_found"]
