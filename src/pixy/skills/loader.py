"""Watchdog-based hot reload for skills/."""

from __future__ import annotations

import asyncio
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from pixy.logging.setup import get_logger
from pixy.skills.registry import SkillRegistry

log = get_logger("pixy.skills.loader")


class _SkillEventHandler(FileSystemEventHandler):
    def __init__(self, registry: SkillRegistry, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.registry = registry
        self.loop = loop

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if path.suffix != ".py" or path.name.startswith("_"):
            return
        self.loop.call_soon_threadsafe(self._handle, path, event.event_type)

    def _handle(self, path: Path, event_type: str) -> None:
        log.info("skill_fs_event", path=str(path), event=event_type)
        if event_type == "deleted":
            self.registry.unload_file(path)
            return
        if path.exists():
            self.registry.load_file(path)


class SkillLoader:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self._observer: Observer | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        skills_dir = self.registry.skills_dir
        skills_dir.mkdir(parents=True, exist_ok=True)
        handler = _SkillEventHandler(self.registry, loop)
        observer = Observer()
        observer.schedule(handler, str(skills_dir), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer
        log.info("skills_watcher_started", path=str(skills_dir))

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
