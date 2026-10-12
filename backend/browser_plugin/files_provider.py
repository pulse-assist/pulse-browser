"""@browser file provider — artifacts and audit dumps per session."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pulse_plugin.fs import FileProvider, NotFound, ReadOnly, entry


class ArtifactsFS(FileProvider):
    """Read-only view of store.artifacts (+ runs as nested folders)."""

    readonly = True

    def __init__(self, artifacts: Path, runs: Path):
        self.artifacts = Path(artifacts)
        self.runs = Path(runs)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir(parents=True, exist_ok=True)

    def list(self, path: str) -> list[dict]:
        path = path.strip("/")
        if not path:
            names = sorted({*(p.name for p in self.artifacts.iterdir() if p.is_dir()),
                            *(p.name for p in self.runs.iterdir() if p.is_dir())})
            return [entry(n, "dir", readonly=True) for n in names]
        session, _, rest = path.partition("/")
        base = self._session_root(session)
        if not rest:
            items = []
            for child in sorted(base.iterdir(), key=lambda p: p.name):
                if child.is_dir():
                    items.append(entry(child.name, "dir", readonly=True,
                                       modified_at=_mtime(child)))
                else:
                    items.append(entry(child.name, "file", size=child.stat().st_size,
                                       readonly=True, modified_at=_mtime(child),
                                       mime=_mime(child.name)))
            # virtual runs/ folder
            run_dir = self.runs / session
            if run_dir.is_dir() and not (base / "runs").exists():
                items.append(entry("runs", "dir", readonly=True, modified_at=_mtime(run_dir)))
            return items
        if rest == "runs" or rest.startswith("runs/"):
            target = self.runs / session / rest[len("runs"):].lstrip("/")
            if rest == "runs":
                target = self.runs / session
            if not target.exists():
                raise NotFound(path)
            if target.is_dir():
                return [
                    entry(c.name, "dir" if c.is_dir() else "file",
                          size=None if c.is_dir() else c.stat().st_size,
                          readonly=True, modified_at=_mtime(c),
                          mime=None if c.is_dir() else _mime(c.name))
                    for c in sorted(target.iterdir(), key=lambda p: p.name)
                ]
            raise NotFound(path)
        target = base / rest
        if not target.exists():
            raise NotFound(path)
        if target.is_dir():
            return [
                entry(c.name, "dir" if c.is_dir() else "file",
                      size=None if c.is_dir() else c.stat().st_size,
                      readonly=True, modified_at=_mtime(c),
                      mime=None if c.is_dir() else _mime(c.name))
                for c in sorted(target.iterdir(), key=lambda p: p.name)
            ]
        raise NotFound(path)

    def stat(self, path: str) -> dict:
        path = path.strip("/")
        if not path:
            return entry("", "dir", readonly=True)
        resolved = self._resolve(path)
        if resolved is None or not resolved.exists():
            raise NotFound(path)
        if resolved.is_dir():
            return entry(resolved.name or path, "dir", readonly=True, modified_at=_mtime(resolved))
        return entry(
            resolved.name, "file", size=resolved.stat().st_size, readonly=True,
            modified_at=_mtime(resolved), mime=_mime(resolved.name),
        )

    def read(self, path: str, offset: int = 0, length: int | None = None) -> bytes:
        resolved = self._resolve(path.strip("/"))
        if resolved is None or not resolved.is_file():
            raise NotFound(path)
        data = resolved.read_bytes()
        if offset:
            data = data[offset:]
        if length is not None:
            data = data[:length]
        return data

    def write(self, path, stream, overwrite=True):
        raise ReadOnly("артефакты браузера только для чтения")

    def _session_root(self, session: str) -> Path:
        path = self.artifacts / session
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve(self, path: str) -> Path | None:
        if not path:
            return self.artifacts
        session, _, rest = path.partition("/")
        if not rest:
            return self.artifacts / session
        if rest == "runs" or rest.startswith("runs/"):
            return self.runs / session / rest[len("runs"):].lstrip("/")
        return self.artifacts / session / rest


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _mime(name: str) -> str | None:
    lower = name.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".html"):
        return "text/html"
    if lower.endswith(".json"):
        return "application/json"
    if lower.endswith(".txt") or lower.endswith(".md"):
        return "text/plain"
    return None
