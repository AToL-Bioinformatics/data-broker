"""Attempt state models — the central state document persisted to disk.

One JSON file per attempt: ~/.broker/state/{attempt_id}.json

Design notes:
- entities is dict[EntityType, list[EntitySubmissionState]] so dependency-order
  traversal is O(1) grouping rather than a sort on every iteration.
- raw_payload on each entity stores the Canopy claim data so that resume never
  needs to re-claim from Canopy.
- hold_until_date is stored at the attempt level so resume re-uses it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from broker.enums import (
    AttemptMode,
    AttemptStatus,
    ENTITY_DEPENDENCY_ORDER,
    EntitySubmissionStatus,
    EntityType,
    SubmissionMode,
)


class EntitySubmissionState(BaseModel):
    entity_id: str
    entity_type: EntityType
    status: EntitySubmissionStatus = EntitySubmissionStatus.PENDING

    # Prerequisites resolved at runtime (CLI args > Canopy payload > state fallback)
    project_accession: str | None = None
    sample_accession: str | None = None
    experiment_accession: str | None = None

    # ENA accessions assigned after successful submission
    ena_accession: str | None = None  # primary: PRJEB*, ERS*, ERX*, ERR*
    biosample_accession: str | None = None  # secondary BioSample accession (samples only)

    # Tree of Life ID — populated after a successful ToLID request (samples only)
    tolid: str | None = None

    # Error tracking
    error_message: str | None = None

    # Timing
    submitted_at: datetime | None = None
    succeeded_at: datetime | None = None

    # Canopy-provided raw payload; kept for transform and for crash-safe resume.
    # This means resume never needs to re-call Canopy.
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    def mark_submitted(self) -> None:
        self.status = EntitySubmissionStatus.SUBMITTED
        self.submitted_at = datetime.now(timezone.utc)

    def mark_succeeded(
        self,
        ena_accession: str,
        biosample_accession: str | None = None,
    ) -> None:
        self.status = EntitySubmissionStatus.SUCCEEDED
        self.ena_accession = ena_accession
        self.biosample_accession = biosample_accession
        self.succeeded_at = datetime.now(timezone.utc)
        self.error_message = None

    def mark_failed(self, error_message: str) -> None:
        self.status = EntitySubmissionStatus.FAILED
        self.error_message = error_message

    def mark_skipped(self) -> None:
        self.status = EntitySubmissionStatus.SKIPPED

    @property
    def is_terminal(self) -> bool:
        """True if no further action should be taken for this entity."""
        return self.status in (
            EntitySubmissionStatus.SUCCEEDED,
            EntitySubmissionStatus.SKIPPED,
        )


class AttemptState(BaseModel):
    attempt_id: str
    tax_id: str | None = None  # populated in bulk mode
    mode: AttemptMode
    submission_mode: SubmissionMode = SubmissionMode.NORMAL
    status: AttemptStatus = AttemptStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # ISO 8601 date (e.g. "2026-01-01") for ENA HOLD action.
    # Applied to project and sample SUBMISSION XML only.
    hold_until_date: str | None = None

    # Entities grouped by type; traversal order defined by ENTITY_DEPENDENCY_ORDER.
    entities: dict[EntityType, list[EntitySubmissionState]] = Field(
        default_factory=lambda: {et: [] for et in EntityType}
    )

    def get_entity(
        self, entity_type: EntityType, entity_id: str
    ) -> EntitySubmissionState | None:
        for e in self.entities.get(entity_type, []):
            if e.entity_id == entity_id:
                return e
        return None

    def all_entities_flat(self) -> list[EntitySubmissionState]:
        """Return all entities in canonical dependency order: project→sample→experiment→run."""
        return [e for et in ENTITY_DEPENDENCY_ORDER for e in self.entities.get(et, [])]

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)

    def compute_status(self) -> AttemptStatus:
        """Derive overall attempt status from current entity states."""
        all_entities = self.all_entities_flat()
        if not all_entities:
            return AttemptStatus.COMPLETED
        statuses = {e.status for e in all_entities}
        if all(e.is_terminal for e in all_entities):
            if EntitySubmissionStatus.FAILED in statuses:
                return AttemptStatus.PARTIAL
            return AttemptStatus.COMPLETED
        if EntitySubmissionStatus.FAILED in statuses:
            return AttemptStatus.PARTIAL
        return AttemptStatus.IN_PROGRESS
