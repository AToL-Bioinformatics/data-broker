"""JSON file-based attempt state store.

One file per attempt: {state_dir}/{attempt_id}.json

Writes are atomic via a .tmp file + os.replace() to prevent partial writes
from corrupting readable state. On POSIX systems os.replace() is an atomic
rename; on Windows it is best-effort (Python guarantees the destination is
replaced, but the rename itself is not guaranteed to be atomic pre-3.12).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from broker.errors import AttemptNotFoundError, StateStoreError
from broker.models.attempt import AttemptState


class StateStore:
    def __init__(self, state_dir: str) -> None:
        self._dir = Path(state_dir).expanduser().resolve()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateStoreError(f"Cannot create state directory {self._dir}: {exc}") from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self, state: AttemptState) -> None:
        """Persist attempt state atomically.

        Writes to a .tmp file first, then replaces the target file so that
        a crash mid-write leaves the previous state file intact.
        """
        state.touch()
        target = self._path(state.attempt_id)
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            raise StateStoreError(
                f"Failed to save state for attempt {state.attempt_id}: {exc}"
            ) from exc
        finally:
            # Clean up .tmp if os.replace failed
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def load(self, attempt_id: str) -> AttemptState:
        """Load and deserialise attempt state.

        Raises:
            AttemptNotFoundError: if the state file does not exist.
            StateStoreError: if the file exists but cannot be parsed.
        """
        path = self._path(attempt_id)
        if not path.exists():
            raise AttemptNotFoundError(attempt_id)
        try:
            return AttemptState.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StateStoreError(
                f"Failed to parse state file for attempt {attempt_id}: {exc}"
            ) from exc

    def list_attempts(self) -> list[str]:
        """Return sorted list of all known attempt IDs."""
        return sorted(p.stem for p in self._dir.glob("*.json"))

    @staticmethod
    def generate_attempt_id() -> str:
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, attempt_id: str) -> Path:
        return self._dir / f"{attempt_id}.json"
