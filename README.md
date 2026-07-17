# Pixy

Telegram personal assistant for **PixelPro Studios** staff (AV / video / photo).

Runs on a Raspberry Pi: Telegram gateway + Gemini agent loop (free tier → Vertex fallback) + memory + hot-reloadable skills + SGT scheduler + local status API.

## Quick start

```bash
# 1. Clone & install
uv sync

# 2. Configure
cp .env.example .env
# Fill in GEMINI_FREE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_URL
# (optional: Vertex SA path/project, TELEGRAM_WEBHOOK_SECRET)

# 3. Smoke-test LLM backends
uv run python scripts/smoke_llm.py
uv run python scripts/smoke_llm.py --force vertex

# 4. Expose HTTPS to Telegram (example: Tailscale Funnel → local :8080)
#    tailscale funnel 8080
#    Then set TELEGRAM_WEBHOOK_URL=https://<magicdns>/telegram/webhook

# 5. Run
uv run python -m pixy
```

API: `http://<host>:8080/health`, `/status`, `/logs`  
Telegram webhook: `POST /telegram/webhook` (path taken from `TELEGRAM_WEBHOOK_URL`)

## Telegram webhook

Pixy no longer polls. Telegram POSTs updates to your public **HTTPS** URL.

1. Set `TELEGRAM_WEBHOOK_URL` to the full public URL (must be HTTPS).
2. Optionally set `TELEGRAM_WEBHOOK_SECRET` — Telegram sends it as `X-Telegram-Bot-Api-Secret-Token`.
3. Bind `API_HOST=0.0.0.0` (or terminate TLS on a proxy that forwards to Pixy).
4. On start, Pixy calls `setWebhook`; on stop it deletes the webhook.

Tailscale Funnel is the usual path on a Pi without opening home-router ports.

## Layout

| Path | Role |
|------|------|
| `core.md` | System personality / PixelPro context |
| `src/pixy/` | Application package |
| `skills/` | Hot-reloadable skill modules (`SKILL` + `run`) |
| `.memory/` | Persistent agent memory (gitignored) |
| `deploy/` | systemd unit + CI deploy notes |

## Skills

Drop a Python file in `skills/`:

```python
SKILL = {
    "name": "ping",
    "description": "Health-check skill that returns pong.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

def run(**kwargs) -> str:
    return "pong"
```

The watcher reloads on add/change — no process restart.

## Memory & alerts

Built-in tools (always available): `memory_read/write/append/list`, `schedule_alert`, `list_alerts`, `cancel_alert`.

Alerts use APScheduler with a SQLite jobstore (`data/jobs.sqlite`) and are mirrored to `.memory/alerts.csv`.

## Deploy (Pi + Tailscale)

1. Copy `deploy/pixy.service` to `/etc/systemd/system/pixy.service` (edit paths/user).
2. Place the Vertex service-account JSON on the Pi; set `GOOGLE_APPLICATION_CREDENTIALS` in `.env`.
3. `sudo systemctl enable --now pixy`
4. Wire GitHub secrets and use `.github/workflows/deploy.yml` (sourced from `deploy/deploy.yml`).

### GitHub secrets

- `TAILSCALE_AUTHKEY`
- `PI_SSH_HOST` / `PI_SSH_USER` / `PI_SSH_KEY`
- `PI_APP_PATH`

## Notes

- Free-tier RPM/RPD are env-configurable (`FREE_TIER_RPM`, `FREE_TIER_RPD`). RPD window follows Pacific midnight.
- Concurrent chats share an asyncio lock in the key rotator.
- `.memory/conversations/` grows over time — add a compaction skill later if the SD card fills up.
