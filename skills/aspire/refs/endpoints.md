# Aspire API — PixelPro read reference

Official docs: https://docs.api.aspireapp.com/api#/

## Auth

- Method: API Keys → `POST /public/v1/login` with `grant_type=client_credentials`
- Header for all other calls: `Authorization: Bearer <access_token>`
- Tokens expire (`expires_in`, typically ~900s); re-login on expiry / 401
- Production base: `https://api.aspireapp.com/public/v1`

## Required scopes

- `Accounts:Read` — list accounts, balances
- `Transactions:Read` — list / view transactions (bank statements)

## Endpoints used by this skill

| Action | Method | Path |
|--------|--------|------|
| login | POST | `/login` |
| accounts | GET | `/accounts` |
| balance | GET | `/accounts/{id}/balance` |
| transactions / statement | GET | `/transactions` |

Transaction filters commonly include `start_date`, `end_date`, `currency_code`, `page` (ISO 8601 timestamps).

## Amounts

Aspire responses often use **integer minor units** (e.g. cents). This skill formats display amounts as `value / 100` with 2 decimal places. If a live payload looks like major units, adjust `format_amount` in `skill.py`.

## Ops

- **IP whitelist:** outbound IPs of the host running Pixy must be whitelisted in Aspire (production).
- Rate limits (per endpoint): GET &lt; ~100 RPM; 429 → back off.
- Never commit `ASPIRE_CLIENT_ID` / `ASPIRE_CLIENT_SECRET`.
