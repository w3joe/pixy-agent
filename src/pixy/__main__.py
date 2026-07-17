"""Pixy entrypoint — starts Telegram gateway, scheduler, and API."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pixy.agent.context import ContextBuilder
from pixy.agent.history import HistoryStore
from pixy.agent.loop import AgentLoop
from pixy.api.server import create_app, serve_api
from pixy.config import REPO_ROOT, get_settings
from pixy.gateway.telegram import TelegramGateway
from pixy.llm.client import LLMClient
from pixy.logging.setup import get_logger, setup_logging
from pixy.memory.store import MemoryStore
from pixy.memory.tools import build_memory_handlers, memory_tool_declarations
from pixy.scheduler.alerts import (
    AlertScheduler,
    alert_tool_declarations,
    build_alert_handlers,
    configure_alert_runtime,
)
from pixy.skills.loader import SkillLoader
from pixy.skills.registry import SkillRegistry

log = get_logger("pixy")

PID_PATH = REPO_ROOT / "data" / "pixy.pid"
LOG_PATH = REPO_ROOT / "data" / "pixy.log"
_STOP_WAIT_SECS = 10.0


async def run() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    setup_logging(settings.logs_db_path)
    started_at = datetime.now(tz=timezone.utc)

    memory = MemoryStore(settings.memory_dir)
    llm = LLMClient(settings)
    context = ContextBuilder(settings.core_md_path, memory)
    history = HistoryStore(max_messages=settings.history_max_messages)
    skills = SkillRegistry(settings.skills_dir)
    skills.load_all()
    skill_loader = SkillLoader(skills)

    alert_scheduler = AlertScheduler(settings.jobs_db_path, tz_name=settings.tz)

    # Agent + telegram are wired after handlers exist; send_message set after gateway start.
    memory_handlers = build_memory_handlers(memory)
    alert_handlers = build_alert_handlers(alert_scheduler)
    tool_handlers = {**memory_handlers, **alert_handlers}
    tool_declarations = [
        *memory_tool_declarations(),
        *alert_tool_declarations(),
    ]

    agent = AgentLoop(
        settings=settings,
        llm=llm,
        context=context,
        history=history,
        memory=memory,
        skills=skills,
        tool_handlers=tool_handlers,
        tool_declarations=tool_declarations,
    )
    gateway = TelegramGateway(
        settings.telegram_bot_token,
        agent,
        webhook_url=settings.telegram_webhook_url,
        webhook_secret=settings.telegram_webhook_secret,
    )

    configure_alert_runtime(
        send_message=gateway.send_message,
        alerts_csv=settings.memory_dir / "alerts.csv",
        tz_name=settings.tz,
    )

    async def status_provider() -> dict:
        stats = await llm.stats()
        return {
            "model": settings.gemini_model,
            "telegram_mode": "webhook",
            "webhook_url": settings.telegram_webhook_url,
            "key_usage": {
                "free_calls": stats.free_calls,
                "vertex_calls": stats.vertex_calls,
                "free_rpm_window": stats.free_rpm_window,
                "free_rpd_today": stats.free_rpd_today,
                "rpd_date": stats.rpd_date,
            },
            "active_jobs": alert_scheduler.active_job_count(),
            "loaded_skills": skills.skill_names,
        }

    app = create_app(
        settings=settings,
        started_at=started_at,
        status_provider=status_provider,
    )
    gateway.mount(app)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _ask_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _ask_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _ask_stop())

    skill_loader.start(loop)
    alert_scheduler.start()

    # Start HTTP first so Telegram can reach the webhook as soon as it is registered.
    api_task = asyncio.create_task(
        serve_api(app, settings.api_host, settings.api_port),
        name="pixy-api",
    )
    await asyncio.sleep(0.3)
    await gateway.start()

    log.info(
        "pixy_started",
        api=f"http://{settings.api_host}:{settings.api_port}",
        webhook=settings.telegram_webhook_url,
        skills=skills.skill_names,
    )

    await stop_event.wait()
    log.info("pixy_shutting_down")

    api_task.cancel()
    try:
        await api_task
    except asyncio.CancelledError:
        pass

    skill_loader.stop()
    alert_scheduler.shutdown()
    await gateway.stop()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid(path: Path = PID_PATH) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _write_pid(pid: int, path: Path = PID_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")


def _clear_pid(path: Path = PID_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _running_pid() -> int | None:
    pid = _read_pid()
    if pid is None:
        return None
    if _pid_alive(pid):
        return pid
    _clear_pid()
    return None


def cmd_run() -> int:
    asyncio.run(run())
    return 0


def cmd_start() -> int:
    existing = _running_pid()
    if existing is not None:
        print(f"pixy already running (pid {existing})", file=sys.stderr)
        return 1

    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_PATH, "a", encoding="utf-8")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pixy", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(REPO_ROOT),
            start_new_session=True,
        )
    finally:
        log_file.close()

    _write_pid(proc.pid)
    print(f"pixy started (pid {proc.pid})")
    print(f"logs: {LOG_PATH}")
    return 0


def cmd_stop() -> int:
    pid = _read_pid()
    if pid is None:
        print("pixy is not running")
        return 0
    if not _pid_alive(pid):
        _clear_pid()
        print("pixy is not running (stale pid file removed)")
        return 0

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_WAIT_SECS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    else:
        print(f"pixy did not exit within {_STOP_WAIT_SECS:.0f}s (pid {pid})", file=sys.stderr)
        return 1

    _clear_pid()
    print(f"pixy stopped (pid {pid})")
    return 0


def cmd_status() -> int:
    pid = _running_pid()
    if pid is None:
        print("pixy is stopped")
        return 1
    print(f"pixy is running (pid {pid})")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pixy", description="Pixy Telegram assistant")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="Run in the foreground (default)")
    sub.add_parser("start", help="Start in the background")
    sub.add_parser("stop", help="Stop the background process")
    sub.add_parser("status", help="Show whether the background process is running")

    args = parser.parse_args(argv)
    command = args.command or "run"

    handlers = {
        "run": cmd_run,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
    }
    raise SystemExit(handlers[command]())


if __name__ == "__main__":
    main()
