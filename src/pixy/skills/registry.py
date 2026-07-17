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
        # registration key -> entry file path (for package reload)
        self._entry_paths: dict[str, Path] = {}

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
        for path in sorted(self.skills_dir.iterdir()):
            if not path.is_dir() or path.name.startswith("_") or path.name == "__pycache__":
                continue
            entry = path / "skill.py"
            if entry.is_file():
                self.load_file(entry)

    def resolve_entry(self, path: Path) -> Path | None:
        """Map any file under a package skill to its skill.py entrypoint."""
        try:
            path = path.resolve()
            skills_dir = self.skills_dir.resolve()
            path.relative_to(skills_dir)
        except ValueError:
            return None

        if path.suffix == ".py" and path.parent == skills_dir and not path.name.startswith("_"):
            return path

        # skills/<pkg>/... → skills/<pkg>/skill.py
        rel = path.relative_to(skills_dir)
        if not rel.parts:
            return None
        pkg = skills_dir / rel.parts[0]
        entry = pkg / "skill.py"
        if entry.is_file():
            return entry
        return None

    def registration_key(self, path: Path) -> str:
        """Flat skills/foo.py → foo; package skills/aspire/skill.py → aspire."""
        path = path.resolve()
        skills_dir = self.skills_dir.resolve()
        if path.parent == skills_dir:
            return path.stem
        try:
            rel = path.relative_to(skills_dir)
        except ValueError:
            return path.stem
        if path.name == "skill.py" and len(rel.parts) == 2:
            return rel.parts[0]
        if len(rel.parts) >= 1:
            entry = skills_dir / rel.parts[0] / "skill.py"
            if entry.is_file():
                return rel.parts[0]
        return path.stem

    def load_file(self, path: Path) -> None:
        path = path.resolve()
        entry = self.resolve_entry(path) or path
        if not entry.is_file():
            return
        # Always load the package entrypoint for nested files.
        if entry != path and entry.name == "skill.py":
            path = entry

        name = self.registration_key(path)
        module_name = f"pixy_skill_{name}"
        package_dir = None
        if (
            path.name == "skill.py"
            and path.parent.parent.resolve() == self.skills_dir.resolve()
        ):
            package_dir = path.parent

        try:
            # Allow package skills to `from sdk... import ...`
            inserted_path = False
            if package_dir is not None:
                pkg_str = str(package_dir)
                if pkg_str not in sys.path:
                    sys.path.insert(0, pkg_str)
                    inserted_path = True
                # Drop cached sdk submodules so reload picks up changes.
                for key in list(sys.modules):
                    if key == "sdk" or key.startswith("sdk."):
                        sys.modules.pop(key, None)

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

            self._unregister_module(name)

            self._modules[name] = module
            self._entry_paths[name] = path
            self._handlers[skill_name] = run
            self._declarations[skill_name] = types.FunctionDeclaration(
                name=skill_name,
                description=description,
                parameters_json_schema=parameters,
            )
            log.info("skill_loaded", skill=skill_name, path=str(path))
        except Exception as exc:  # noqa: BLE001
            log.error("skill_load_failed", path=str(path), error=str(exc))
        finally:
            # Keep package_dir on sys.path so later run() imports keep working.
            _ = inserted_path  # path left intentionally for runtime imports

    def unload_file(self, path: Path) -> None:
        entry = self.resolve_entry(path)
        if entry is None:
            self._unregister_module(path.stem)
            return
        self._unregister_module(self.registration_key(entry))

    def _unregister_module(self, module_stem: str) -> None:
        module = self._modules.pop(module_stem, None)
        self._entry_paths.pop(module_stem, None)
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
