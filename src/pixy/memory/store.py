"""Filesystem-backed memory under .memory/."""

from __future__ import annotations

from pathlib import Path


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative: str) -> Path:
        rel = relative.strip().lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise ValueError(f"invalid memory path: {relative!r}")
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"path escapes memory root: {relative!r}")
        return path

    def list_files(self) -> list[str]:
        files: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                files.append(str(path.relative_to(self.root)))
        return files

    def read(self, relative: str) -> str:
        path = self._resolve(relative)
        if not path.exists():
            raise FileNotFoundError(f"memory file not found: {relative}")
        return path.read_text(encoding="utf-8")

    def write(self, relative: str, content: str) -> str:
        path = self._resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {relative} ({len(content)} chars)"

    def append(self, relative: str, line: str) -> str:
        path = self._resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            if line.endswith("\n"):
                fh.write(line)
            else:
                fh.write(line + "\n")
        return f"appended to {relative}"
