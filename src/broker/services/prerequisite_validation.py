"""Prerequisite accession resolver and validator.

Enforces the rule: the broker never silently auto-submits dependencies.
If required accessions cannot be resolved, it raises PrerequisiteMissingError
with a clear message naming the missing fields.

Resolution order (highest priority first):
  1. CLI-supplied args (cli_overrides dict)
  2. Canopy payload (entity.project_accession / sample_accession / experiment_accession)
  3. Within-attempt state: an already-succeeded entity of the matching type
     — ONLY in bulk mode (allow_state_fallback=True)
  4. Fail with PrerequisiteMissingError

Prerequisites by entity type:
  project:    none
  sample:     project_accession (required by ENA study reference)
  experiment: project_accession, sample_accession
  run:        experiment_accession
"""

from __future__ import annotations

import logging

from broker.enums import EntitySubmissionStatus, EntityType
from broker.errors import PrerequisiteMissingError
from broker.models.attempt import AttemptState, EntitySubmissionState

logger = logging.getLogger(__name__)

# What each entity type needs, and which field provides it.
_PREREQUISITES: dict[EntityType, list[str]] = {
    EntityType.PROJECT: [],
    EntityType.SAMPLE: ["project_accession"],
    EntityType.EXPERIMENT: ["project_accession", "sample_accession"],
    EntityType.RUN: ["experiment_accession"],
}

# Maps prerequisite field name → the entity type whose accession satisfies it.
_FIELD_TO_TYPE: dict[str, EntityType] = {
    "project_accession": EntityType.PROJECT,
    "sample_accession": EntityType.SAMPLE,
    "experiment_accession": EntityType.EXPERIMENT,
}


class PrerequisiteValidator:
    """Resolves and validates prerequisites for a single entity submission.

    This is a pure logic class with no external calls.
    Canopy validation (HTTP) is a separate optional step in the orchestrator.
    """

    def resolve_prerequisites(
        self,
        entity: EntitySubmissionState,
        attempt_state: AttemptState,
        cli_overrides: dict[str, str],
        allow_state_fallback: bool = True,
    ) -> EntitySubmissionState:
        """Resolve prerequisite accession fields onto entity in place.

        Args:
            entity:              The entity being submitted. Modified in place.
            attempt_state:       Full attempt state (used for state fallback).
            cli_overrides:       Values supplied via CLI flags, highest priority.
            allow_state_fallback: If True (bulk mode), fall back to a succeeded
                                  entity within the same attempt. Must be False
                                  in targeted/partial mode to prevent silent
                                  dependency resolution.

        Returns:
            The mutated entity with prerequisite fields populated.

        Raises:
            PrerequisiteMissingError: if any required field cannot be resolved.
        """
        required_fields = _PREREQUISITES.get(entity.entity_type, [])
        if not required_fields:
            return entity

        missing: list[str] = []
        for field in required_fields:
            value = self._resolve_field(
                field, entity, attempt_state, cli_overrides, allow_state_fallback
            )
            if value is not None:
                setattr(entity, field, value)
                logger.debug(
                    "Resolved %s for %s %s: %s",
                    field,
                    entity.entity_type,
                    entity.entity_id,
                    value,
                )
            else:
                missing.append(field)

        if missing:
            raise PrerequisiteMissingError(
                entity_type=str(entity.entity_type),
                entity_id=entity.entity_id,
                missing=missing,
            )

        return entity

    def _resolve_field(
        self,
        field: str,
        entity: EntitySubmissionState,
        attempt_state: AttemptState,
        cli_overrides: dict[str, str],
        allow_state_fallback: bool,
    ) -> str | None:
        # 1. CLI args (highest priority)
        if field in cli_overrides and cli_overrides[field]:
            return cli_overrides[field]

        # 2. Already set on entity from Canopy payload
        existing = getattr(entity, field, None)
        if existing:
            return existing

        # 3. State fallback — only in bulk mode
        if allow_state_fallback:
            source_type = _FIELD_TO_TYPE.get(field)
            if source_type is not None:
                accession = self._find_succeeded_accession(source_type, attempt_state)
                if accession:
                    return accession

        return None

    @staticmethod
    def _find_succeeded_accession(
        entity_type: EntityType, attempt_state: AttemptState
    ) -> str | None:
        """Return the ENA accession of the most recent succeeded entity of the given type."""
        for e in attempt_state.entities.get(entity_type, []):
            if e.status == EntitySubmissionStatus.SUCCEEDED and e.ena_accession:
                return e.ena_accession
        return None
