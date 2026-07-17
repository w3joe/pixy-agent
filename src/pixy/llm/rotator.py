"""Free-tier / Vertex route selection with RPM + RPD windows."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

# Free-tier daily quota resets at midnight Pacific Time.
_PACIFIC = ZoneInfo("America/Los_Angeles")


class Backend(str, Enum):
    FREE = "free"
    VERTEX = "vertex"


@dataclass
class RotatorStats:
    free_calls: int = 0
    vertex_calls: int = 0
    free_rpm_window: int = 0
    free_rpd_today: int = 0
    rpd_date: str | None = None


@dataclass
class KeyRotator:
    rpm_limit: int
    rpd_limit: int
    _rpm_window: deque[float] = field(default_factory=deque)
    _rpd_count: int = 0
    _rpd_day: date | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    free_calls: int = 0
    vertex_calls: int = 0
    _force_vertex_until_rpd_reset: bool = False

    def _pacific_today(self) -> date:
        return datetime.now(tz=_PACIFIC).date()

    def _roll_rpd_if_needed(self) -> None:
        today = self._pacific_today()
        if self._rpd_day != today:
            self._rpd_day = today
            self._rpd_count = 0
            self._force_vertex_until_rpd_reset = False

    def _prune_rpm(self, now: float) -> None:
        cutoff = now - 60.0
        while self._rpm_window and self._rpm_window[0] < cutoff:
            self._rpm_window.popleft()

    async def choose(self) -> Backend:
        async with self._lock:
            self._roll_rpd_if_needed()
            now = datetime.now(tz=timezone.utc).timestamp()
            self._prune_rpm(now)

            if self._force_vertex_until_rpd_reset:
                return Backend.VERTEX
            if self._rpd_count >= self.rpd_limit:
                return Backend.VERTEX
            if len(self._rpm_window) >= self.rpm_limit:
                return Backend.VERTEX
            return Backend.FREE

    async def record_success(self, backend: Backend) -> None:
        async with self._lock:
            self._roll_rpd_if_needed()
            if backend is Backend.FREE:
                now = datetime.now(tz=timezone.utc).timestamp()
                self._prune_rpm(now)
                self._rpm_window.append(now)
                self._rpd_count += 1
                self.free_calls += 1
            else:
                self.vertex_calls += 1

    async def mark_free_exhausted(self) -> None:
        """After a free-tier 429, prefer Vertex until Pacific midnight."""
        async with self._lock:
            self._roll_rpd_if_needed()
            self._force_vertex_until_rpd_reset = True

    async def stats(self) -> RotatorStats:
        async with self._lock:
            self._roll_rpd_if_needed()
            now = datetime.now(tz=timezone.utc).timestamp()
            self._prune_rpm(now)
            return RotatorStats(
                free_calls=self.free_calls,
                vertex_calls=self.vertex_calls,
                free_rpm_window=len(self._rpm_window),
                free_rpd_today=self._rpd_count,
                rpd_date=self._rpd_day.isoformat() if self._rpd_day else None,
            )
