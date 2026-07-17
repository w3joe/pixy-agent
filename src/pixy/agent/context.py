"""Builds system instruction + conversation contents for the LLM."""

from __future__ import annotations

from pathlib import Path

from pixy.agent.history import ChatHistory
from pixy.memory.store import MemoryStore


class ContextBuilder:
    def __init__(self, core_md_path: Path, memory: MemoryStore) -> None:
        self.core_md_path = core_md_path
        self.memory = memory
        self._core_cache: str | None = None

    def load_core(self) -> str:
        if self._core_cache is None:
            self._core_cache = self.core_md_path.read_text(encoding="utf-8")
        return self._core_cache

    def reload_core(self) -> None:
        self._core_cache = None

    def memory_index_snippet(self, limit: int = 40) -> str:
        files = self.memory.list_files()
        if not files:
            return ""
        shown = files[:limit]
        extra = len(files) - len(shown)
        lines = ["## Memory index", "Available memory files:"]
        lines.extend(f"- {f}" for f in shown)
        if extra > 0:
            lines.append(f"- …and {extra} more (use memory_list)")
        return "\n".join(lines)

    def system_instruction(self) -> str:
        parts = [self.load_core()]
        index = self.memory_index_snippet()
        if index:
            parts.append(index)
        return "\n\n".join(parts)

    def contents_for(self, history: ChatHistory) -> list[dict]:
        return history.as_gemini_contents()
