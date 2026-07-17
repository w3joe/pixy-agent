"""Smoke-test free + Vertex Gemini backends.

Usage (from repo root, with .env populated):

    uv run python scripts/smoke_llm.py
    uv run python scripts/smoke_llm.py --force vertex
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pixy.config import get_settings
from pixy.llm.client import LLMClient, VertexNotConfiguredError
from pixy.llm.rotator import Backend
from pixy.logging.setup import setup_logging


async def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Pixy LLM backends")
    parser.add_argument(
        "--force",
        choices=["free", "vertex"],
        default=None,
        help="Force a specific backend",
    )
    args = parser.parse_args()

    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    setup_logging(settings.logs_db_path)

    print(f"vertex_enabled={settings.vertex_enabled}")
    client = LLMClient(settings)
    force = Backend(args.force) if args.force else None
    try:
        response = await client.generate(
            contents="Reply with exactly: pong",
            system_instruction="You are a connectivity probe. Reply with one word only.",
            force_backend=force,
        )
    except VertexNotConfiguredError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    text = (response.text or "").strip()
    stats = await client.stats()
    print(f"response={text!r}")
    print(
        f"stats free={stats.free_calls} vertex={stats.vertex_calls} "
        f"rpm_window={stats.free_rpm_window} rpd={stats.free_rpd_today}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
