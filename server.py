"""
Korral StoreLink MCP server — stdio JSON-RPC for Duvo category buyers.

Step 1: granular inventory / POS / replenishment tools.
Step 3: FDE traces + buyer audit on stderr only (stdout reserved for MCP).
Step 4: per-store vault with fail-closed auth on every tool call.
"""

from __future__ import annotations

import json
import logging
import sys
from mcp.server.fastmcp import FastMCP

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

VAULT_CREDENTIAL_REGISTRY: dict[int, str] = {
    47: "key_store_47_valid",
    102: "key_store_102_valid",
}

ROTATE_TRIGGER_KEY = "rotate_trigger_key"

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


def enforce_security_boundary(store_id: int, auth_key: str) -> None:
    """Step 4: fail closed on unknown store, rotation mid-flight, or bad token."""
    if store_id not in VAULT_CREDENTIAL_REGISTRY:
        logger.error(
            "Unauthorized store lookup — store missing from credential registry.",
            extra={"store_id": store_id},
        )
        raise ValueError(
            f"CRITICAL_AUTH_FAILURE: Store {store_id} missing from credential registry. "
            "Contact Korral IT to register store keys."
        )

    if auth_key == ROTATE_TRIGGER_KEY:
        logger.warning(
            "Mid-flight API key rotation detected.",
            extra={"store_id": store_id},
        )
        raise PermissionError(
            f"API_KEY_ROTATED: Token for Store {store_id} expired mid-flight. "
            "Refresh from vault and retry."
        )

    if VAULT_CREDENTIAL_REGISTRY[store_id] != auth_key:
        logger.error("Token mismatch for store.", extra={"store_id": store_id})
        raise PermissionError(f"UNAUTHORIZED: Invalid token for Store {store_id}.")


def _sku_row(store_id: int, sku: int) -> dict[str, int]:
    return STORELINK_DB.get(store_id, {}).get(sku, {"on_hand": 0, "pos_24h": 0})


# ---------------------------------------------------------------------------
# Step 1 — MCP tool surface (importable by test_harness.py)
# ---------------------------------------------------------------------------

app = FastMCP("duvo-korral-storelink-server")

# Re-apply stderr logging after FastMCP init (SDK may configure its own handlers).
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
        "Requires per-store auth_key from Korral IT vault. "
        "Returns JSON with fields: store_id (int), sku (int), on_hand (int units)."
    ),
)
def get_store_inventory(store_id: int, sku: int, auth_key: str) -> str:
    """Look up on-hand quantity for one SKU at one store."""
    enforce_security_boundary(store_id, auth_key)
    logger.info(f"Inventory lookup SKU {sku}", extra={"store_id": store_id})
    data = _sku_row(store_id, sku)
    return json.dumps({"store_id": store_id, "sku": sku, "on_hand": data["on_hand"]})


@app.tool(
    name="get_store_pos_24h",
    description=(
        "Returns last-24-hour POS transaction volume for a SKU at a store. "
        "Requires per-store auth_key. "
        "Returns JSON with fields: store_id, sku, pos_transactions_24h (int units sold)."
    ),
)
def get_store_pos_24h(store_id: int, sku: int, auth_key: str) -> str:
    """Look up 24h POS sold units for one SKU at one store."""
    enforce_security_boundary(store_id, auth_key)
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
        "Requires per-store auth_key. Call only when gap (pos_24h − on_hand) exceeds "
        "the buyer threshold (typically 6 units). "
        "Returns JSON with status, order_id, quantity_dispatched."
    ),
)
def create_replenishment_order(
    store_id: int, sku: int, quantity: int, auth_key: str
) -> str:
    """Dispatch a replenishment order after auth and audit logging."""
    enforce_security_boundary(store_id, auth_key)
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
