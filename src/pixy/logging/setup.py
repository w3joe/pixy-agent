"""Structured logging to stdout and SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog


_DB_LOCK = threading.Lock()
_DB_PATH: Path | None = None


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            event TEXT NOT NULL,
            logger TEXT,
            payload TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
    conn.commit()


class SQLiteLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if _DB_PATH is None:
            return
        try:
            payload: dict[str, Any] = {}
            event = record.getMessage()
            if hasattr(record, "_structlog_payload"):
                payload = dict(getattr(record, "_structlog_payload"))
                event = str(payload.pop("event", event))
            level = record.levelname.lower()
            logger_name = record.name
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            with _DB_LOCK:
                conn = sqlite3.connect(_DB_PATH)
                try:
                    _ensure_table(conn)
                    conn.execute(
                        "INSERT INTO logs (ts, level, event, logger, payload) VALUES (?, ?, ?, ?, ?)",
                        (
                            ts,
                            level,
                            event,
                            logger_name,
                            json.dumps(payload, default=str),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            self.handleError(record)


def _sqlite_processor(
    logger: logging.Logger, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Attach structured payload onto the stdlib record via a side channel.

    structlog's ProcessorFormatter already renders for stdout; we stash a copy
    for SQLiteLogHandler by monkey-patching via a thread-local isn't needed —
    instead we write directly here when the DB path is set.
    """
    if _DB_PATH is None:
        return event_dict

    try:
        payload = {k: v for k, v in event_dict.items() if k != "event"}
        event = str(event_dict.get("event", ""))
        level = str(event_dict.get("level", method_name)).lower()
        logger_name = getattr(logger, "name", "pixy")
        ts = datetime.now(tz=timezone.utc).isoformat()
        with _DB_LOCK:
            conn = sqlite3.connect(_DB_PATH)
            try:
                _ensure_table(conn)
                conn.execute(
                    "INSERT INTO logs (ts, level, event, logger, payload) VALUES (?, ?, ?, ?, ?)",
                    (ts, level, event, logger_name, json.dumps(payload, default=str)),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass
    return event_dict


def setup_logging(db_path: Path, level: str = "INFO") -> None:
    global _DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _DB_PATH = db_path

    with _DB_LOCK:
        conn = sqlite3.connect(db_path)
        try:
            _ensure_table(conn)
        finally:
            conn.close()

    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _sqlite_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def query_logs(
    db_path: Path,
    *,
    since: str | None = None,
    level: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    if level:
        clauses.append("level = ?")
        params.append(level.lower())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT id, ts, level, event, logger, payload FROM logs {where} ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _DB_LOCK:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    results: list[dict[str, Any]] = []
    for row in rows:
        payload_raw = row[5]
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except json.JSONDecodeError:
            payload = {"raw": payload_raw}
        results.append(
            {
                "id": row[0],
                "ts": row[1],
                "level": row[2],
                "event": row[3],
                "logger": row[4],
                "payload": payload,
            }
        )
    return results


def get_logger(name: str = "pixy") -> Any:
    return structlog.get_logger(name)
