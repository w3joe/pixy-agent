"""Local FastAPI status surface."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, Query

from pixy.config import Settings
from pixy.logging.setup import query_logs


def create_app(
    *,
    settings: Settings,
    started_at: datetime,
    status_provider: Callable[[], dict[str, Any]],
) -> FastAPI:
    app = FastAPI(title="Pixy", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        uptime = (datetime.now(tz=timezone.utc) - started_at).total_seconds()
        payload = status_provider()
        if asyncio.iscoroutine(payload):
            payload = await payload
        payload["uptime_seconds"] = round(uptime, 1)
        payload["started_at"] = started_at.isoformat()
        return payload

    @app.get("/logs")
    def logs(
        since: str | None = Query(default=None),
        level: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        rows = query_logs(settings.logs_db_path, since=since, level=level, limit=limit)
        return {"count": len(rows), "logs": rows}

    return app


async def serve_api(app: FastAPI, host: str, port: int) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
