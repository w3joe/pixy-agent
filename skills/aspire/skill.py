"""Aspire bank skill — read-only PixelPro accounts, balances, statements."""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pixy.config import get_settings
from sdk.client import AspireClient, AspireError

_PKG = Path(__file__).resolve().parent
_TEMPLATES = _PKG / "templates"
_SGT = ZoneInfo("Asia/Singapore")

SKILL = {
    "name": "aspire_bank",
    "description": (
        "Read-only access to PixelPro's Aspire bank accounts, balances, and "
        "transaction statements. Use action=accounts|balance|transactions|statement. "
        "Does NOT create transfers, cards, or any write operations. "
        "For statement/balance reports, prefer action=statement or include the "
        "returned Telegram HTML in your reply so it renders with parse_mode HTML."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["accounts", "balance", "transactions", "statement"],
                "description": "Which read operation to perform.",
            },
            "account_id": {
                "type": "string",
                "description": "Aspire account UUID (required for balance; optional filter for transactions).",
            },
            "start_date": {
                "type": "string",
                "description": "ISO 8601 start (e.g. 2026-07-01T00:00:00Z) for transactions/statement.",
            },
            "end_date": {
                "type": "string",
                "description": "ISO 8601 end for transactions/statement.",
            },
            "currency_code": {
                "type": "string",
                "description": "ISO 4217 currency filter (e.g. SGD).",
            },
            "page": {
                "type": "integer",
                "description": "Pagination page (1-based).",
            },
            "limit": {
                "type": "integer",
                "description": "Max transactions to include in a statement (default 50).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def run(
    action: str,
    account_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    currency_code: str | None = None,
    page: int | None = None,
    limit: int | None = None,
    **kwargs: Any,
) -> str:
    del kwargs
    try:
        client = _client()
    except AspireError as exc:
        return f"error: {exc}"

    try:
        with client:
            if action == "accounts":
                data = client.list_accounts(page=page)
                return json.dumps(_compact_accounts(data), ensure_ascii=False, indent=2)
            if action == "balance":
                if not account_id:
                    return "error: account_id is required for action=balance"
                data = client.get_balance(account_id)
                return json.dumps(data, ensure_ascii=False, indent=2)
            if action == "transactions":
                data = client.list_transactions(
                    start_date=start_date,
                    end_date=end_date,
                    currency_code=currency_code,
                    account_id=account_id,
                    page=page,
                    per_page=limit,
                )
                return json.dumps(_compact_transactions(data), ensure_ascii=False, indent=2)
            if action == "statement":
                return _render_statement(
                    client,
                    account_id=account_id,
                    start_date=start_date,
                    end_date=end_date,
                    currency_code=currency_code,
                    page=page,
                    limit=limit or 50,
                )
            return f"error: unknown action {action!r}"
    except AspireError as exc:
        return f"error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _client() -> AspireClient:
    settings = get_settings()
    return AspireClient(
        client_id=settings.aspire_client_id,
        client_secret=settings.aspire_client_secret,
        base_url=settings.aspire_base_url,
    )


def format_amount(raw: Any, currency: str = "") -> str:
    """Format Aspire integer minor units as major currency (value/100)."""
    try:
        minor = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    major = minor / 100.0
    body = f"{major:,.2f}"
    return f"{currency} {body}".strip() if currency else body


def _compact_accounts(payload: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "id": item.get("id"),
                "account_status": item.get("account_status"),
                "account_type": item.get("account_type"),
                "currency_code": item.get("currency_code"),
                "available_balance": item.get("available_balance"),
                "available_balance_display": format_amount(
                    item.get("available_balance"), str(item.get("currency_code") or "")
                ),
            }
        )
    return {"accounts": rows, "metadata": payload.get("metadata")}


def _compact_transactions(payload: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        rows.append(_tx_summary(item))
    return {"transactions": rows, "metadata": payload.get("metadata")}


def _tx_summary(item: dict[str, Any]) -> dict[str, Any]:
    currency = str(
        item.get("currency_code")
        or item.get("currency")
        or item.get("source_currency")
        or ""
    )
    amount = item.get("amount")
    if amount is None:
        amount = item.get("payment_amount")
    if amount is None:
        amount = item.get("source_amount")
    return {
        "id": item.get("id"),
        "datetime": item.get("transaction_datetime")
        or item.get("created_at")
        or item.get("initiated_at"),
        "description": item.get("description")
        or item.get("reference")
        or item.get("counterparty_name")
        or item.get("merchant_name"),
        "amount": amount,
        "amount_display": format_amount(amount, currency),
        "currency_code": currency,
        "type": item.get("type") or item.get("transaction_type"),
        "status": item.get("status") or item.get("transfer_status"),
    }


def _load_template(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def _render_balances_html(accounts_payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in accounts_payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        cur = str(item.get("currency_code") or "")
        name = ""
        details = item.get("debit_details") or []
        if isinstance(details, list) and details and isinstance(details[0], dict):
            name = str(details[0].get("account_name") or "")
        label = escape(name or cur or "Account")
        bal = escape(format_amount(item.get("available_balance"), cur))
        aid = escape(str(item.get("id") or ""))
        lines.append(f"• <b>{label}</b> ({escape(cur)}): <code>{bal}</code>\n  <code>{aid}</code>")

    as_of = datetime.now(_SGT).strftime("%Y-%m-%d %H:%M SGT")
    tpl = _load_template("balances.html")
    return tpl.format(
        account_lines="\n".join(lines) if lines else "<i>No accounts returned.</i>",
        as_of=escape(as_of),
    )


def _render_statement(
    client: AspireClient,
    *,
    account_id: str | None,
    start_date: str | None,
    end_date: str | None,
    currency_code: str | None,
    page: int | None,
    limit: int,
) -> str:
    accounts = client.list_accounts()
    balances_html = _render_balances_html(accounts)

    tx_payload = client.list_transactions(
        start_date=start_date,
        end_date=end_date,
        currency_code=currency_code,
        account_id=account_id,
        page=page,
        per_page=limit,
    )
    txs = [_tx_summary(t) for t in (tx_payload.get("data") or []) if isinstance(t, dict)]
    txs = txs[:limit]

    tx_lines: list[str] = []
    for t in txs:
        when = escape(str(t.get("datetime") or "—"))
        desc = escape(str(t.get("description") or "—"))
        amt = escape(str(t.get("amount_display") or "—"))
        tx_lines.append(f"• <code>{when}</code> {desc}: <b>{amt}</b>")


    period_bits = []
    if start_date:
        period_bits.append(f"from {escape(start_date)}")
    if end_date:
        period_bits.append(f"to {escape(end_date)}")
    period_line = " ".join(period_bits) if period_bits else "<i>Recent transactions</i>"

    account_line = (
        f"Account: <code>{escape(account_id)}</code>"
        if account_id
        else "<i>All accounts</i>"
    )
    meta = tx_payload.get("metadata") or {}
    total = meta.get("total")
    footer = f"Showing {len(txs)}" + (f" of {total}" if total is not None else "") + " transactions."

    statement = _load_template("statement.html").format(
        period_line=period_line,
        account_line=account_line,
        tx_lines="\n".join(tx_lines) if tx_lines else "<i>No transactions in range.</i>",
        footer=escape(footer),
    )
    # Lead with balances then statement so the model can forward HTML as-is.
    return f"{balances_html.strip()}\n\n{statement.strip()}"
