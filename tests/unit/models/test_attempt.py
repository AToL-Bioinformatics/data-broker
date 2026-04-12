"""Tests for AttemptState and EntitySubmissionState."""

from __future__ import annotations

import pytest

from broker.enums import AttemptMode, AttemptStatus, EntitySubmissionStatus, EntityType
from broker.models.attempt import AttemptState, EntitySubmissionState


def make_entity(entity_id: str, entity_type: EntityType) -> EntitySubmissionState:
    return EntitySubmissionState(entity_id=entity_id, entity_type=entity_type)


def make_bulk_attempt(attempt_id: str = "atm-1") -> AttemptState:
    return AttemptState(attempt_id=attempt_id, tax_id="9606", mode=AttemptMode.BULK)


# ---------------------------------------------------------------------------
# EntitySubmissionState transitions
# ---------------------------------------------------------------------------


def test_initial_status_is_pending():
    e = make_entity("e1", EntityType.PROJECT)
    assert e.status == EntitySubmissionStatus.PENDING


def test_mark_submitted():
    e = make_entity("e1", EntityType.PROJECT)
    e.mark_submitted()
    assert e.status == EntitySubmissionStatus.SUBMITTED
    assert e.submitted_at is not None


def test_mark_succeeded():
    e = make_entity("e1", EntityType.PROJECT)
    e.mark_succeeded("PRJEB12345")
    assert e.status == EntitySubmissionStatus.SUCCEEDED
    assert e.ena_accession == "PRJEB12345"
    assert e.succeeded_at is not None
    assert e.error_message is None


def test_mark_succeeded_with_biosample():
    e = make_entity("s1", EntityType.SAMPLE)
    e.mark_succeeded("ERS111111", biosample_accession="SAMEA111111")
    assert e.biosample_accession == "SAMEA111111"


def test_mark_failed():
    e = make_entity("e1", EntityType.PROJECT)
    e.mark_failed("ENA returned 400")
    assert e.status == EntitySubmissionStatus.FAILED
    assert e.error_message == "ENA returned 400"


def test_mark_skipped():
    e = make_entity("e1", EntityType.PROJECT)
    e.mark_skipped()
    assert e.status == EntitySubmissionStatus.SKIPPED


def test_is_terminal_for_succeeded():
    e = make_entity("e1", EntityType.PROJECT)
    e.mark_succeeded("PRJEB1")
    assert e.is_terminal is True


def test_is_terminal_for_skipped():
    e = make_entity("e1", EntityType.PROJECT)
    e.mark_skipped()
    assert e.is_terminal is True


def test_is_not_terminal_for_pending():
    e = make_entity("e1", EntityType.PROJECT)
    assert e.is_terminal is False


def test_is_not_terminal_for_submitted():
    e = make_entity("e1", EntityType.PROJECT)
    e.mark_submitted()
    assert e.is_terminal is False


def test_is_not_terminal_for_failed():
    e = make_entity("e1", EntityType.PROJECT)
    e.mark_failed("oops")
    assert e.is_terminal is False


# ---------------------------------------------------------------------------
# AttemptState.compute_status
# ---------------------------------------------------------------------------


def test_compute_status_empty_is_completed():
    state = make_bulk_attempt()
    assert state.compute_status() == AttemptStatus.COMPLETED


def test_compute_status_all_succeeded():
    state = make_bulk_attempt()
    e = make_entity("p1", EntityType.PROJECT)
    e.mark_succeeded("PRJEB1")
    state.entities[EntityType.PROJECT].append(e)
    assert state.compute_status() == AttemptStatus.COMPLETED


def test_compute_status_all_skipped():
    state = make_bulk_attempt()
    e = make_entity("p1", EntityType.PROJECT)
    e.mark_skipped()
    state.entities[EntityType.PROJECT].append(e)
    assert state.compute_status() == AttemptStatus.COMPLETED


def test_compute_status_some_failed():
    state = make_bulk_attempt()
    e1 = make_entity("p1", EntityType.PROJECT)
    e1.mark_succeeded("PRJEB1")
    e2 = make_entity("s1", EntityType.SAMPLE)
    e2.mark_failed("bad payload")
    state.entities[EntityType.PROJECT].append(e1)
    state.entities[EntityType.SAMPLE].append(e2)
    assert state.compute_status() == AttemptStatus.PARTIAL


def test_compute_status_in_progress():
    state = make_bulk_attempt()
    e1 = make_entity("p1", EntityType.PROJECT)
    e1.mark_succeeded("PRJEB1")
    e2 = make_entity("s1", EntityType.SAMPLE)
    # s1 still pending
    state.entities[EntityType.PROJECT].append(e1)
    state.entities[EntityType.SAMPLE].append(e2)
    assert state.compute_status() == AttemptStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# AttemptState.all_entities_flat — dependency order
# ---------------------------------------------------------------------------


def test_all_entities_flat_ordering():
    state = make_bulk_attempt()
    run_e = make_entity("r1", EntityType.RUN)
    exp_e = make_entity("x1", EntityType.EXPERIMENT)
    sam_e = make_entity("s1", EntityType.SAMPLE)
    proj_e = make_entity("p1", EntityType.PROJECT)
    # Add in reverse order; flat() should still return in dependency order
    state.entities[EntityType.RUN].append(run_e)
    state.entities[EntityType.EXPERIMENT].append(exp_e)
    state.entities[EntityType.SAMPLE].append(sam_e)
    state.entities[EntityType.PROJECT].append(proj_e)
    flat = state.all_entities_flat()
    types = [e.entity_type for e in flat]
    assert types == [
        EntityType.PROJECT,
        EntityType.SAMPLE,
        EntityType.EXPERIMENT,
        EntityType.RUN,
    ]


# ---------------------------------------------------------------------------
# AttemptState.get_entity
# ---------------------------------------------------------------------------


def test_get_entity_found():
    state = make_bulk_attempt()
    e = make_entity("p1", EntityType.PROJECT)
    state.entities[EntityType.PROJECT].append(e)
    found = state.get_entity(EntityType.PROJECT, "p1")
    assert found is e


def test_get_entity_not_found():
    state = make_bulk_attempt()
    assert state.get_entity(EntityType.PROJECT, "missing") is None
