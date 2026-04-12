"""Tests for PrerequisiteValidator — prerequisite resolution and failure behaviour."""

from __future__ import annotations

import pytest

from broker.enums import AttemptMode, EntitySubmissionStatus, EntityType
from broker.errors import PrerequisiteMissingError
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.services.prerequisite_validation import PrerequisiteValidator


def make_attempt() -> AttemptState:
    return AttemptState(attempt_id="atm-1", tax_id="9606", mode=AttemptMode.BULK)


def make_entity(entity_type: EntityType, entity_id: str = "e1") -> EntitySubmissionState:
    return EntitySubmissionState(entity_id=entity_id, entity_type=entity_type)


validator = PrerequisiteValidator()


# ---------------------------------------------------------------------------
# Project — no prerequisites
# ---------------------------------------------------------------------------


def test_project_needs_no_prerequisites():
    entity = make_entity(EntityType.PROJECT, "p1")
    attempt = make_attempt()
    result = validator.resolve_prerequisites(entity, attempt, cli_overrides={})
    assert result is entity  # same object, mutated in place


# ---------------------------------------------------------------------------
# Sample — needs project_accession
# ---------------------------------------------------------------------------


def test_sample_resolved_from_cli_overrides():
    entity = make_entity(EntityType.SAMPLE, "s1")
    attempt = make_attempt()
    validator.resolve_prerequisites(
        entity, attempt, cli_overrides={"project_accession": "PRJEB999"}
    )
    assert entity.project_accession == "PRJEB999"


def test_sample_resolved_from_payload():
    entity = make_entity(EntityType.SAMPLE, "s1")
    entity.project_accession = "PRJEB001"  # set from Canopy payload
    attempt = make_attempt()
    validator.resolve_prerequisites(entity, attempt, cli_overrides={})
    assert entity.project_accession == "PRJEB001"


def test_cli_override_wins_over_payload():
    entity = make_entity(EntityType.SAMPLE, "s1")
    entity.project_accession = "PRJEB-FROM-PAYLOAD"
    attempt = make_attempt()
    validator.resolve_prerequisites(
        entity, attempt, cli_overrides={"project_accession": "PRJEB-FROM-CLI"}
    )
    assert entity.project_accession == "PRJEB-FROM-CLI"


def test_sample_resolved_from_state_fallback():
    entity = make_entity(EntityType.SAMPLE, "s1")
    attempt = make_attempt()
    # Add a succeeded project to the attempt state
    proj = make_entity(EntityType.PROJECT, "p1")
    proj.mark_succeeded("PRJEB-FROM-STATE")
    attempt.entities[EntityType.PROJECT].append(proj)
    validator.resolve_prerequisites(entity, attempt, cli_overrides={}, allow_state_fallback=True)
    assert entity.project_accession == "PRJEB-FROM-STATE"


def test_sample_fails_when_project_accession_missing():
    entity = make_entity(EntityType.SAMPLE, "s1")
    attempt = make_attempt()
    with pytest.raises(PrerequisiteMissingError) as exc_info:
        validator.resolve_prerequisites(entity, attempt, cli_overrides={})
    assert "project_accession" in exc_info.value.missing
    assert "s1" in str(exc_info.value)


def test_targeted_mode_no_state_fallback():
    """In targeted mode (allow_state_fallback=False), state is NOT consulted."""
    entity = make_entity(EntityType.SAMPLE, "s1")
    attempt = make_attempt()
    # Succeeded project exists in state — but should NOT be used in targeted mode
    proj = make_entity(EntityType.PROJECT, "p1")
    proj.mark_succeeded("PRJEB-FROM-STATE")
    attempt.entities[EntityType.PROJECT].append(proj)
    with pytest.raises(PrerequisiteMissingError) as exc_info:
        validator.resolve_prerequisites(
            entity, attempt, cli_overrides={}, allow_state_fallback=False
        )
    assert "project_accession" in exc_info.value.missing


# ---------------------------------------------------------------------------
# Experiment — needs project_accession + sample_accession
# ---------------------------------------------------------------------------


def test_experiment_all_from_cli():
    entity = make_entity(EntityType.EXPERIMENT, "x1")
    attempt = make_attempt()
    validator.resolve_prerequisites(
        entity,
        attempt,
        cli_overrides={
            "project_accession": "PRJEB1",
            "sample_accession": "ERS1",
        },
    )
    assert entity.project_accession == "PRJEB1"
    assert entity.sample_accession == "ERS1"


def test_experiment_fails_with_multiple_missing():
    entity = make_entity(EntityType.EXPERIMENT, "x1")
    attempt = make_attempt()
    with pytest.raises(PrerequisiteMissingError) as exc_info:
        validator.resolve_prerequisites(entity, attempt, cli_overrides={})
    assert len(exc_info.value.missing) == 2
    assert "project_accession" in exc_info.value.missing
    assert "sample_accession" in exc_info.value.missing


def test_experiment_one_from_cli_one_missing():
    entity = make_entity(EntityType.EXPERIMENT, "x1")
    attempt = make_attempt()
    with pytest.raises(PrerequisiteMissingError) as exc_info:
        validator.resolve_prerequisites(
            entity, attempt, cli_overrides={"project_accession": "PRJEB1"}
        )
    assert exc_info.value.missing == ["sample_accession"]


# ---------------------------------------------------------------------------
# Run — needs experiment_accession
# ---------------------------------------------------------------------------


def test_run_resolved_from_cli():
    entity = make_entity(EntityType.RUN, "r1")
    attempt = make_attempt()
    validator.resolve_prerequisites(
        entity, attempt, cli_overrides={"experiment_accession": "ERX1"}
    )
    assert entity.experiment_accession == "ERX1"


def test_run_fails_when_experiment_missing():
    entity = make_entity(EntityType.RUN, "r1")
    attempt = make_attempt()
    with pytest.raises(PrerequisiteMissingError) as exc_info:
        validator.resolve_prerequisites(entity, attempt, cli_overrides={})
    assert "experiment_accession" in exc_info.value.missing
