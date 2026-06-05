# Korral StoreLink MCP — FDE Project

Custom [Model Context Protocol](https://modelcontextprotocol.io) server that lets a Duvo agent perform a Korral category buyer's job on top of StoreLink. The server runs inside Korral's private GCP network over **stdio JSON-RPC** — no inbound TCP ports.

**Customer context:** Korral operates ~180 stores and ~18k SKUs. StoreLink is the internal inventory and replenishment API. This pilot stubs StoreLink with representative butter-SKU data; the agent-facing contract is production-grade.

---

## MCP tools (Step 1)

Three granular primitives — not a monolith. **Auth is server-side** (agent passes business args only).

| Tool | Purpose | Key inputs | Returns |
|------|---------|------------|---------|
| `get_store_inventory` | Current on-hand stock for one SKU at one store | `store_id`, `sku` | `on_hand` (units) |
| `get_store_pos_24h` | Last 24h POS volume for one SKU | `store_id`, `sku` | `pos_transactions_24h` (units sold) |
| `create_replenishment_order` | Raise a replenishment order | `store_id`, `sku`, `quantity` | `order_id`, `status`, `quantity_dispatched` |

Docstrings describe return fields explicitly so the LLM does not hallucinate response shape.

---

## What we deliberately omit

| Omitted | Why |
|---------|-----|
| **HTTP passthrough** | Exposing raw StoreLink HTTP would bypass buyer semantics and leak internal API surface to the agent. |
| **Bulk / all-store scans** | Pollutes LLM context; buyers work store-by-store on targeted SKUs. |
| **Monolithic `analyze_and_order()`** | Hides reasoning; the agent should chain lookups and decide like a human buyer. |
| **Server-side gap formula** | Gap logic (`pos_24h − on_hand > threshold`) lives in agent reasoning, not a hidden smart endpoint. |

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

Per-store keys load from `secrets/store-keys.json` (override with `STORE_KEYS_PATH`). The server resolves credentials internally via `resolve_store_key(store_id)` — **keys never appear on the MCP tool surface**.

| Store | Vault key (server-side only) |
|-------|------------------------------|
| 47 | `key_store_47_valid` |
| 102 | `key_store_102_valid` |

**Failure modes (fail closed):**

- Unknown `store_id` → `ValueError` (`CRITICAL_AUTH_FAILURE`)
- Stale key after vault reload → `PermissionError` (`API_KEY_ROTATED`) — simulates Korral IT weekly rotation mid-flight

Copy `secrets/store-keys.example.json` → `secrets/store-keys.json` for local dev. In GCP, Korral IT mounts the vault from Secret Manager.

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

`2>&1` merges stderr so FDE logs and `[BUYER AUDIT]` lines appear alongside harness output.

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
| `requirements.txt` | `mcp`, `pydantic` |
