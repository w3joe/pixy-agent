"""Smoke-test Aspire API credentials + read endpoints.

Usage (from repo root, with ASPIRE_* in .env):

    uv run python scripts/smoke_aspire.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing skills/aspire/sdk the same way the skill does.
_ASPIRE = Path(__file__).resolve().parents[1] / "skills" / "aspire"
sys.path.insert(0, str(_ASPIRE))

from pixy.config import get_settings  # noqa: E402
from sdk.client import AspireClient, AspireError  # noqa: E402


def main() -> int:
    get_settings.cache_clear()
    settings = get_settings()
    if not settings.aspire_client_id or not settings.aspire_client_secret:
        print("ASPIRE_CLIENT_ID / ASPIRE_CLIENT_SECRET not set in .env")
        return 1

    print(f"base_url={settings.aspire_base_url}")
    try:
        with AspireClient(
            client_id=settings.aspire_client_id,
            client_secret=settings.aspire_client_secret,
            base_url=settings.aspire_base_url,
        ) as client:
            token = client.login()
            print(f"login=ok token_prefix={token[:8]}…")

            accounts = client.list_accounts()
            rows = accounts.get("data") or []
            print(f"accounts={len(rows)}")
            for row in rows[:5]:
                if not isinstance(row, dict):
                    continue
                aid = row.get("id")
                cur = row.get("currency_code")
                bal = row.get("available_balance")
                print(f"  - {aid} {cur} balance={bal}")
                if aid:
                    bal_payload = client.get_balance(str(aid))
                    data = bal_payload.get("data") or bal_payload
                    print(f"    balance_endpoint={data}")

            tx = client.list_transactions(per_page=5)
            tx_rows = tx.get("data") or []
            print(f"transactions_sample={len(tx_rows)}")
            for row in tx_rows[:5]:
                if isinstance(row, dict):
                    print(f"  - {row.get('id') or row}")
    except AspireError as exc:
        print(f"aspire_error: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}")
        return 3

    print("smoke_aspire=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
