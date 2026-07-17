"""Read-only Aspire API client for PixelPro bank data."""

from __future__ import annotations

import time
from typing import Any

import httpx

from sdk.auth import TokenState


class AspireError(Exception):
    """Aspire API or client configuration error."""


class AspireClient:
    """Sync client: login, accounts, balances, transactions."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: str = "https://api.aspireapp.com/public/v1",
        timeout: float = 30.0,
    ) -> None:
        if not client_id or not client_secret:
            raise AspireError("ASPIRE_CLIENT_ID and ASPIRE_CLIENT_SECRET are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._token: TokenState | None = None
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AspireClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def login(self) -> str:
        """Obtain a new access token via client_credentials."""
        url = f"{self.base_url}/login"
        resp = self._http.post(
            url,
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/json"},
        )
        self._raise_for_status(resp, "login")
        payload = resp.json()
        # Docs show a list with one token object.
        row = payload[0] if isinstance(payload, list) else payload
        if not isinstance(row, dict) or "access_token" not in row:
            raise AspireError(f"unexpected login response: {payload!r}")
        expires_in = float(row.get("expires_in") or 900)
        self._token = TokenState(
            access_token=str(row["access_token"]),
            expires_at=time.monotonic() + expires_in,
        )
        return self._token.access_token

    def _ensure_token(self) -> str:
        if self._token is None or not self._token.is_valid():
            return self.login()
        return self._token.access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._http.request(method, url, headers=self._headers(), params=params)
        if resp.status_code == 401 and retry_auth:
            self.login()
            resp = self._http.request(method, url, headers=self._headers(), params=params)
        self._raise_for_status(resp, path)
        if not resp.content:
            return {}
        return resp.json()

    @staticmethod
    def _raise_for_status(resp: httpx.Response, context: str) -> None:
        if resp.is_success:
            return
        detail = resp.text[:500]
        if resp.status_code == 401:
            raise AspireError(f"unauthorized ({context}): check API keys / IP whitelist")
        if resp.status_code == 403:
            raise AspireError(f"forbidden ({context}): missing scope or IP not whitelisted")
        if resp.status_code == 429:
            raise AspireError(f"rate limited ({context}): retry later")
        raise AspireError(f"Aspire {resp.status_code} on {context}: {detail}")

    def list_accounts(self, *, page: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        return self._request("GET", "/accounts", params=params or None)

    def get_balance(self, account_id: str) -> dict[str, Any]:
        if not account_id:
            raise AspireError("account_id is required")
        return self._request("GET", f"/accounts/{account_id}/balance")

    def list_transactions(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        currency_code: str | None = None,
        account_id: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if currency_code:
            params["currency_code"] = currency_code
        if account_id:
            params["account_id"] = account_id
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        return self._request("GET", "/transactions", params=params or None)
