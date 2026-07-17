"""APScheduler-backed proactive alerts (SGT) — one-shot and recurring cron."""

from __future__ import annotations

import csv
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from google.genai import types

from pixy.logging.setup import get_logger

log = get_logger("pixy.scheduler")

SendMessageFn = Callable[[int, str], Awaitable[None]]

CSV_FIELDS = ["id", "when", "chat_id", "message", "status", "kind", "cron", "last_fired"]

# Module-level sender so APScheduler can pickle/call jobs across restarts.
_SEND_MESSAGE: SendMessageFn | None = None
_ALERTS_CSV: Path | None = None
_TZ_NAME = "Asia/Singapore"

_CRON_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$")


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


def _parse_cron(cron: str) -> tuple[str, str, str, str, str]:
    text = cron.strip()
    match = _CRON_RE.match(text)
    if not match:
        raise ValueError(
            "cron must be 5 fields: minute hour day month day_of_week "
            "(e.g. '0 9 * * 1-5' for weekdays 09:00 SGT)"
        )
    return match.groups()  # type: ignore[return-value]


def _normalize_row(row: dict[str, str | None]) -> dict[str, str]:
    return {
        "id": row.get("id") or "",
        "when": row.get("when") or "",
        "chat_id": row.get("chat_id") or "",
        "message": row.get("message") or "",
        "status": row.get("status") or "",
        "kind": row.get("kind") or "once",
        "cron": row.get("cron") or "",
        "last_fired": row.get("last_fired") or "",
    }


def _read_csv_rows() -> list[dict[str, str]]:
    if _ALERTS_CSV is None or not _ALERTS_CSV.exists():
        return []
    with _ALERTS_CSV.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [_normalize_row(row) for row in reader]


def _write_csv_rows(rows: list[dict[str, str]]) -> None:
    if _ALERTS_CSV is None:
        return
    _ALERTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with _ALERTS_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_normalize_row(row))


def _append_alert_csv(
    *,
    job_id: str,
    when: str,
    chat_id: int,
    message: str,
    kind: str,
    cron: str = "",
    status: str,
) -> None:
    rows = [r for r in _read_csv_rows() if r.get("id") != job_id]
    rows.append(
        {
            "id": job_id,
            "when": when,
            "chat_id": str(chat_id),
            "message": message,
            "status": status,
            "kind": kind,
            "cron": cron,
            "last_fired": "",
        }
    )
    _write_csv_rows(rows)


def _remove_alert_csv(job_id: str) -> None:
    rows = [r for r in _read_csv_rows() if r.get("id") != job_id]
    _write_csv_rows(rows)


def _touch_cron_last_fired(job_id: str) -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    rows = _read_csv_rows()
    changed = False
    for row in rows:
        if row.get("id") == job_id:
            row["status"] = "active"
            row["last_fired"] = now
            row["kind"] = row.get("kind") or "cron"
            changed = True
    if changed:
        _write_csv_rows(rows)


async def _fire_alert(
    chat_id: int,
    message: str,
    job_id: str,
    kind: str = "once",
) -> None:
    log.info("alert_fired", chat_id=chat_id, job_id=job_id, kind=kind)
    if _SEND_MESSAGE is not None:
        await _SEND_MESSAGE(chat_id, message)
    if kind == "cron":
        _touch_cron_last_fired(job_id)
    else:
        # One-shot: clear from memory after delivery.
        _remove_alert_csv(job_id)


class AlertScheduler:
    def __init__(self, jobs_db: Path, tz_name: str = "Asia/Singapore") -> None:
        self.tz = ZoneInfo(tz_name)
        self.tz_name = tz_name
        jobs_db.parent.mkdir(parents=True, exist_ok=True)
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

    def schedule_alert(
        self,
        chat_id: int,
        message: str,
        when: str | None = None,
        cron: str | None = None,
    ) -> str:
        when_text = (when or "").strip() or None
        cron_text = (cron or "").strip() or None
        if not when_text and not cron_text:
            return "error: provide `when` (one-shot ISO) or `cron` (recurring 5-field)"
        if when_text and cron_text:
            return "error: provide either `when` or `cron`, not both"

        if cron_text:
            return self._schedule_cron(int(chat_id), message, cron_text)
        assert when_text is not None
        return self._schedule_once(int(chat_id), message, when_text)

    def _schedule_once(self, chat_id: int, message: str, when: str) -> str:
        try:
            run_at = datetime.fromisoformat(when)
        except ValueError as exc:
            return f"error: invalid when ISO datetime: {exc}"
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=self.tz)
        job_id = f"alert_{chat_id}_{int(run_at.timestamp())}"
        _remove_alert_csv(job_id)
        self.scheduler.add_job(
            _fire_alert,
            trigger="date",
            run_date=run_at,
            id=job_id,
            replace_existing=True,
            kwargs={
                "chat_id": chat_id,
                "message": message,
                "job_id": job_id,
                "kind": "once",
            },
        )
        _append_alert_csv(
            job_id=job_id,
            when=run_at.isoformat(),
            chat_id=chat_id,
            message=message,
            kind="once",
            status="scheduled",
        )
        return f"scheduled one-shot {job_id} at {run_at.isoformat()}"

    def _schedule_cron(self, chat_id: int, message: str, cron: str) -> str:
        try:
            minute, hour, day, month, day_of_week = _parse_cron(cron)
        except ValueError as exc:
            return f"error: {exc}"

        digest = hashlib.sha1(f"{cron}|{message}".encode()).hexdigest()[:10]
        job_id = f"alert_{chat_id}_cron_{digest}"
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=self.tz,
        )
        _remove_alert_csv(job_id)
        self.scheduler.add_job(
            _fire_alert,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            kwargs={
                "chat_id": chat_id,
                "message": message,
                "job_id": job_id,
                "kind": "cron",
            },
        )
        _append_alert_csv(
            job_id=job_id,
            when="",
            chat_id=chat_id,
            message=message,
            kind="cron",
            cron=cron.strip(),
            status="active",
        )
        job = self.scheduler.get_job(job_id)
        next_run = job.next_run_time if job else None
        return (
            f"scheduled recurring {job_id} cron={cron.strip()!r} "
            f"tz={self.tz_name} next={next_run}"
        )

    def list_alerts(self) -> str:
        jobs = self.scheduler.get_jobs()
        if not jobs:
            return "(no active alerts)"
        csv_by_id = {r["id"]: r for r in _read_csv_rows()}
        lines = []
        for job in jobs:
            kind = (job.kwargs or {}).get("kind", "once")
            cron = csv_by_id.get(job.id, {}).get("cron", "")
            parts = [
                job.id,
                f"kind={kind}",
                f"next={job.next_run_time}",
            ]
            if cron:
                parts.append(f"cron={cron}")
            msg = (job.kwargs or {}).get("message", "")
            if msg:
                parts.append(f"message={msg!r}")
            lines.append("\t".join(parts))
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
                "Schedule a Telegram reminder. "
                "One-shot: pass `when` as ISO-8601 with SGT offset "
                "(e.g. 2026-07-18T10:00:00+08:00). "
                "Recurring: pass `cron` as 5-field cron in Asia/Singapore "
                "(e.g. '0 9 * * 1-5' = weekdays 09:00). "
                "Provide exactly one of `when` or `cron`."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "integer"},
                    "message": {"type": "string"},
                    "when": {
                        "type": "string",
                        "description": "One-shot ISO-8601 datetime with SGT offset",
                    },
                    "cron": {
                        "type": "string",
                        "description": (
                            "Recurring 5-field cron (min hour day month dow) in SGT"
                        ),
                    },
                },
                "required": ["chat_id", "message"],
                "additionalProperties": False,
            },
        ),
        types.FunctionDeclaration(
            name="list_alerts",
            description="List scheduled alerts (one-shot and recurring).",
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
    def schedule_alert(
        chat_id: int,
        message: str,
        when: str | None = None,
        cron: str | None = None,
    ) -> str:
        return scheduler.schedule_alert(
            chat_id=int(chat_id),
            message=message,
            when=when,
            cron=cron,
        )

    def list_alerts() -> str:
        return scheduler.list_alerts()

    def cancel_alert(id: str) -> str:  # noqa: A002 — matches tool schema
        return scheduler.cancel_alert(id)

    return {
        "schedule_alert": schedule_alert,
        "list_alerts": list_alerts,
        "cancel_alert": cancel_alert,
    }
