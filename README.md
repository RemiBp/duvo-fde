# Korral StoreLink MCP — FDE Project

Custom [Model Context Protocol](https://modelcontextprotocol.io) server that lets a Duvo agent perform a Korral category buyer's job on top of StoreLink. The server runs inside Korral's private GCP network over **stdio JSON-RPC** — no inbound TCP ports.

**Context:** Korral operates ~180 stores and ~18k SKUs. StoreLink is the internal inventory and replenishment API. This pilot stubs StoreLink with representative butter-SKU data; the agent-facing contract is production-grade.

---

## MCP tools (Step 1)

Three granular primitives — not a monolith. Every store-scoped call requires a per-store `auth_key` from Korral IT's vault.

| Tool | Purpose | Inputs | Returns |
|------|---------|--------|---------|
| `get_store_inventory` | Current on-hand stock for one SKU at one store | `store_id`, `sku`, `auth_key` | `store_id`, `sku`, `on_hand` |
| `get_store_pos_24h` | Last 24h POS volume for one SKU | `store_id`, `sku`, `auth_key` | `store_id`, `sku`, `pos_transactions_24h` |
| `create_replenishment_order` | Raise a replenishment order | `store_id`, `sku`, `quantity`, `auth_key` | `status`, `order_id`, `quantity_dispatched` |

Docstrings describe return fields explicitly so the LLM does not hallucinate response shape.

---

## What we deliberately omit

| Omitted | Why |
|---------|-----|
| **HTTP passthrough** | Raw StoreLink HTTP would bypass buyer semantics and expose internal API surface to the agent. |
| **Bulk / all-store scans** | Pollutes LLM context; buyers work store-by-store on targeted SKUs. |
| **Monolithic `analyze_and_order()`** | Hides reasoning; the agent chains lookups and decides like a human buyer. |
| **Server-side gap formula** | Gap logic (`pos_transactions_24h − on_hand > 6`) lives in agent reasoning, not a hidden smart endpoint. |

---

## Demo outcome — SKU 8847291 (Madeta butter 250g)

Official buyer task: check stores **47** and **102**; order when gap exceeds **6** units.

`gap = pos_transactions_24h − on_hand`

| Store | On-hand | POS 24h | Gap | Order? |
|-------|---------|---------|-----|--------|
| **47** | 12 | 20 | **+8** | **Yes** — `KRL-ORD-47-8847291`, qty 8 |
| **102** | 15 | 17 | **+2** | **No** — within threshold |

---

## Observability — dual audience (Step 3)

| Audience | Channel | Format |
|----------|---------|--------|
| **FDE / SRE** (11 PM debugging) | `stderr` | `[FDE-DEBUG]` structured logs via `logging.basicConfig` |
| **Category buyer** (next-morning review) | `stderr` | `[BUYER AUDIT]` JSON via `log_buyer_audit()` |

**stdout is sacred** — reserved for MCP JSON-RPC only. No `print()` in server code.

---

## Secrets & auth (Step 4)

`enforce_security_boundary(store_id, auth_key)` runs on every tool call before business logic.

| Store | Vault key |
|-------|-----------|
| 47 | `key_store_47_valid` |
| 102 | `key_store_102_valid` |

**Failure modes (fail closed):**

| Condition | Exception | Code |
|-----------|-----------|------|
| Unknown `store_id` | `ValueError` | `CRITICAL_AUTH_FAILURE` |
| `auth_key == rotate_trigger_key` | `PermissionError` | `API_KEY_ROTATED` (weekly rotation mid-flight) |
| Token mismatch | `PermissionError` | `UNAUTHORIZED` |

In production, Korral IT mounts per-store keys from GCP Secret Manager. This pilot uses an in-memory `VAULT_CREDENTIAL_REGISTRY` in `server.py`.

---

## How to run

### Prerequisites

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires **Python 3.10+** (MCP SDK dependency).

### Integration harness (Step 2 + Step 4 proof)

```bash
python3 test_harness.py 2>&1
```

`2>&1` merges stderr so FDE logs and `[BUYER AUDIT]` lines appear alongside harness output. Expects order on store 47 only, security tests for rotation and unknown store.

### MCP server (stdio)

```bash
python3 server.py
```

Spawned by the Duvo worker or Cursor MCP client with stdin/stdout pipes. Example Cursor config:

```json
{
  "mcpServers": {
    "korral-storelink": {
      "command": "${workspaceFolder}/.venv/bin/python3",
      "args": ["${workspaceFolder}/server.py"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

### Docker (Korral IT deployment)

```bash
docker build -t duvo-korral-mcp .
docker run -i --rm duvo-korral-mcp
```

Non-root `duvouser` / `duvogroup`, `PYTHONUNBUFFERED=1`, stdio entrypoint — see `Dockerfile` and `DEPLOYMENT.md`.

---

## Project layout

| File | Role |
|------|------|
| `server.py` | MCP server — tools, auth, observability, stub data |
| `test_harness.py` | Buyer task simulation + security tests |
| `Dockerfile` | Non-root container image for GCP |
| `DEPLOYMENT.md` | How Korral IT runs the server in isolated GCP |
| `requirements.txt` | `mcp`, `pydantic` |
