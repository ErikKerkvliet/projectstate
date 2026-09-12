"""The five MCP tools. Tenant comes from the request context set by the metering middleware."""
from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .store import KINDS, Store, StoreError
from .tenancy import require_caller

INSTRUCTIONS = """projectstate keeps per-project memory for coding agents: decisions, attempts (what was tried and
whether it worked), open tasks and notes, scoped to a project slug. Typical session:
1. project_open(project) at the start -> a short brief of status, open tasks, latest decisions and failures.
2. plan_check(project, intent) before starting anything non-trivial -> the failed attempts, binding decisions
   and overlapping tasks that would change your plan. One call instead of guessing.
3. recall(project, query) to look something up -> the few most relevant entries, never the whole history.
4. remember(project, kind, title, body) whenever you decide something, try something, or finish/plan a task.
5. update(project, id, status=...) to close tasks, mark attempts, or supersede decisions.
6. project_status(project, set_status=...) to leave a one-line handoff note for the next session.
Every call is metered (prices are listed per tool in tools/list _meta.priceUsd). Writes are idempotent."""


def _err(exc: Exception) -> ToolError:
    return ToolError(str(exc))


def register_tools(mcp: MCPServer, store: Store) -> None:
    @mcp.tool(name="project_open", title="Open project", annotations=None)
    async def project_open(project: str | None = None, name: str | None = None, description: str | None = None) -> str:
        """Open (or create) a project and get a compact brief: status line, open tasks, latest active decisions and
        failed attempts. Call this first in a session. Omit `project` to list your projects.
        `project` is a slug like 'my-app' (lowercase, digits, '-', '_' or '.')."""
        caller = require_caller()
        try:
            if not project:
                projects = store.list_projects(caller.tenant_id)
                if not projects:
                    return "You have no projects yet. Call project_open(project='some-slug', name='Readable name') to create one."
                lines = [f"{len(projects)} project(s):"]
                for p in projects[:50]:
                    lines.append(f"- {p['slug']} — {p['name']} ({p['n']} entries, updated {p['updated_at'][:10]})" + (f": {p['status'][:80]}" if p["status"] else ""))
                return "\n".join(lines)
            p, created = store.open_project(caller.tenant_id, project, name, description)
            brief = store.brief(caller.tenant_id, p.slug)
            return ("Created new project.\n" if created else "") + brief
        except StoreError as exc:
            raise _err(exc)

    @mcp.tool(name="project_status", title="Project status")
    async def project_status(project: str, set_status: str | None = None) -> str:
        """Get the project's one-line status, open tasks and last change. Pass `set_status` to overwrite the status
        line (e.g. 'v0.3 shipped; next: migrate auth to OAuth'). Cheap; use it for handoff notes between sessions."""
        caller = require_caller()
        try:
            if set_status is not None:
                store.set_status(caller.tenant_id, project, set_status)
            return store.status_block(caller.tenant_id, project)
        except StoreError as exc:
            raise _err(exc)

    @mcp.tool(name="remember", title="Remember")
    async def remember(
        project: str,
        kind: str,
        title: str,
        body: str = "",
        tags: list[str] | None = None,
        files: list[str] | None = None,
        status: str | None = None,
        supersedes: int | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Store one fact about the project. `kind` is one of: decision (what was chosen and why), attempt (something
        tried; status 'worked'|'failed'|'partial'), task (status 'open'|'done'|'dropped'), note (anything else).
        `title` is one line; put details, reasons and error messages in `body`. `files` lists related paths.
        `supersedes` marks an older decision/task as replaced by this one. Identical content is stored once, so
        retries are safe; `idempotency_key` additionally guarantees a retry is not billed twice."""
        caller = require_caller()
        try:
            e, created = store.remember(caller.tenant_id, project, kind, title, body, tags, files, status, supersedes)
        except StoreError as exc:
            raise _err(exc)
        verb = "Saved" if created else "Already stored (identical content)"
        return f"{verb} #{e['id']} {e['kind']}/{e['status']}: {e['title']}"

    @mcp.tool(name="recall", title="Recall")
    async def recall(
        project: str,
        query: str = "",
        kind: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        files: list[str] | None = None,
        limit: int = 5,
        max_chars: int = 1500,
    ) -> str:
        """Search the project's memory with keywords and get the few most relevant entries (ranked, active entries
        first, superseded ones last), trimmed to `max_chars`. Use 2-5 specific keywords (e.g. 'auth token expiry').
        Filters: kind (decision|attempt|task|note), status, tags, files. Omit `query` for the most recent entries.
        Follow up with update(project, id, ...) or remember(...)."""
        caller = require_caller()
        try:
            r = store.recall(caller.tenant_id, project, query, kind, status, tags, files, limit, max_chars)
        except StoreError as exc:
            raise _err(exc)
        return r["text"]

    @mcp.tool(name="plan_check", title="Plan check")
    async def plan_check(
        project: str,
        intent: str,
        files: list[str] | None = None,
        limit: int = 3,
        max_chars: int = 1200,
    ) -> str:
        """Before you start something non-trivial, say what you are about to do and get back only what would
        change your mind: attempts that already failed at this, decisions that are still in force and constrain
        it, and open tasks that overlap. `intent` is one line in plain words, e.g. 'switch the cache to Redis'
        or 'rewrite the auth middleware to use refresh tokens'. Pass `files` if you know which paths you will
        touch; entries about those files are included even when the wording differs. Cheaper than repeating
        work that already failed once."""
        caller = require_caller()
        try:
            r = store.plan_check(caller.tenant_id, project, intent, files, limit, max_chars)
        except StoreError as exc:
            raise _err(exc)
        return r["text"]

    @mcp.tool(name="update", title="Update entry")
    async def update(
        project: str,
        id: int,
        status: str | None = None,
        title: str | None = None,
        body: str | None = None,
        append: str | None = None,
        tags: list[str] | None = None,
        files: list[str] | None = None,
        delete: bool = False,
    ) -> str:
        """Change an existing entry by id: set `status` (task open|done|dropped, attempt worked|failed|partial,
        decision active|superseded|reverted, note active|archived), replace `title`/`body`, `append` a line to the
        body (e.g. an outcome), replace `tags`/`files`, or `delete` it."""
        caller = require_caller()
        try:
            e = store.update(caller.tenant_id, project, id, status, title, body, append, tags, files, delete)
        except StoreError as exc:
            raise _err(exc)
        if e.get("deleted"):
            return f"Deleted #{e['id']} ({e['kind']}: {e['title']})"
        return f"Updated #{e['id']} {e['kind']}/{e['status']}: {e['title']}"


TOOL_NAMES = ("project_open", "project_status", "remember", "recall", "plan_check", "update")
