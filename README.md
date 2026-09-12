# projectstate

**Persistent project memory for AI agents, paid per call in USDC over [x402](https://x402.org). No account, no API key.**

`https://projectstate.online/mcp` — an MCP server (Streamable HTTP) that remembers, per project, *what was decided, what was tried and failed, and what is still open*, and hands an agent back the three facts it needs instead of the whole history.

## Why

Every agent already has "memory". What it does not have is **project state that survives across tools and sessions**: Claude Code, Cursor, a CI agent and a script all working on the same repo, sharing one ranked, compact record of decisions and dead ends. That is what this is. Retrieval is the point: `recall` returns at most a few lines inside a character budget, ranked so superseded decisions sink and active ones float.

## Tools

| Tool | Does | Price |
|---|---|---|
| `project_open(project, name?, description?)` | open/create a project; returns a brief: status, open tasks, latest decisions, failed attempts | $0.002 |
| `project_status(project, set_status?)` | read or set the one-line status (hand-off note between sessions) | $0.002 |
| `remember(project, kind, title, body?, tags?, files?, status?, supersedes?, idempotency_key?)` | store a `decision`, `attempt`, `task` or `note` | $0.002 |
| `recall(project, query?, kind?, status?, tags?, files?, limit=5, max_chars=1500)` | ranked keyword search, trimmed to a budget | $0.005 |
| `plan_check(project, intent, files?, limit=3, max_chars=1200)` | say what you are about to do; get back the failed attempts, binding decisions and overlapping tasks that would change the plan | $0.010 |
| `update(project, id, status?, title?, body?, append?, tags?, files?, delete?)` | close tasks, mark attempts, supersede decisions | $0.002 |

Failed calls are free. Identical retries (or any call with the same `idempotency_key`) are never billed twice. Prices are published per tool in `tools/list` under `_meta.priceUsd`.

### plan_check

The tool to call before starting something non-trivial:

```
plan_check(project="my-app", intent="switch the session cache to Redis", files=["src/cache.py"])

Plan check for 'my-app': 'switch the session cache to Redis'
Prior failures (1):
  #8 attempt/failed (2026-09-12): Tried Redis for the session cache — connection pool exhausted under load
Active decisions that constrain this (1):
  #9 decision/active (2026-09-12): Cache with SQLite instead of Redis — one less service to run
Open tasks that overlap (1):
  #10 task/open (2026-09-12): Benchmark the cache options
```

Three targeted searches in one call, returning only what would change the plan. A clean plan answers
"Nothing on record matches this plan" so the agent can proceed without a second lookup.

## How paying works

1. Call a tool. Without credit the result is `isError: true` with an x402 v2 `PaymentRequired` in `structuredContent` (scheme `exact`, USDC on Base, the amount, the pay-to address).
2. Sign the USDC payment and retry the same call with the payload in `_meta["x402/payment"]`.
3. The result carries the settlement receipt in `_meta["x402/payment-response"]` and a credit token in `_meta["projectstate/x402-credit"]`.
4. One payment buys credit ($0.10 on a session-based connection, $0.01 minimum per call without one). Later calls draw from it automatically on a session, or by sending the credit token as `Authorization: Bearer xc_…`. Unused credit stays on your wallet address.

Works with any x402-capable client: `x402-mcp` (`withPayment()`) for the Vercel AI SDK, the `x402` Python package, or the 30-line handshake in `scripts/x402_agent_demo.py`.

```python
from eth_account import Account
from mcp import Client
from x402 import x402Client
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired

x = x402Client(); register_exact_evm_client(x, EthAccountSigner(Account.from_key(KEY)))
async with Client("https://projectstate.online/mcp") as mcp:
    r = await mcp.call_tool("project_open", {"project": "my-app"})       # -> PaymentRequired
    pay = (await x.create_payment_payload(PaymentRequired(**r.structured_content))).model_dump(by_alias=True, exclude_none=True)
    r = await mcp.call_tool("project_open", {"project": "my-app"}, meta={"x402/payment": pay})
    token = r.meta["projectstate/x402-credit"]                          # reuse on later calls
```

## What is under the hood

Python 3.12, the official MCP Python SDK (v2), SQLite + FTS5 (bm25, no vectors), the official `x402` package against the Coinbase facilitator on Base mainnet. One process, one SQLite file, an admin dashboard with per-tool latency, error messages, a full audit ledger and a retrieval-quality meter (zero-hit and re-query rates) that says when keyword search stops being enough.

Docs: https://projectstate.online/docs · Registry: `online.projectstate/projectstate` · License: MIT

## Run your own

`deploy/install.sh` sets up systemd, nginx, fail2ban, hourly SQLite backups and unattended upgrades on a small Ubuntu VPS; `deploy/env.example` lists every setting; `deploy/x402_live_setup.sh` switches the x402 rail to Base mainnet with Coinbase CDP keys. `pytest tests/` runs 33 tests including a mock x402 facilitator that verifies real EIP-712 signatures.
