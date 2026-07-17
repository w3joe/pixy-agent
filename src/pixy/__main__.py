"""Pixy entrypoint — starts Telegram gateway, scheduler, and API."""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone

from pixy.agent.context import ContextBuilder
from pixy.agent.history import HistoryStore
from pixy.agent.loop import AgentLoop
from pixy.api.server import create_app, serve_api
from pixy.config import get_settings
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


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
