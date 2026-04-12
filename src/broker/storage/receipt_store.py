"""Raw ENA receipt store.

Persists ENA response bodies exactly as received — no reformatting.
One file per entity: {receipt_dir}/{attempt_id}/{entity_type}_{entity_id}.txt
"""

from __future__ import annotations

from pathlib import Path

from broker.enums import EntityType
from broker.errors import StateStoreError


class ReceiptStore:
    def __init__(self, receipt_dir: str) -> None:
        self._dir = Path(receipt_dir).expanduser().resolve()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateStoreError(
                f"Cannot create receipt directory {self._dir}: {exc}"
            ) from exc

    def save(
        self,
        attempt_id: str,
        entity_type: EntityType,
        entity_id: str,
        raw_receipt: str,
    ) -> Path:
        """Write raw receipt verbatim. Returns the path of the saved file."""
        attempt_dir = self._dir / attempt_id
        try:
            attempt_dir.mkdir(parents=True, exist_ok=True)
            path = attempt_dir / f"{entity_type}_{entity_id}.txt"
            path.write_text(raw_receipt, encoding="utf-8")
            return path
        except OSError as exc:
            raise StateStoreError(
                f"Failed to save receipt for {entity_type} {entity_id}: {exc}"
            ) from exc

    def load(
        self,
        attempt_id: str,
        entity_type: EntityType,
        entity_id: str,
    ) -> str | None:
        """Return the raw receipt string, or None if not found."""
        path = self._dir / attempt_id / f"{entity_type}_{entity_id}.txt"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")
