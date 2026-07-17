"""Telegram speaker identity for per-user history isolation."""

from __future__ import annotations

from dataclasses import dataclass

from telegram import User


@dataclass(frozen=True)
class Speaker:
    user_id: str
    username: str | None = None
    display_name: str = "Unknown"

    @classmethod
    def from_telegram_user(cls, user: User | None) -> Speaker:
        if user is None:
            return cls(user_id="anon", display_name="Anonymous")
        display = (user.full_name or "").strip()
        if not display:
            parts = [user.first_name or "", user.last_name or ""]
            display = " ".join(p for p in parts if p).strip() or (
                f"@{user.username}" if user.username else f"user_{user.id}"
            )
        return cls(
            user_id=str(user.id),
            username=user.username,
            display_name=display,
        )

    @property
    def label(self) -> str:
        if self.username:
            return f"{self.display_name} (@{self.username})"
        return self.display_name

    def history_key(self, chat_id: int | str) -> str:
        return f"{chat_id}:{self.user_id}"

    def persist_filename(self, chat_id: int | str) -> str:
        """Filesystem-safe conversation summary path under conversations/."""
        return f"conversations/{chat_id}_{self.user_id}.md"
