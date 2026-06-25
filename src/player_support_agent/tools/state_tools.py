"""Local case state and audit-log tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import StateConfig


class StateTools:
    """Simple JSON state store for the MVP."""

    def __init__(self, config: StateConfig) -> None:
        self.config = config

    def _state_path(self) -> Path:
        return Path(self.config.state_path)

    def _audit_path(self) -> Path:
        return Path(self.config.audit_log_path)

    def _read_state(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(self, data: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def get_case_state(self, case_id: str) -> dict[str, Any]:
        """Read case state by Gmail thread id or internal case id."""

        data = self._read_state()
        return {"case_id": case_id, "state": data.get(case_id)}

    def save_case_state(
        self,
        case_id: str,
        status: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist case state."""

        state = self._read_state()
        existing = state.get(case_id, {})
        now = datetime.now(timezone.utc).isoformat()
        state[case_id] = {
            **existing,
            "case_id": case_id,
            "status": status,
            "data": data,
            "updated_at": now,
            "created_at": existing.get("created_at", now),
        }
        self._write_state(state)
        return state[case_id]

    def write_audit_log(
        self,
        case_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Append a JSONL audit event."""

        path = self._audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "case_id": case_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"written": True, "path": str(path), "event": event}
