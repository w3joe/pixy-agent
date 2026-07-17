"""Discover and load skill modules from skills/."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from google.genai import types

from pixy.logging.setup import get_logger

log = get_logger("pixy.skills")


class SkillRegistry:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self._modules: dict[str, ModuleType] = {}
        self._handlers: dict[str, Callable[..., str]] = {}
        self._declarations: dict[str, types.FunctionDeclaration] = {}

    @property
    def skill_names(self) -> list[str]:
        return sorted(self._handlers.keys())

    def declarations(self) -> list[types.FunctionDeclaration]:
        return list(self._declarations.values())

    def load_all(self) -> None:
        if not self.skills_dir.exists():
            return
        for path in sorted(self.skills_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            self.load_file(path)

    def load_file(self, path: Path) -> None:
        name = path.stem
        module_name = f"pixy_skill_{name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load spec for {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            skill = getattr(module, "SKILL", None)
            run = getattr(module, "run", None)
            if not isinstance(skill, dict) or not callable(run):
                log.warning("skill_invalid", path=str(path))
                return

            skill_name = str(skill.get("name") or name)
            description = str(skill.get("description") or "")
            parameters = skill.get("parameters") or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }

            # Drop previous registration if reloading under a different name.
            self._unregister_module(name)

            self._modules[name] = module
            self._handlers[skill_name] = run
            self._declarations[skill_name] = types.FunctionDeclaration(
                name=skill_name,
                description=description,
                parameters_json_schema=parameters,
            )
            log.info("skill_loaded", skill=skill_name, path=str(path))
        except Exception as exc:  # noqa: BLE001
            log.error("skill_load_failed", path=str(path), error=str(exc))

    def unload_file(self, path: Path) -> None:
        self._unregister_module(path.stem)

    def _unregister_module(self, module_stem: str) -> None:
        module = self._modules.pop(module_stem, None)
        if module is None:
            return
        skill = getattr(module, "SKILL", {}) or {}
        skill_name = str(skill.get("name") or module_stem)
        self._handlers.pop(skill_name, None)
        self._declarations.pop(skill_name, None)
        sys.modules.pop(f"pixy_skill_{module_stem}", None)

    def run(self, name: str, args: dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"unknown skill: {name}"
        try:
            result = handler(**args)
            return str(result)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
