"""Unified Gemini client with free / Vertex backend rotation."""

from __future__ import annotations

import os
from typing import Any

from google import genai
from google.genai import types

from pixy.config import Settings
from pixy.llm.rotator import Backend, KeyRotator, RotatorStats
from pixy.logging.setup import get_logger

log = get_logger("pixy.llm")


class VertexNotConfiguredError(RuntimeError):
    """Raised when Vertex is required but SA credentials are missing."""


def _is_rate_limited(exc: BaseException) -> bool:
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    markers = ("429", "resource_exhausted", "resource exhausted", "rate limit", "quota")
    return any(m in text or m in name for m in markers)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.gemini_model
        self.rotator = KeyRotator(
            rpm_limit=settings.free_tier_rpm,
            rpd_limit=settings.free_tier_rpd,
        )
        self._free = genai.Client(api_key=settings.gemini_free_api_key)
        self._vertex: genai.Client | None = None

        if settings.vertex_enabled:
            log.info(
                "vertex_configured",
                project=settings.vertex_project_id,
                location=settings.vertex_location,
            )
        else:
            log.warning(
                "vertex_not_configured",
                hint=(
                    "Set GOOGLE_APPLICATION_CREDENTIALS to a real service-account "
                    "JSON path and VERTEX_PROJECT_ID to enable paid fallback."
                ),
            )

    def _get_vertex(self) -> genai.Client:
        if self._vertex is not None:
            return self._vertex
        if not self.settings.vertex_enabled:
            raise VertexNotConfiguredError(
                "Vertex AI is not configured. Set GOOGLE_APPLICATION_CREDENTIALS "
                "to an existing service-account JSON file and VERTEX_PROJECT_ID "
                "in .env (do not leave /path/to/service-account.json)."
            )
        creds = self.settings.google_application_credentials
        assert creds is not None
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds)
        self._vertex = genai.Client(
            vertexai=True,
            project=self.settings.vertex_project_id,
            location=self.settings.vertex_location,
        )
        return self._vertex

    def _client_for(self, backend: Backend) -> genai.Client:
        if backend is Backend.FREE:
            return self._free
        return self._get_vertex()

    async def generate(
        self,
        *,
        contents: list[Any],
        system_instruction: str | None = None,
        tools: list[Any] | None = None,
        force_backend: Backend | None = None,
    ) -> types.GenerateContentResponse:
        backend = force_backend or await self.rotator.choose()
        # Stay on free if Vertex isn't set up yet (until free quota forces the issue).
        if backend is Backend.VERTEX and not self.settings.vertex_enabled:
            if force_backend is Backend.VERTEX:
                self._get_vertex()  # raises VertexNotConfiguredError
            backend = Backend.FREE

        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if tools:
            config_kwargs["tools"] = tools
        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

        try:
            response = await self._client_for(backend).aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
            await self.rotator.record_success(backend)
            return response
        except Exception as exc:
            if backend is Backend.FREE and _is_rate_limited(exc):
                log.warning("free_tier_exhausted_retry_vertex", error=str(exc))
                await self.rotator.mark_free_exhausted()
                if not self.settings.vertex_enabled:
                    raise VertexNotConfiguredError(
                        "Free tier rate-limited, but Vertex is not configured. "
                        "Add a service-account JSON path + VERTEX_PROJECT_ID to .env."
                    ) from exc
                response = await self._get_vertex().aio.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                await self.rotator.record_success(Backend.VERTEX)
                return response
            raise

    async def stats(self) -> RotatorStats:
        return await self.rotator.stats()
