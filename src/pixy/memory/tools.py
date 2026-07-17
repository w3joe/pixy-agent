"""Memory tools exposed to the LLM."""

from __future__ import annotations

from typing import Any, Callable

from google.genai import types

from pixy.memory.store import MemoryStore


def memory_tool_declarations() -> list[types.FunctionDeclaration]:
    return [
        types.FunctionDeclaration(
            name="memory_list",
            description="List all files in the agent's memory directory.",
            parameters_json_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.FunctionDeclaration(
            name="memory_read",
            description="Read a memory file by relative path (e.g. contacts.md).",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Relative path under .memory/",
                    }
                },
                "required": ["file"],
                "additionalProperties": False,
            },
        ),
        types.FunctionDeclaration(
            name="memory_write",
            description="Create or overwrite a memory file.",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file", "content"],
                "additionalProperties": False,
            },
        ),
        types.FunctionDeclaration(
            name="memory_append",
            description="Append a line or block to a memory file.",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "string"},
                },
                "required": ["file", "line"],
                "additionalProperties": False,
            },
        ),
    ]


def build_memory_handlers(store: MemoryStore) -> dict[str, Callable[..., str]]:
    def memory_list() -> str:
        files = store.list_files()
        return "\n".join(files) if files else "(empty)"

    def memory_read(file: str) -> str:
        return store.read(file)

    def memory_write(file: str, content: str) -> str:
        return store.write(file, content)

    def memory_append(file: str, line: str) -> str:
        return store.append(file, line)

    return {
        "memory_list": memory_list,
        "memory_read": memory_read,
        "memory_write": memory_write,
        "memory_append": memory_append,
    }


def run_memory_tool(handlers: dict[str, Callable[..., str]], name: str, args: dict[str, Any]) -> str:
    handler = handlers.get(name)
    if handler is None:
        return f"unknown memory tool: {name}"
    try:
        return handler(**args)
    except Exception as exc:  # noqa: BLE001 — surface to LLM
        return f"error: {exc}"
