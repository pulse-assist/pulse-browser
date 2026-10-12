"""Persistent session config and audit log on plugin data disk."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,39}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """sessions.json + profiles/ + artifacts/ + runs/ under PULSE_PLUGIN_DATA."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir)
        self.sessions_file = self.root / "sessions.json"
        self.profiles = self.root / "profiles"
        self.artifacts = self.root / "artifacts"
        self.runs = self.root / "runs"
        self.pending_file = self.root / "pending_confirms.json"
        self._lock = threading.RLock()
        for path in (self.profiles, self.artifacts, self.runs):
            path.mkdir(parents=True, exist_ok=True)
        if not self.sessions_file.exists():
            self._write_json(self.sessions_file, {})
        if not self.pending_file.exists():
            self._write_json(self.pending_file, {})

    def validate_name(self, name: str) -> str:
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ValueError(
                "имя сессии — латиница/цифры/_/-, 1–40 символов, с буквы (например amazon)"
            )
        return name

    def list_sessions(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._read_json(self.sessions_file))

    def get(self, name: str) -> dict:
        sessions = self.list_sessions()
        if name not in sessions:
            raise KeyError(f"сессия «{name}» не найдена — pulse browser session create --name {name}")
        return dict(sessions[name])

    def create(
        self,
        name: str,
        *,
        mode: str = "read-only",
        proxy: str | None = None,
        allowlist: list[str] | None = None,
    ) -> dict:
        name = self.validate_name(name)
        mode = self._mode(mode)
        with self._lock:
            sessions = self._read_json(self.sessions_file)
            if name in sessions:
                raise ValueError(f"сессия «{name}» уже есть")
            item = {
                "name": name,
                "mode": mode,
                "proxy": (proxy or "").strip() or None,
                "allowlist": allowlist or [],
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "last_url": None,
                "last_title": None,
            }
            sessions[name] = item
            self._write_json(self.sessions_file, sessions)
            (self.profiles / name).mkdir(parents=True, exist_ok=True)
            (self.artifacts / name).mkdir(parents=True, exist_ok=True)
            (self.runs / name).mkdir(parents=True, exist_ok=True)
            return dict(item)

    def update(self, name: str, **fields: Any) -> dict:
        with self._lock:
            sessions = self._read_json(self.sessions_file)
            if name not in sessions:
                raise KeyError(f"сессия «{name}» не найдена")
            item = sessions[name]
            if "mode" in fields and fields["mode"] is not None:
                item["mode"] = self._mode(fields["mode"])
            if "proxy" in fields:
                proxy = fields["proxy"]
                item["proxy"] = (proxy or "").strip() or None
            if "allowlist" in fields and fields["allowlist"] is not None:
                item["allowlist"] = list(fields["allowlist"])
            if "last_url" in fields:
                item["last_url"] = fields["last_url"]
            if "last_title" in fields:
                item["last_title"] = fields["last_title"]
            item["updated_at"] = utc_now()
            sessions[name] = item
            self._write_json(self.sessions_file, sessions)
            return dict(item)

    def delete(self, name: str) -> None:
        import shutil

        with self._lock:
            sessions = self._read_json(self.sessions_file)
            sessions.pop(name, None)
            self._write_json(self.sessions_file, sessions)
            for base in (self.profiles, self.artifacts, self.runs):
                path = base / name
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)

    def profile_dir(self, name: str) -> Path:
        path = self.profiles / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifact_dir(self, name: str) -> Path:
        path = self.artifacts / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def audit(self, name: str, action: str, **extra: Any) -> Path:
        run_dir = self.runs / name
        run_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = run_dir / f"{stamp}_{action}.json"
        payload = {"at": utc_now(), "action": action, **extra}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def add_pending(self, session: str, action: str, detail: dict) -> str:
        confirm_id = f"c{int(time.time() * 1000)}"
        with self._lock:
            pending = self._read_json(self.pending_file)
            pending[confirm_id] = {
                "id": confirm_id,
                "session": session,
                "action": action,
                "detail": detail,
                "created_at": utc_now(),
                "status": "pending",
            }
            self._write_json(self.pending_file, pending)
        return confirm_id

    def list_pending(self, session: str | None = None) -> list[dict]:
        with self._lock:
            pending = self._read_json(self.pending_file)
        items = [v for v in pending.values() if v.get("status") == "pending"]
        if session:
            items = [i for i in items if i.get("session") == session]
        return sorted(items, key=lambda i: i.get("created_at") or "")

    def consume_pending(self, confirm_id: str, session: str) -> dict:
        with self._lock:
            pending = self._read_json(self.pending_file)
            item = pending.get(confirm_id)
            if not item or item.get("status") != "pending":
                raise KeyError(f"подтверждение «{confirm_id}» не найдено или уже использовано")
            if item.get("session") != session:
                raise ValueError("подтверждение относится к другой сессии")
            item["status"] = "approved"
            item["approved_at"] = utc_now()
            pending[confirm_id] = item
            self._write_json(self.pending_file, pending)
            return dict(item)

    def _mode(self, mode: str) -> str:
        mode = (mode or "read-only").strip().lower().replace("_", "-")
        if mode not in ("read-only", "write"):
            raise ValueError("mode — read-only или write")
        return mode

    def _read_json(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
