"""APScheduler-backed proactive alerts (SGT)."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google.genai import types

from pixy.logging.setup import get_logger

log = get_logger("pixy.scheduler")

SendMessageFn = Callable[[int, str], Awaitable[None]]

# Module-level sender so APScheduler can pickle/call jobs across restarts.
_SEND_MESSAGE: SendMessageFn | None = None
_ALERTS_CSV: Path | None = None
_TZ_NAME = "Asia/Singapore"


def configure_alert_runtime(
    *,
    send_message: SendMessageFn,
    alerts_csv: Path,
    tz_name: str = "Asia/Singapore",
) -> None:
    global _SEND_MESSAGE, _ALERTS_CSV, _TZ_NAME
    _SEND_MESSAGE = send_message
    _ALERTS_CSV = alerts_csv
    _TZ_NAME = tz_name


async def _fire_alert(chat_id: int, message: str, job_id: str) -> None:
    log.info("alert_fired", chat_id=chat_id, job_id=job_id)
    if _SEND_MESSAGE is not None:
        await _SEND_MESSAGE(chat_id, message)
    _mark_alert_status(job_id, "fired")


def _mark_alert_status(job_id: str, status: str) -> None:
    if _ALERTS_CSV is None or not _ALERTS_CSV.exists():
        return
    rows: list[dict[str, str]] = []
    with _ALERTS_CSV.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or ["id", "when", "chat_id", "message", "status"]
        for row in reader:
            if row.get("id") == job_id:
                row["status"] = status
            rows.append(row)
    with _ALERTS_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _append_alert_csv(job_id: str, when: str, chat_id: int, message: str) -> None:
    if _ALERTS_CSV is None:
        return
    _ALERTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not _ALERTS_CSV.exists()
    with _ALERTS_CSV.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["id", "when", "chat_id", "message", "status"]
        )
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "id": job_id,
                "when": when,
                "chat_id": str(chat_id),
                "message": message,
                "status": "scheduled",
            }
        )


def _remove_alert_csv(job_id: str) -> None:
    if _ALERTS_CSV is None or not _ALERTS_CSV.exists():
        return
    rows: list[dict[str, str]] = []
    with _ALERTS_CSV.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or ["id", "when", "chat_id", "message", "status"]
        for row in reader:
            if row.get("id") != job_id:
                rows.append(row)
    with _ALERTS_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class AlertScheduler:
    def __init__(self, jobs_db: Path, tz_name: str = "Asia/Singapore") -> None:
        self.tz = ZoneInfo(tz_name)
        self.tz_name = tz_name
        jobs_db.parent.mkdir(parents=True, exist_ok=True)
        # Use SQLite URL; apscheduler's SQLAlchemy jobstore needs sqlalchemy installed.
        url = f"sqlite:///{jobs_db}"
        self.scheduler = AsyncIOScheduler(
            timezone=self.tz,
            jobstores={"default": SQLAlchemyJobStore(url=url)},
        )

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
            log.info("scheduler_started", tz=self.tz_name)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def schedule_alert(self, when: str, chat_id: int, message: str) -> str:
        run_at = datetime.fromisoformat(when)
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=self.tz)
        job_id = f"alert_{chat_id}_{int(run_at.timestamp())}"
        # Replace any prior CSV row for this id before re-adding.
        _remove_alert_csv(job_id)
        self.scheduler.add_job(
            _fire_alert,
            trigger="date",
            run_date=run_at,
            id=job_id,
            replace_existing=True,
            kwargs={"chat_id": int(chat_id), "message": message, "job_id": job_id},
        )
        _append_alert_csv(job_id, run_at.isoformat(), int(chat_id), message)
        return f"scheduled {job_id} at {run_at.isoformat()}"

    def list_alerts(self) -> str:
        jobs = self.scheduler.get_jobs()
        if not jobs:
            return "(no active alerts)"
        lines = []
        for job in jobs:
            lines.append(f"{job.id}\t next={job.next_run_time}\t args={job.kwargs}")
        return "\n".join(lines)

    def cancel_alert(self, alert_id: str) -> str:
        try:
            self.scheduler.remove_job(alert_id)
            _remove_alert_csv(alert_id)
            return f"cancelled {alert_id}"
        except Exception as exc:  # noqa: BLE001
            return f"error cancelling {alert_id}: {exc}"

    def active_job_count(self) -> int:
        return len(self.scheduler.get_jobs())


def alert_tool_declarations() -> list[types.FunctionDeclaration]:
    return [
        types.FunctionDeclaration(
            name="schedule_alert",
            description=(
                "Schedule a proactive Telegram reminder. "
                "`when` must be ISO-8601 with SGT offset, e.g. 2026-07-18T10:00:00+08:00."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "when": {"type": "string"},
                    "chat_id": {"type": "integer"},
                    "message": {"type": "string"},
                },
                "required": ["when", "chat_id", "message"],
                "additionalProperties": False,
            },
        ),
        types.FunctionDeclaration(
            name="list_alerts",
            description="List scheduled alerts.",
            parameters_json_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.FunctionDeclaration(
            name="cancel_alert",
            description="Cancel a scheduled alert by id.",
            parameters_json_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        ),
    ]


def build_alert_handlers(scheduler: AlertScheduler) -> dict[str, Callable[..., str]]:
    def schedule_alert(when: str, chat_id: int, message: str) -> str:
        return scheduler.schedule_alert(when, int(chat_id), message)

    def list_alerts() -> str:
        return scheduler.list_alerts()

    def cancel_alert(id: str) -> str:  # noqa: A002 — matches tool schema
        return scheduler.cancel_alert(id)

    return {
        "schedule_alert": schedule_alert,
        "list_alerts": list_alerts,
        "cancel_alert": cancel_alert,
    }
