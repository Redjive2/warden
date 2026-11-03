"""warden's two files: an append-only audit log, and the catch-up state."""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .rules import Outcome


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)


class Audit:
    """One JSON object per line, appended. Never rewritten."""

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        _ensure_dir(self.path)

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event}
        record.update(fields)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def outcome(self, o: Outcome, dry_run: bool) -> None:
        self.write(
            "outcome",
            status=o.status,
            user_id=str(o.target.user_id),
            name=o.target.name,
            trigger=o.trigger.kind,
            message_id=None if o.trigger.message_id is None else str(o.trigger.message_id),
            detail=o.detail,
            delete_seconds=o.delete_seconds,
            message_deletion=o.message_deletion,
            dry_run=dry_run,
        )


class State:
    """The last honeypot message warden handled, so a restart catches up from there."""

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        self._data: Dict[str, Any] = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)
        except FileNotFoundError:
            pass

    @property
    def last_honeypot_message_id(self) -> Optional[int]:
        value = self._data.get("last_honeypot_message_id")
        return int(value) if value is not None else None

    def advance_honeypot(self, message_id: int) -> None:
        """Record message_id as handled, if it is newer than what is recorded."""
        current = self.last_honeypot_message_id
        if current is not None and message_id <= current:
            return
        self._data["last_honeypot_message_id"] = str(message_id)
        self._save()

    def _save(self) -> None:
        _ensure_dir(self.path)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
        os.replace(tmp, self.path)
