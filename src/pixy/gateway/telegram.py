"""Telegram gateway via HTTPS webhook (FastAPI-mounted)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from pixy.logging.setup import get_logger

if TYPE_CHECKING:
    from pixy.agent.loop import AgentLoop

log = get_logger("pixy.telegram")


class TelegramGateway:
    def __init__(
        self,
        token: str,
        agent: AgentLoop,
        *,
        webhook_url: str,
        webhook_secret: str = "",
    ) -> None:
        self.token = token
        self.agent = agent
        self.webhook_url = webhook_url.rstrip("/")
        self.webhook_secret = webhook_secret
        self.webhook_path = urlparse(self.webhook_url).path or "/telegram/webhook"
        if not self.webhook_path.startswith("/"):
            self.webhook_path = "/" + self.webhook_path

        self.application = Application.builder().token(token).build()
        self._bot_username: str | None = None

        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text)
        )

    def mount(self, app: FastAPI) -> None:
        """Register the Telegram webhook route on the FastAPI app."""

        @app.post(self.webhook_path)
        async def telegram_webhook(
            request: Request,
            x_telegram_bot_api_secret_token: str | None = Header(default=None),
        ) -> dict[str, str]:
            if self.webhook_secret and x_telegram_bot_api_secret_token != self.webhook_secret:
                raise HTTPException(status_code=403, detail="invalid webhook secret")

            try:
                payload: dict[str, Any] = await request.json()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail="invalid json") from exc

            if not isinstance(payload, dict) or "update_id" not in payload:
                raise HTTPException(status_code=400, detail="invalid update")

            try:
                update = Update.de_json(payload, self.application.bot)
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram_bad_update", error=str(exc))
                raise HTTPException(status_code=400, detail="invalid update") from exc

            if update is None:
                raise HTTPException(status_code=400, detail="invalid update")

            await self.application.process_update(update)
            return {"ok": "true"}

        log.info("telegram_webhook_mounted", path=self.webhook_path)

    async def start(self) -> None:
        await self.application.initialize()
        me = await self.application.bot.get_me()
        self._bot_username = (me.username or "").lower()
        await self.application.start()
        await self.application.bot.set_webhook(
            url=self.webhook_url,
            secret_token=self.webhook_secret or None,
            drop_pending_updates=True,
            allowed_updates=["message"],
        )
        log.info(
            "telegram_webhook_started",
            username=self._bot_username,
            url=self.webhook_url,
        )

    async def stop(self) -> None:
        try:
            await self.application.bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram_delete_webhook_failed", error=str(exc))
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
        log.info("telegram_stopped")

    async def send_message(self, chat_id: int, text: str) -> None:
        await self.application.bot.send_message(chat_id=chat_id, text=text)

    def _should_respond(self, update: Update) -> bool:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return False
        if chat.type == ChatType.PRIVATE:
            return True

        text = message.text or ""
        username = self._bot_username or ""
        mentioned = bool(username) and f"@{username}" in text.lower()

        replied_to_bot = False
        if message.reply_to_message and message.reply_to_message.from_user:
            replied_to_bot = bool(message.reply_to_message.from_user.is_bot) and (
                (message.reply_to_message.from_user.username or "").lower() == username
            )

        if message.entities:
            for ent in message.entities:
                if ent.type == "mention":
                    mention = text[ent.offset : ent.offset + ent.length].lstrip("@").lower()
                    if mention == username:
                        mentioned = True
                if ent.type == "text_mention" and ent.user and ent.user.is_bot:
                    if (ent.user.username or "").lower() == username:
                        mentioned = True

        return mentioned or replied_to_bot

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._should_respond(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not message.text:
            return

        text = message.text
        if self._bot_username:
            text = text.replace(f"@{self._bot_username}", "").replace(
                f"@{self._bot_username.capitalize()}", ""
            ).strip()

        chat_id = chat.id
        log.info("message_in", chat_id=chat_id, text=text[:200])
        await self._set_pending_reaction(context, chat_id, message.message_id, pending=True)
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            reply = await self.agent.handle_message(chat_id, text)
            await message.reply_text(reply)
        except Exception as exc:  # noqa: BLE001
            log.error("message_failed", chat_id=chat_id, error=str(exc))
            await message.reply_text(
                "Something went wrong on my side — try again in a moment."
            )
        finally:
            await self._set_pending_reaction(
                context, chat_id, message.message_id, pending=False
            )

    async def _set_pending_reaction(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        *,
        pending: bool,
    ) -> None:
        """👀 while working; clear when done. Best-effort (some chats disallow reactions)."""
        try:
            await context.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=["👀"] if pending else [],
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "reaction_failed",
                pending=pending,
                chat_id=chat_id,
                error=str(exc),
            )
