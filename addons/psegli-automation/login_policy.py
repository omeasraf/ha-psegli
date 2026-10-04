"""Persist the automatic login cooldown across add-on restarts."""
import json
import time
from pathlib import Path


class LoginGate:
    def __init__(self, path: Path, cooldown_seconds: int = 21600):
        self.path = path
        self.cooldown_seconds = cooldown_seconds

    def retry_after(self) -> float:
        if not self.path.exists():
            return 0
        try:
            return float(json.loads(self.path.read_text())["retry_after"])
        except (OSError, ValueError, KeyError, TypeError):
            # A corrupt cooldown must not cause a burst of fresh logins.
            return time.time() + self.cooldown_seconds

    def allowed(self) -> bool:
        return time.time() >= self.retry_after()

    def _save(self, retry_after: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"retry_after": retry_after}))
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def attempted(self) -> None:
        self._save(time.time() + self.cooldown_seconds)

    def succeeded(self) -> None:
        self._save(0)
