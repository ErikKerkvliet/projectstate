import pytest

from projectstate.store import Store, StoreError


@pytest.fixture
def store(db):
    db.ensure_tenant("u_a", "user")
    db.ensure_tenant("u_b", "user")
    return Store(db)


def test_open_is_idempotent(store):
    p1, created1 = store.open_project("u_a", "My-App", name="My App")
    p2, created2 = store.open_project("u_a", "my-app")
    assert created1 and not created2 and p1.id == p2.id and p2.name == "My App"


def test_bad_slug(store):
    with pytest.raises(StoreError, match="Invalid project slug"):
        store.open_project("u_a", "bad slug!")


def test_remember_dedups_identical_content(store):
    store.open_project("u_a", "app")
    e1, c1 = store.remember("u_a", "app", "decision", "Use SQLite", "FTS5 is enough for v1")
    e2, c2 = store.remember("u_a", "app", "decision", "use  sqlite", "fts5 is enough for v1 ")
    assert c1 and not c2 and e1["id"] == e2["id"]


def test_validation_messages_are_actionable(store):
    store.open_project("u_a", "app")
    with pytest.raises(StoreError, match="Invalid kind"):
        store.remember("u_a", "app", "thought", "x")
    with pytest.raises(StoreError, match="Invalid status 'done' for kind 'decision'"):
        store.remember("u_a", "app", "decision", "x", status="done")
    with pytest.raises(StoreError, match="Unknown project 'nope'"):
        store.remember("u_a", "nope", "note", "x")


def test_recall_ranks_and_trims(store):
    store.open_project("u_a", "app")
    store.remember("u_a", "app", "decision", "Auth uses short-lived JWT tokens", "expiry 15 minutes, refresh via cookie", tags=["auth"])
    store.remember("u_a", "app", "attempt", "Tried storing refresh token in localStorage", "XSS risk, rejected", status="failed", tags=["auth"])
    store.remember("u_a", "app", "note", "Database is Postgres 16", "hosted on RDS", tags=["db"])
    for i in range(30):
        store.remember("u_a", "app", "note", f"Filler note {i}", "unrelated content about widgets")
    r = store.recall("u_a", "app", "jwt token expiry")
    ids = [e["id"] for e in r["entries"]]
    assert r["entries"][0]["title"].startswith("Auth uses short-lived JWT")
    assert "#" in r["text"] and "tags: auth" in r["text"]
    assert len(r["text"]) <= 1500
    # AND fails -> OR fallback
    r2 = store.recall("u_a", "app", "jwt widgets")
    assert r2["mode"] == "or" and r2["n_candidates"] > 1
    # kind filter
    r3 = store.recall("u_a", "app", "token", kind="attempt")
    assert all(e["kind"] == "attempt" for e in r3["entries"])
    # budget respected
    r4 = store.recall("u_a", "app", "note", max_chars=300)
    assert len(r4["text"]) <= 300
    # empty query => recent
    r5 = store.recall("u_a", "app", "")
    assert r5["mode"] == "recent" and len(r5["entries"]) == 5
    # search log captured
    assert store.db.val("SELECT COUNT(*) FROM search_log") == 5
    q = store.search_quality()
    assert q["searches"] == 4  # empty query not counted


def test_superseded_decisions_rank_lower(store):
    store.open_project("u_a", "app")
    old, _ = store.remember("u_a", "app", "decision", "Cache with Redis", "for sessions")
    new, _ = store.remember("u_a", "app", "decision", "Cache with Memcached instead of Redis", "cheaper", supersedes=old["id"])
    assert store.get_entry("u_a", old["id"])["status"] == "superseded"
    r = store.recall("u_a", "app", "redis cache")
    assert r["entries"][0]["id"] == new["id"]


def test_update_and_delete(store):
    store.open_project("u_a", "app")
    t, _ = store.remember("u_a", "app", "task", "Write tests")
    u = store.update("u_a", "app", t["id"], status="done", append="done in commit abc")
    assert u["status"] == "done" and "abc" in u["body"]
    with pytest.raises(StoreError, match="Invalid status"):
        store.update("u_a", "app", t["id"], status="superseded")
    with pytest.raises(StoreError, match="Nothing to update"):
        store.update("u_a", "app", t["id"])
    d = store.update("u_a", "app", t["id"], delete=True)
    assert d["deleted"] is True and store.get_entry("u_a", t["id"]) is None
    assert store.recall("u_a", "app", "tests")["entries"] == []


def test_tenant_isolation(store):
    store.open_project("u_a", "shared-slug")
    store.open_project("u_b", "shared-slug")
    ea, _ = store.remember("u_a", "shared-slug", "note", "Secret of A", "alpha secret")
    eb, _ = store.remember("u_b", "shared-slug", "note", "Secret of B", "bravo secret")
    assert [e["id"] for e in store.recall("u_b", "shared-slug", "secret")["entries"]] == [eb["id"]]
    assert store.get_entry("u_b", ea["id"]) is None
    with pytest.raises(StoreError, match="not found"):
        store.update("u_b", "shared-slug", ea["id"], status="archived")
    with pytest.raises(StoreError, match="unknown entry"):
        store.remember("u_b", "shared-slug", "decision", "steal", supersedes=ea["id"])
    assert "Secret of A" not in store.brief("u_b", "shared-slug")
    assert [p["slug"] for p in store.list_projects("u_b")] == ["shared-slug"]


def test_brief_and_status(store):
    store.open_project("u_a", "app", name="App", description="An app")
    store.remember("u_a", "app", "task", "Ship v1")
    store.remember("u_a", "app", "decision", "Go with SQLite")
    store.remember("u_a", "app", "attempt", "Tried vectors", status="failed")
    store.set_status("u_a", "app", "v1 nearly done")
    b = store.brief("u_a", "app")
    assert "Status: v1 nearly done" in b and "Open tasks:" in b and "Failed attempts" in b and "Go with SQLite" in b
    s = store.status_block("u_a", "app")
    assert "v1 nearly done" in s and "Ship v1" in s
