"""Core agent loop: message → context → LLM → tools → reply."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from google.genai import types

from pixy.agent.context import ContextBuilder
from pixy.agent.history import HistoryStore
from pixy.config import Settings
from pixy.llm.client import LLMClient
from pixy.logging.setup import get_logger
from pixy.memory.store import MemoryStore
from pixy.skills.registry import SkillRegistry

log = get_logger("pixy.agent")

MAX_TOOL_ROUNDS = 8


class AgentLoop:
    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMClient,
        context: ContextBuilder,
        history: HistoryStore,
        memory: MemoryStore,
        skills: SkillRegistry,
        tool_handlers: dict[str, Callable[..., str]],
        tool_declarations: list[types.FunctionDeclaration],
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.context = context
        self.history = history
        self.memory = memory
        self.skills = skills
        self.tool_handlers = tool_handlers
        self._base_declarations = tool_declarations
        self.tz = ZoneInfo(settings.tz)

    def _all_declarations(self) -> list[types.FunctionDeclaration]:
        return [*self._base_declarations, *self.skills.declarations()]

    def _tools_config(self) -> list[types.Tool] | None:
        decls = self._all_declarations()
        if not decls:
            return None
        return [types.Tool(function_declarations=decls)]

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> str:
        if name in self.tool_handlers:
            try:
                return self.tool_handlers[name](**args)
            except Exception as exc:  # noqa: BLE001
                return f"error: {exc}"
        return self.skills.run(name, args)

    async def handle_message(self, chat_id: int | str, text: str) -> str:
        chat_key = str(chat_id)
        hist = self.history.get(chat_key)
        hist.add("user", text)

        system = self.context.system_instruction()
        now = datetime.now(tz=self.tz).strftime("%Y-%m-%d %H:%M %Z")
        system = f"{system}\n\n## Runtime\nCurrent time: {now}\nCurrent chat_id: {chat_id}\n"

        contents: list[Any] = self.context.contents_for(hist)
        tools = self._tools_config()

        for _round in range(MAX_TOOL_ROUNDS):
            response = await self.llm.generate(
                contents=contents,
                system_instruction=system,
                tools=tools,
            )

            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None:
                reply = "I couldn't generate a reply just now."
                hist.add("assistant", reply)
                await self._maybe_persist(chat_key)
                return reply

            parts = candidate.content.parts or []
            fn_calls = [p for p in parts if getattr(p, "function_call", None)]
            text_parts = [
                p.text for p in parts if getattr(p, "text", None) and not getattr(p, "function_call", None)
            ]

            if not fn_calls:
                reply = "\n".join(t for t in text_parts if t).strip() or "(empty reply)"
                hist.add("assistant", reply)
                await self._maybe_persist(chat_key)
                return reply

            # Append model turn, then tool responses.
            contents.append(candidate.content)
            function_response_parts: list[types.Part] = []
            for part in fn_calls:
                fc = part.function_call
                name = fc.name or ""
                args = dict(fc.args or {})
                log.info("tool_call", name=name, args=args, chat_id=chat_key)
                result = self._dispatch_tool(name, args)
                function_response_parts.append(
                    types.Part.from_function_response(
                        name=name,
                        response={"result": result},
                    )
                )
            contents.append(types.Content(role="user", parts=function_response_parts))

        reply = "I hit the tool-call limit for this turn — try again with a narrower ask."
        hist.add("assistant", reply)
        await self._maybe_persist(chat_key)
        return reply

    async def _maybe_persist(self, chat_id: str) -> None:
        hist = self.history.get(chat_id)
        if hist.turns_since_persist < self.settings.history_persist_every:
            return
        snapshot = hist.snapshot_text()
        try:
            summary_response = await self.llm.generate(
                contents=[
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": (
                                    "Summarise this chat segment for long-term memory. "
                                    "Keep facts, decisions, open loops, names, and times. "
                                    "Use short bullets.\n\n"
                                    f"{snapshot}"
                                )
                            }
                        ],
                    }
                ],
                system_instruction="You compress chat history into durable notes.",
            )
            summary = (summary_response.text or "").strip()
            if not summary:
                return
            stamp = datetime.now(tz=self.tz).isoformat()
            block = f"\n## Summary @ {stamp}\n{summary}\n"
            self.memory.append(f"conversations/{chat_id}.md", block)
            hist.trim_after_persist(keep=4)
            log.info("history_persisted", chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001
            log.error("history_persist_failed", chat_id=chat_id, error=str(exc))
