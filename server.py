"""
Korral StoreLink MCP server — stdio JSON-RPC for Duvo category buyers.

Step 1: granular inventory / POS / replenishment tools.
Step 3: FDE traces + buyer audit on stderr only (stdout reserved for MCP).
Step 4: per-store vault with fail-closed auth on every tool call.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_KEYS_PATH = PROJECT_ROOT / "secrets" / "store-keys.json"
FALLBACK_KEYS_PATH = PROJECT_ROOT / "secrets" / "store-keys.example.json"

# Stub: keys ending with this suffix simulate StoreLink 401 after vault reload.
STALE_KEY_SUFFIX = "-stale"

# ---------------------------------------------------------------------------
# Step 3 — observability (stderr ONLY; stdout is MCP JSON-RPC)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [FDE-DEBUG] [Store: %(store_id)s] %(message)s",
    stream=sys.stderr,
    force=True,
)
logger = logging.getLogger("duvo-mcp-engine")


class _StoreContextFilter(logging.Filter):
    """Ensure %(store_id)s is always defined for the FDE log format."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "store_id"):
            record.store_id = "—"  # noqa: RUF001
        return True


logger.addFilter(_StoreContextFilter())


def log_buyer_audit(store_id: int, action: str, details: str) -> None:
    """Step 3: buyer-facing audit — structured JSON for morning review."""
    audit_entry = {
        "AUDIT_LOG": "BUYER_MORNING_REVIEW",
        "STORE_ID": store_id,
        "ACTION_EXECUTED": action,
        "HUMAN_READABLE_SUMMARY": details,
    }
    sys.stderr.write(f"\n[BUYER AUDIT] {json.dumps(audit_entry)}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Step 4 — per-store credential vault (Korral IT rotates weekly)
# ---------------------------------------------------------------------------


class KeyStore:
    """Per-store StoreLink API keys loaded from a mounted JSON vault."""

    def __init__(self, keys_path: Path) -> None:
        self._keys_path = keys_path
        self._keys: dict[str, dict[str, str]] = {}

    def load(self) -> None:
        path = self._keys_path
        if not path.is_file() and path == DEFAULT_KEYS_PATH and FALLBACK_KEYS_PATH.is_file():
            path = FALLBACK_KEYS_PATH

        if not path.is_file():
            self._keys = {}
            return

        raw = json.loads(path.read_text(encoding="utf-8"))
        next_keys: dict[str, dict[str, str]] = {}
        for store_id, value in raw.items():
            if isinstance(value, str):
                next_keys[store_id] = {"current": value}
            elif isinstance(value, dict) and isinstance(value.get("current"), str):
                next_keys[store_id] = dict(value)
        self._keys = next_keys

    def reload(self) -> None:
        self.load()
        logger.info(
            "secrets.reloaded",
            extra={
                "store_id": "—",  # noqa: RUF001
            },
        )

    def has_store(self, store_id: int) -> bool:
        return str(store_id) in self._keys

    def configured_store_ids(self) -> list[int]:
        ids: list[int] = []
        for key in self._keys:
            try:
                ids.append(int(key))
            except ValueError:
                continue
        return sorted(ids)

    def get_key(self, store_id: int) -> str | None:
        record = self._keys.get(str(store_id))
        if not record:
            return None
        return record.get("current")


def _keys_path() -> Path:
    raw = os.environ.get("STORE_KEYS_PATH", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else PROJECT_ROOT / path
    return DEFAULT_KEYS_PATH


VAULT = KeyStore(_keys_path())
VAULT.load()
logger.info(
    "secrets.loaded",
    extra={
        "store_id": "—",  # noqa: RUF001
    },
)


def fail_api_key_rotated(store_id: int) -> None:
    """Raise after StoreLink 401 persists post-reload (harness + production path)."""
    logger.warning(
        "Mid-flight API key rotation detected.",
        extra={"store_id": store_id},
    )
    raise PermissionError(
        f"API_KEY_ROTATED: Token for Store {store_id} expired mid-flight. "
        "Refresh from vault and retry."
    )


def fail_critical_auth(store_id: int) -> None:
    """Raise when no vault credential exists for the requested store."""
    logger.error(
        "Unauthorized store lookup — store missing from credential registry.",
        extra={"store_id": store_id},
    )
    raise ValueError(
        f"CRITICAL_AUTH_FAILURE: Store {store_id} missing from credential registry. "
        "Contact Korral IT to register store keys."
    )


def resolve_store_key(store_id: int) -> str:
    """
    Load per-store key from vault. Simulates rotation retry when key is stale:
    reload vault once, then fail closed with API_KEY_ROTATED.
    """
    key = VAULT.get_key(store_id)
    if key is None:
        fail_critical_auth(store_id)

    assert key is not None
    if key.endswith(STALE_KEY_SUFFIX):
        VAULT.reload()
        key = VAULT.get_key(store_id)
        if key is None or key.endswith(STALE_KEY_SUFFIX):
            fail_api_key_rotated(store_id)

    return key


# ---------------------------------------------------------------------------
# Stub StoreLink state (Step 1 + official buyer task demo)
# SKU 8847291 Madeta butter 250g — gap = pos_24h - on_hand
#   Store 47:  12 on-hand, 20 sold → gap 8  (order: gap > 6)
#   Store 102: 15 on-hand, 17 sold → gap 2  (no order)
# ---------------------------------------------------------------------------

STORELINK_DB: dict[int, dict[int, dict[str, int]]] = {
    47: {8847291: {"on_hand": 12, "pos_24h": 20}},
    102: {8847291: {"on_hand": 15, "pos_24h": 17}},
}

SKU_CATALOG: dict[int, str] = {
    8847291: "Madeta butter 250g",
}


def _sku_row(store_id: int, sku: int) -> dict[str, int]:
    return STORELINK_DB.get(store_id, {}).get(sku, {"on_hand": 0, "pos_24h": 0})


# ---------------------------------------------------------------------------
# Step 1 — MCP tool surface (importable by test_harness.py)
# ---------------------------------------------------------------------------

app = FastMCP("duvo-korral-storelink-server")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [FDE-DEBUG] [Store: %(store_id)s] %(message)s",
    stream=sys.stderr,
    force=True,
)
logger.addFilter(_StoreContextFilter())


@app.tool(
    name="get_store_inventory",
    description=(
        "Returns current on-hand stock for a SKU at a Korral store. "
        "Auth is server-side (per-store vault); agent passes store_id only. "
        "Returns JSON with fields: store_id (int), sku (int), on_hand (int units)."
    ),
)
def get_store_inventory(store_id: int, sku: int) -> str:
    """Look up on-hand quantity for one SKU at one store."""
    resolve_store_key(store_id)
    logger.info(f"Inventory lookup SKU {sku}", extra={"store_id": store_id})
    data = _sku_row(store_id, sku)
    return json.dumps({"store_id": store_id, "sku": sku, "on_hand": data["on_hand"]})


@app.tool(
    name="get_store_pos_24h",
    description=(
        "Returns last-24-hour POS transaction volume for a SKU at a store. "
        "Auth is server-side (per-store vault). "
        "Returns JSON with fields: store_id, sku, pos_transactions_24h (int units sold)."
    ),
)
def get_store_pos_24h(store_id: int, sku: int) -> str:
    """Look up 24h POS sold units for one SKU at one store."""
    resolve_store_key(store_id)
    logger.info(f"POS 24h lookup SKU {sku}", extra={"store_id": store_id})
    data = _sku_row(store_id, sku)
    return json.dumps(
        {
            "store_id": store_id,
            "sku": sku,
            "pos_transactions_24h": data["pos_24h"],
        }
    )


@app.tool(
    name="create_replenishment_order",
    description=(
        "Raises a replenishment order at StoreLink for a SKU quantity. "
        "Auth is server-side (per-store vault). Call only when gap "
        "(pos_24h − on_hand) exceeds the buyer threshold (typically 6 units). "
        "Returns JSON with status, order_id, quantity_dispatched."
    ),
)
def create_replenishment_order(store_id: int, sku: int, quantity: int) -> str:
    """Dispatch a replenishment order after auth and audit logging."""
    resolve_store_key(store_id)
    product = SKU_CATALOG.get(sku, f"SKU {sku}")
    logger.info(
        f"Replenishment order {quantity} units SKU {sku}",
        extra={"store_id": store_id},
    )
    log_buyer_audit(
        store_id=store_id,
        action="AUTOMATED_STOCK_REPLENISHMENT",
        details=(
            f"Ordered {quantity} units of SKU {sku} ({product}). "
            "Reason: deficit gap exceeded the 6-unit safety threshold."
        ),
    )
    return json.dumps(
        {
            "status": "ORDER_PROCESSED",
            "order_id": f"KRL-ORD-{store_id}-{sku}",
            "quantity_dispatched": quantity,
        }
    )


if __name__ == "__main__":
    app.run()
