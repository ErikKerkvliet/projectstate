"""Project-state domain: projects, entries, FTS5 recall with compact output and retrieval-quality logging.

Every function takes a tenant_id and only ever touches that tenant's rows (multi-tenant isolation lives here,
not in the callers).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .db import Database, dumps, now_iso

KINDS = ("decision", "attempt", "task", "note")
STATUS_BY_KIND: dict[str, tuple[str, ...]] = {
    "decision": ("active", "superseded", "reverted"),
    "attempt": ("worked", "failed", "partial"),
    "task": ("open", "done", "dropped"),
    "note": ("active", "archived"),
}
DEFAULT_STATUS = {"decision": "active", "attempt": "failed", "task": "open", "note": "active"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_TITLE = 200
MAX_BODY = 8000
MAX_TAGS = 20


class StoreError(ValueError):
    """Raised with a message written for an LLM caller: what was wrong and how to fix it."""


@dataclass
class Project:
    id: int
    tenant_id: str
    slug: str
    name: str
    description: str
    status: str
    created_at: str
    updated_at: str


def _norm_slug(slug: str) -> str:
    s = (slug or "").strip().lower()
    if not SLUG_RE.match(s):
        raise StoreError(
            f"Invalid project slug {slug!r}: use 1-64 lowercase letters, digits, '.', '_' or '-' (e.g. 'my-app')."
        )
    return s


def _norm_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\s]+", tags) if t]
    if not isinstance(tags, list):
        raise StoreError("Parameter 'tags' must be a list of short strings, e.g. [\"auth\", \"db\"].")
    out: list[str] = []
    for t in tags[:MAX_TAGS]:
        t = str(t).strip().lower()[:40]
        if t and t not in out:
            out.append(t)
    return out


def _norm_files(files: Any) -> list[str]:
    if files is None:
        return []
    if isinstance(files, str):
        files = [f for f in re.split(r"[,\s]+", files) if f]
    if not isinstance(files, list):
        raise StoreError("Parameter 'files' must be a list of file paths, e.g. [\"src/auth.py\"].")
    out: list[str] = []
    for f in files[:50]:
        f = str(f).strip()[:300]
        if f and f not in out:
            out.append(f)
    return out


def _content_hash(kind: str, title: str, body: str) -> str:
    h = hashlib.sha256()
    h.update(kind.encode())
    h.update(b"\0")
    h.update(" ".join(title.split()).lower().encode())
    h.update(b"\0")
    h.update(" ".join(body.split()).lower().encode())
    return h.hexdigest()[:32]


def _short(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


class Store:
    def __init__(self, db: Database):
        self.db = db

    # -- projects -----------------------------------------------------------------------------
    def get_project(self, tenant_id: str, slug: str) -> Project | None:
        r = self.db.one("SELECT * FROM projects WHERE tenant_id=? AND slug=?", (tenant_id, _norm_slug(slug)))
        return Project(**dict(r)) if r else None

    def require_project(self, tenant_id: str, slug: str) -> Project:
        p = self.get_project(tenant_id, slug)
        if p is None:
            known = [r["slug"] for r in self.db.q("SELECT slug FROM projects WHERE tenant_id=? ORDER BY updated_at DESC LIMIT 8", (tenant_id,))]
            hint = f" Known projects: {', '.join(known)}." if known else " You have no projects yet."
            raise StoreError(f"Unknown project {slug!r}. Call project_open(project={slug!r}) first to create it.{hint}")
        return p

    def open_project(self, tenant_id: str, slug: str, name: str | None = None, description: str | None = None) -> tuple[Project, bool]:
        slug = _norm_slug(slug)
        with self.db.tx():
            p = self.get_project(tenant_id, slug)
            created = False
            if p is None:
                ts = now_iso()
                self.db.exec(
                    "INSERT INTO projects(tenant_id,slug,name,description,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (tenant_id, slug, (name or slug)[:120], (description or "")[:2000], "", ts, ts),
                )
                created = True
            elif (name and name != p.name) or (description and description != p.description):
                self.db.exec(
                    "UPDATE projects SET name=?, description=?, updated_at=? WHERE id=?",
                    ((name or p.name)[:120], (description if description is not None else p.description)[:2000], now_iso(), p.id),
                )
            p = self.get_project(tenant_id, slug)
            assert p is not None
            return p, created

    def list_projects(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self.db.q(
            "SELECT p.slug, p.name, p.status, p.updated_at, "
            "(SELECT COUNT(*) FROM entries e WHERE e.project_id=p.id AND e.deleted=0) AS n "
            "FROM projects p WHERE p.tenant_id=? ORDER BY p.updated_at DESC LIMIT 100",
            (tenant_id,),
        )
        return [dict(r) for r in rows]

    def set_status(self, tenant_id: str, slug: str, status: str) -> Project:
        p = self.require_project(tenant_id, slug)
        self.db.exec("UPDATE projects SET status=?, updated_at=? WHERE id=?", (_short(status, 600), now_iso(), p.id))
        return self.require_project(tenant_id, slug)

    # -- entries ------------------------------------------------------------------------------
    def remember(
        self,
        tenant_id: str,
        slug: str,
        kind: str,
        title: str,
        body: str = "",
        tags: Any = None,
        files: Any = None,
        status: str | None = None,
        supersedes: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert an entry. Returns (entry, created). Identical content in the same project is returned, not duplicated."""
        kind = (kind or "").strip().lower()
        if kind not in KINDS:
            raise StoreError(f"Invalid kind {kind!r}. Use one of: {', '.join(KINDS)}.")
        title = " ".join((title or "").split())
        if not title:
            raise StoreError("Parameter 'title' is required: one line that states the decision/attempt/task/note.")
        if len(title) > MAX_TITLE:
            raise StoreError(f"'title' is too long ({len(title)} chars, max {MAX_TITLE}); put details in 'body'.")
        body = (body or "").strip()
        if len(body) > MAX_BODY:
            raise StoreError(f"'body' is too long ({len(body)} chars, max {MAX_BODY}); summarise it.")
        allowed = STATUS_BY_KIND[kind]
        status = (status or DEFAULT_STATUS[kind]).strip().lower()
        if status not in allowed:
            raise StoreError(f"Invalid status {status!r} for kind {kind!r}. Use one of: {', '.join(allowed)}.")
        tag_list = _norm_tags(tags)
        file_list = _norm_files(files)
        p = self.require_project(tenant_id, slug)
        chash = _content_hash(kind, title, body)
        with self.db.tx():
            existing = self.db.one(
                "SELECT * FROM entries WHERE project_id=? AND content_hash=? AND deleted=0", (p.id, chash)
            )
            if existing:
                return self._row_to_entry(existing), False
            if supersedes is not None:
                old = self.db.one("SELECT * FROM entries WHERE id=? AND tenant_id=? AND deleted=0", (supersedes, tenant_id))
                if old is None:
                    raise StoreError(f"'supersedes' refers to unknown entry #{supersedes}. Use an id returned by recall/remember.")
                if old["kind"] == "decision":
                    self.db.exec("UPDATE entries SET status='superseded', updated_at=? WHERE id=?", (now_iso(), old["id"]))
                elif old["kind"] == "task":
                    self.db.exec("UPDATE entries SET status='dropped', updated_at=? WHERE id=?", (now_iso(), old["id"]))
            ts = now_iso()
            eid = self.db.exec(
                "INSERT INTO entries(tenant_id,project_id,kind,title,body,tags,files,status,supersedes,content_hash,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, p.id, kind, title, body, " ".join(tag_list), " ".join(file_list), status, supersedes, chash, ts, ts),
            )
            self.db.exec("UPDATE projects SET updated_at=? WHERE id=?", (ts, p.id))
            row = self.db.one("SELECT * FROM entries WHERE id=?", (eid,))
            assert row is not None
            return self._row_to_entry(row), True

    def get_entry(self, tenant_id: str, entry_id: int) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM entries WHERE id=? AND tenant_id=? AND deleted=0", (entry_id, tenant_id))
        return self._row_to_entry(row) if row else None

    def update(
        self,
        tenant_id: str,
        slug: str,
        entry_id: int,
        status: str | None = None,
        title: str | None = None,
        body: str | None = None,
        append: str | None = None,
        tags: Any = None,
        files: Any = None,
        delete: bool = False,
    ) -> dict[str, Any]:
        p = self.require_project(tenant_id, slug)
        with self.db.tx():
            row = self.db.one("SELECT * FROM entries WHERE id=? AND tenant_id=? AND project_id=? AND deleted=0", (entry_id, tenant_id, p.id))
            if row is None:
                raise StoreError(f"Entry #{entry_id} not found in project {p.slug!r}. Use an id returned by recall or remember.")
            if delete:
                self.db.exec("UPDATE entries SET deleted=1, updated_at=? WHERE id=?", (now_iso(), entry_id))
                e = self._row_to_entry(row)
                e["deleted"] = True
                return e
            sets: dict[str, Any] = {}
            if status is not None:
                status = status.strip().lower()
                allowed = STATUS_BY_KIND[row["kind"]]
                if status not in allowed:
                    raise StoreError(f"Invalid status {status!r} for a {row['kind']}. Use one of: {', '.join(allowed)}.")
                sets["status"] = status
            if title is not None:
                title = " ".join(title.split())
                if not title or len(title) > MAX_TITLE:
                    raise StoreError(f"'title' must be 1-{MAX_TITLE} chars.")
                sets["title"] = title
            new_body = row["body"]
            if body is not None:
                new_body = body.strip()
            if append:
                new_body = (new_body + "\n" if new_body else "") + append.strip()
            if len(new_body) > MAX_BODY:
                raise StoreError(f"'body' would be {len(new_body)} chars, max {MAX_BODY}. Summarise or start a new entry.")
            if new_body != row["body"]:
                sets["body"] = new_body
            if tags is not None:
                sets["tags"] = " ".join(_norm_tags(tags))
            if files is not None:
                sets["files"] = " ".join(_norm_files(files))
            if not sets:
                raise StoreError("Nothing to update: pass at least one of status, title, body, append, tags, files or delete=true.")
            if "title" in sets or "body" in sets:
                sets["content_hash"] = _content_hash(row["kind"], sets.get("title", row["title"]), sets.get("body", row["body"]))
            sets["updated_at"] = now_iso()
            cols = ", ".join(f"{k}=?" for k in sets)
            try:
                self.db.exec(f"UPDATE entries SET {cols} WHERE id=?", (*sets.values(), entry_id))
            except Exception as exc:  # UNIQUE(project_id, content_hash)
                if "UNIQUE" in str(exc):
                    raise StoreError("An identical entry already exists in this project; update that one instead.") from exc
                raise
            self.db.exec("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), p.id))
            new = self.db.one("SELECT * FROM entries WHERE id=?", (entry_id,))
            assert new is not None
            return self._row_to_entry(new)

    def _row_to_entry(self, row: Any) -> dict[str, Any]:
        d = dict(row)
        d["tags"] = d["tags"].split() if d.get("tags") else []
        d["files"] = d["files"].split() if d.get("files") else []
        return d

    # -- recall -------------------------------------------------------------------------------
    @staticmethod
    def _fts_terms(query: str) -> list[str]:
        toks = re.findall(r"[\w][\w'’.-]*", query.lower())
        out = []
        for t in toks:
            t = t.strip(".-'’")
            if len(t) >= 2 and t not in ("the", "and", "for", "with", "that", "this", "from", "how", "what", "why", "did", "was", "were"):
                out.append(t)
        return out[:12]

    def recall(
        self,
        tenant_id: str,
        slug: str,
        query: str = "",
        kind: str | None = None,
        status: str | None = None,
        tags: Any = None,
        files: Any = None,
        limit: int = 5,
        max_chars: int = 1500,
        log: bool = True,
    ) -> dict[str, Any]:
        """Keyword search, ranked, trimmed to a character budget. Returns {'entries': [...], 'mode', 'n_candidates', 'text'}."""
        p = self.require_project(tenant_id, slug)
        limit = max(1, min(int(limit or 5), 20))
        max_chars = max(200, min(int(max_chars or 1500), 8000))
        kind = (kind or "").strip().lower() or None
        if kind and kind not in KINDS:
            raise StoreError(f"Invalid kind filter {kind!r}. Use one of: {', '.join(KINDS)}, or omit it.")
        status = (status or "").strip().lower() or None
        tag_list = _norm_tags(tags)
        file_list = _norm_files(files)
        where = ["e.project_id=?", "e.tenant_id=?", "e.deleted=0"]
        params: list[Any] = [p.id, tenant_id]
        if kind:
            where.append("e.kind=?")
            params.append(kind)
        if status:
            where.append("e.status=?")
            params.append(status)
        for t in tag_list:
            where.append("(' '||e.tags||' ') LIKE ?")
            params.append(f"% {t} %")
        for f in file_list:
            where.append("(' '||e.files||' ') LIKE ?")
            params.append(f"%{f}%")
        terms = self._fts_terms(query or "")
        rows: list[Any] = []
        mode = "recent"
        n_candidates = 0
        if terms:
            for mode_name, joiner in (("and", " AND "), ("or", " OR ")):
                match = joiner.join(f'"{t}"' + ("*" if i == len(terms) - 1 and len(t) >= 3 else "") for i, t in enumerate(terms))
                sql = (
                    "SELECT e.*, bm25(entries_fts, 4.0, 1.0, 2.0, 2.0) AS score, "
                    "snippet(entries_fts, 1, '[', ']', '…', 18) AS snip "
                    "FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
                    f"WHERE entries_fts MATCH ? AND {' AND '.join(where)} ORDER BY score LIMIT 60"
                )
                try:
                    rows = self.db.q(sql, (match, *params))
                except Exception:
                    rows = []
                n_candidates = len(rows)
                mode = mode_name
                if len(rows) >= limit:
                    break
        if not terms:
            sql = f"SELECT e.*, 0.0 AS score, '' AS snip FROM entries e WHERE {' AND '.join(where)} ORDER BY e.updated_at DESC, e.id DESC LIMIT 60"
            rows = self.db.q(sql, tuple(params))
            n_candidates = len(rows)
        ranked = self._rank(rows, terms)
        chosen = ranked[:limit]
        text, chars = self._format(p, query, chosen, terms, max_chars, n_candidates, mode)
        top_score = float(chosen[0]["_score"]) if chosen else None
        if log:
            self.db.exec(
                "INSERT INTO search_log(ts,tenant_id,project_id,query,filters,mode,n_candidates,n_returned,top_score,chars_returned)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), tenant_id, p.id, query or "", dumps({"kind": kind, "status": status, "tags": tag_list, "files": file_list}),
                 mode, n_candidates, len(chosen), top_score, chars),
            )
        return {"entries": chosen, "mode": mode, "n_candidates": n_candidates, "text": text}

    def _rank(self, rows: list[Any], terms: list[str]) -> list[dict[str, Any]]:
        out = []
        for r in rows:
            e = self._row_to_entry(r)
            base = -float(r["score"]) if terms else 0.0  # bm25 is negative-better in sqlite
            boost = 0.0
            st = e["status"]
            if st in ("superseded", "dropped", "archived", "reverted"):
                boost -= 1.5
            elif st in ("active", "open", "worked"):
                boost += 0.3
            if e["kind"] == "decision":
                boost += 0.2
            e["_score"] = base + boost
            e["_snip"] = r["snip"]
            out.append(e)
        out.sort(key=lambda e: (-e["_score"], e["updated_at"]), reverse=False)
        out.sort(key=lambda e: -e["_score"])
        return out

    @staticmethod
    def format_entry_line(e: dict[str, Any], snippet: bool = True, body_chars: int = 160) -> str:
        date = e["updated_at"][:10]
        head = f"#{e['id']} {e['kind']}/{e['status']} ({date}): {e['title']}"
        extra = []
        snip = (e.get("_snip") or "").strip() if snippet else ""
        if snip and snip.replace("[", "").replace("]", "") not in e["title"]:
            extra.append(_short(snip, body_chars))
        elif e.get("body"):
            extra.append(_short(e["body"], body_chars))
        if e.get("tags"):
            extra.append("tags: " + ",".join(e["tags"][:6]))
        if e.get("files"):
            extra.append("files: " + ",".join(e["files"][:4]))
        return head + (" — " + " | ".join(extra) if extra else "")

    def _format(self, p: Project, query: str, chosen: list[dict[str, Any]], terms: list[str], max_chars: int, n_cand: int, mode: str) -> tuple[str, int]:
        if not chosen:
            if terms:
                txt = f"No entries in '{p.slug}' match {query!r}. Try fewer or different keywords, or omit query to list recent entries."
            else:
                txt = f"Project '{p.slug}' has no entries yet (matching the given filters)."
            return txt, len(txt)
        header = f"{len(chosen)} of {n_cand} matching entries in '{p.slug}'" + (f" for {query!r}" if terms else " (most recent)") + ":"
        body_lines = [self.format_entry_line(e) for e in chosen]
        kept = len(body_lines)
        while kept >= 0:
            lines = [header] + body_lines[:kept]
            dropped = len(body_lines) - kept
            if dropped:
                lines.append(f"… {dropped} more not shown (raise max_chars or narrow the query)")
            txt = "\n".join(lines)
            if len(txt) <= max_chars or kept == 0:
                break
            kept -= 1
        if len(txt) > max_chars:
            txt = txt[: max_chars - 1] + "…"
        return txt, len(txt)

    # -- briefs -------------------------------------------------------------------------------
    def brief(self, tenant_id: str, slug: str, max_chars: int = 1800) -> str:
        p = self.require_project(tenant_id, slug)
        counts = {r["kind"]: r["n"] for r in self.db.q("SELECT kind, COUNT(*) n FROM entries WHERE project_id=? AND deleted=0 GROUP BY kind", (p.id,))}
        total = sum(counts.values())
        lines = [f"Project '{p.slug}' — {p.name}" + (f": {_short(p.description, 200)}" if p.description else "")]
        lines.append(f"Status: {p.status}" if p.status else "Status: (none set — use project_status to set one)")
        lines.append("Entries: " + (", ".join(f"{counts.get(k, 0)} {k}s" for k in KINDS if counts.get(k)) or "none yet") + f" ({total} total)")

        def section(title: str, sql: str, n: int, body_chars: int = 110) -> None:
            rows = self.db.q(sql, (p.id, n))
            if rows:
                lines.append(title)
                for r in rows:
                    lines.append("  " + self.format_entry_line(self._row_to_entry(r), snippet=False, body_chars=body_chars))

        section("Open tasks:", "SELECT * FROM entries WHERE project_id=? AND deleted=0 AND kind='task' AND status='open' ORDER BY updated_at DESC LIMIT ?", 5)
        section("Active decisions (latest):", "SELECT * FROM entries WHERE project_id=? AND deleted=0 AND kind='decision' AND status='active' ORDER BY updated_at DESC LIMIT ?", 5)
        section("Failed attempts (latest):", "SELECT * FROM entries WHERE project_id=? AND deleted=0 AND kind='attempt' AND status='failed' ORDER BY updated_at DESC LIMIT ?", 3)
        lines.append("Use recall(project, query) to search; remember(project, kind, title, body) to add.")
        out = []
        used = 0
        for ln in lines:
            if used + len(ln) + 1 > max_chars:
                out.append("…")
                break
            out.append(ln)
            used += len(ln) + 1
        return "\n".join(out)

    def status_block(self, tenant_id: str, slug: str) -> str:
        p = self.require_project(tenant_id, slug)
        lines = [f"'{p.slug}' status: {p.status or '(none set)'}  (updated {p.updated_at[:16].replace('T', ' ')} UTC)"]
        open_tasks = self.db.q("SELECT * FROM entries WHERE project_id=? AND deleted=0 AND kind='task' AND status='open' ORDER BY updated_at DESC LIMIT 5", (p.id,))
        if open_tasks:
            lines.append(f"Open tasks ({len(open_tasks)} shown):")
            lines += ["  " + self.format_entry_line(self._row_to_entry(r), snippet=False, body_chars=80) for r in open_tasks]
        last = self.db.one("SELECT * FROM entries WHERE project_id=? AND deleted=0 ORDER BY updated_at DESC LIMIT 1", (p.id,))
        if last:
            lines.append("Last change: " + self.format_entry_line(self._row_to_entry(last), snippet=False, body_chars=80))
        return "\n".join(lines)

    # -- retrieval quality metrics (is FTS enough?) -------------------------------------------
    def search_quality(self, days: int = 30) -> dict[str, Any]:
        rows = self.db.q(
            "SELECT ts, tenant_id, project_id, query, mode, n_candidates, n_returned FROM search_log "
            "WHERE ts >= datetime('now', ?) AND query <> '' ORDER BY tenant_id, project_id, ts",
            (f"-{int(days)} days",),
        )
        total = len(rows)
        zero = sum(1 for r in rows if (r["n_returned"] or 0) == 0)
        or_fallback = sum(1 for r in rows if r["mode"] == "or")
        requery = 0
        prev = None
        for r in rows:
            if prev and prev["tenant_id"] == r["tenant_id"] and prev["project_id"] == r["project_id"]:
                from datetime import datetime

                dt = (datetime.fromisoformat(r["ts"]) - datetime.fromisoformat(prev["ts"])).total_seconds()
                if 0 <= dt <= 90 and r["query"].strip().lower() != prev["query"].strip().lower():
                    requery += 1
            prev = r
        return {
            "searches": total,
            "zero_hit": zero,
            "zero_hit_rate": (zero / total) if total else 0.0,
            "or_fallback": or_fallback,
            "or_fallback_rate": (or_fallback / total) if total else 0.0,
            "requery_within_90s": requery,
            "requery_rate": (requery / total) if total else 0.0,
        }
