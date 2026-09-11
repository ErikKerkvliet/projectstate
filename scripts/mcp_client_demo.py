"""Smoke test: connect as a real MCP client with a credit token and call every tool.

usage: python scripts/mcp_client_demo.py URL CREDIT_TOKEN      (token from scripts/grant_credit.py)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, token: str) -> None:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30, verify=False)
    async with Client(streamable_http_client(url, http_client=http)) as client:
        tools = await client.list_tools()
        print("tools:", [(t.name, (t.meta or {}).get("priceUsd")) for t in tools.tools])
        for name, args in [
            ("project_open", {"project": "demo", "name": "Demo project", "description": "smoke test"}),
            ("remember", {"project": "demo", "kind": "decision", "title": "Use SQLite FTS5 for recall", "body": "no vectors in v1", "tags": ["search"]}),
            ("remember", {"project": "demo", "kind": "attempt", "title": "Tried pgvector", "body": "too heavy for 1 GB VPS", "status": "failed"}),
            ("remember", {"project": "demo", "kind": "task", "title": "Write smoke test"}),
            ("recall", {"project": "demo", "query": "vectors search"}),
            ("project_status", {"project": "demo", "set_status": "smoke test passed"}),
            ("project_open", {}),
            ("recall", {"project": "nope", "query": "x"}),
        ]:
            r = await client.call_tool(name, args)
            txt = " | ".join(getattr(c, "text", "") for c in r.content)
            print(f"\n== {name} {json.dumps(args)[:60]} -> is_error={r.is_error} charged={(r.meta or {}).get('projectstate/chargedUsd')} balance={(r.meta or {}).get('projectstate/balanceUsd')}\n{txt[:400]}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
