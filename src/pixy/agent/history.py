"""Per-user-per-chat rolling conversation history."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatHistory:
    max_messages: int = 40
    messages: list[dict[str, Any]] = field(default_factory=list)
    turns_since_persist: int = 0

    def add(self, role: str, text: str) -> None:
        self.messages.append({"role": role, "text": text})
        self.turns_since_persist += 1
        overflow = len(self.messages) - self.max_messages
        if overflow > 0:
            self.messages = self.messages[overflow:]

    def as_gemini_contents(self) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for msg in self.messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["text"]}]})
        return contents

    def snapshot_text(self) -> str:
        lines = []
        for msg in self.messages:
            lines.append(f"{msg['role'].upper()}: {msg['text']}")
        return "\n".join(lines)

    def trim_after_persist(self, keep: int = 4) -> None:
        if len(self.messages) > keep:
            self.messages = self.messages[-keep:]
        self.turns_since_persist = 0


class HistoryStore:
    """History keyed by `{chat_id}:{user_id}` (isolated per staff member in a group)."""

    def __init__(self, max_messages: int = 40) -> None:
        self.max_messages = max_messages
        self._chats: dict[str, ChatHistory] = {}

    def get(self, history_key: str) -> ChatHistory:
        if history_key not in self._chats:
            self._chats[history_key] = ChatHistory(max_messages=self.max_messages)
        return self._chats[history_key]
