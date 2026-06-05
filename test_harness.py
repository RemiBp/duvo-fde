"""Step 2 buyer task + Step 4 security proof (imports server.py tool logic).

Run from project root with venv active:
    cd /path/to/duvo-fde
    source .venv/bin/activate
    python3 test_harness.py 2>&1
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr
from pathlib import Path

# Fix ImportError when launched outside project root.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from server import (  # noqa: E402
    create_replenishment_order,
    get_store_inventory,
    get_store_pos_24h,
)

TARGET_SKU = 8847291
TARGET_STORES = [47, 102]
VAULT_KEYS = {47: "key_store_47_valid", 102: "key_store_102_valid"}
THRESHOLD = 6


def _p(message: str) -> None:
    """Print with flush so stdout interleaves with stderr under 2>&1."""
    print(message, flush=True)


def run_integration_test_harness() -> None:
    orders_placed: list[int] = []
    buyer_audit_for_47 = False

    _p("\n" + "=" * 60)
    _p("DUVO AGENT — OFFICIAL BUYER TASK SIMULATION")
    _p("Task: SKU 8847291 @ stores 47, 102 | threshold = 6")
    _p("Gap formula: pos_transactions_24h - on_hand")
    _p("=" * 60)

    for store_id in TARGET_STORES:
        _p(f"\n[WORKFLOW] Store {store_id}")

        inv = json.loads(get_store_inventory(store_id, TARGET_SKU, VAULT_KEYS[store_id]))
        on_hand = inv["on_hand"]

        pos = json.loads(get_store_pos_24h(store_id, TARGET_SKU, VAULT_KEYS[store_id]))
        sold_24h = pos["pos_transactions_24h"]

        # Official rule: gap = pos_transactions_24h - on_hand; order only if gap > 6
        gap = sold_24h - on_hand
        _p(f"  Metrics: on_hand={on_hand} | pos_24h={sold_24h} | gap={gap}")

        if gap > THRESHOLD:
            _p(f"  ALERT: gap {gap} exceeds threshold {THRESHOLD} — placing order")
            stderr_capture = io.StringIO()
            with redirect_stderr(stderr_capture):
                order = json.loads(
                    create_replenishment_order(
                        store_id, TARGET_SKU, gap, VAULT_KEYS[store_id]
                    )
                )
            audit_text = stderr_capture.getvalue()
            if store_id == 47 and "[BUYER AUDIT]" in audit_text and '"STORE_ID": 47' in audit_text:
                buyer_audit_for_47 = True
            # Still emit captured stderr so 2>&1 shows audit + FDE logs
            sys.stderr.write(audit_text)
            sys.stderr.flush()

            orders_placed.append(store_id)
            _p(f"  SUCCESS: {order['order_id']} | qty={order['quantity_dispatched']}")
        else:
            _p(f"  OK: gap {gap} within threshold — no order")

    if orders_placed != [47]:
        _p(f"\nFAIL: expected order only for store 47, got stores {orders_placed}")
        sys.exit(1)

    if not buyer_audit_for_47:
        _p("\nFAIL: store 47 order did not emit [BUYER AUDIT] on stderr")
        sys.exit(1)

    _p("\n" + "=" * 60)
    _p("STEP 4 — SECURITY FAIL-SAFE TESTS")
    _p("=" * 60)

    _p("\n[TEST A] Mid-flight key rotation (store 47 + rotate_trigger_key)...")
    try:
        get_store_inventory(47, TARGET_SKU, "rotate_trigger_key")
        _p("  FAIL: should have raised PermissionError")
        sys.exit(1)
    except PermissionError as ex:
        if "API_KEY_ROTATED" not in str(ex):
            _p(f"  FAIL: expected API_KEY_ROTATED in: {ex}")
            sys.exit(1)
        _p(f"  PASS: {ex}")

    _p("\n[TEST B] Unmapped store (999)...")
    try:
        get_store_inventory(999, TARGET_SKU, "any_token")
        _p("  FAIL: should have raised ValueError")
        sys.exit(1)
    except ValueError as ex:
        if "CRITICAL_AUTH_FAILURE" not in str(ex):
            _p(f"  FAIL: expected CRITICAL_AUTH_FAILURE in: {ex}")
            sys.exit(1)
        _p(f"  PASS: {ex}")

    _p("\n" + "=" * 60)
    _p("HARNESS COMPLETE")
    _p("  Orders placed: store 47 only (qty=8)")
    _p("  Buyer audit:   [BUYER AUDIT] emitted for store 47 on stderr")
    _p("=" * 60 + "\n")


if __name__ == "__main__":
    run_integration_test_harness()
