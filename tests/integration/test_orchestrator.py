"""Integration tests for Orchestrator and ResumeService.

Uses mocked CanopyClient and SubmissionService to verify:
- Dependency ordering (projects before samples, etc.)
- --only filter disables state fallback for prerequisites
- State file exists on disk after attempt completes
- Resume skips already-succeeded entities
- Resume re-submits non-terminal entities
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from broker.enums import (
    AttemptMode,
    AttemptStatus,
    EntitySubmissionStatus,
    EntityType,
    SubmissionMode,
)
from broker.errors import AttemptNotFoundError, PrerequisiteMissingError
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.models.canopy import CanopyEntity, ClaimResponse
from broker.services.orchestrator import Orchestrator
from broker.services.resume_service import ResumeService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_entity(entity_id: str) -> CanopyEntity:
    return CanopyEntity(
        id=entity_id,
        prepared_payload={"title": f"T-{entity_id}", "description": "D"},
    )


def make_entity_payload(entity_type: EntityType, entity_id: str) -> tuple[EntityType, CanopyEntity]:
    """Return (entity_type, CanopyEntity) for use with make_claim."""
    return entity_type, make_entity(entity_id)


def make_claim(
    attempt_id: str, entities: list[tuple[EntityType, CanopyEntity]]
) -> ClaimResponse:
    """Build a ClaimResponse from a list of (entity_type, CanopyEntity) pairs."""
    projects = [e for et, e in entities if et == EntityType.PROJECT]
    samples = [e for et, e in entities if et == EntityType.SAMPLE]
    experiments = [e for et, e in entities if et == EntityType.EXPERIMENT]
    reads = [e for et, e in entities if et == EntityType.RUN]
    return ClaimResponse(
        attempt_id=attempt_id,
        projects=projects,
        samples=samples,
        experiments=experiments,
        reads=reads,
    )


def make_mock_submission_service(side_effect=None):
    svc = MagicMock()
    if side_effect:
        svc.submit_entity.side_effect = side_effect
    else:
        # Default: mark each entity as succeeded on call
        def succeed(entity, attempt_state, cli_overrides=None, allow_state_fallback=True):
            entity.mark_succeeded(f"ACC-{entity.entity_id}")
            return entity
        svc.submit_entity.side_effect = succeed
    return svc


def make_orchestrator(canopy_client, submission_service, state_store):
    return Orchestrator(
        canopy_client=canopy_client,
        submission_service=submission_service,
        state_store=state_store,
    )


# ---------------------------------------------------------------------------
# Bulk submission — ordering
# ---------------------------------------------------------------------------


def test_bulk_submits_in_dependency_order(tmp_state_store):
    """Entities must be submitted: project → sample → experiment → run."""
    entities = [
        make_entity_payload(EntityType.RUN, "r1"),
        make_entity_payload(EntityType.EXPERIMENT, "x1"),
        make_entity_payload(EntityType.SAMPLE, "s1"),
        make_entity_payload(EntityType.PROJECT, "p1"),
    ]
    claim = make_claim("atm-1", entities)
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim

    submitted_order = []

    def track_submit(entity, attempt_state, cli_overrides=None, allow_state_fallback=True):
        submitted_order.append(entity.entity_type)
        entity.mark_succeeded(f"ACC-{entity.entity_id}")
        return entity

    submission_svc = MagicMock()
    submission_svc.submit_entity.side_effect = track_submit

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)

    assert submitted_order == [
        EntityType.PROJECT,
        EntityType.SAMPLE,
        EntityType.EXPERIMENT,
        EntityType.RUN,
    ]


def test_bulk_state_file_created(tmp_state_store):
    """A state file must exist on disk after run_bulk completes."""
    claim = make_claim("atm-2", [make_entity_payload(EntityType.PROJECT, "p1")])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    attempt = orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)

    loaded = tmp_state_store.load("atm-2")
    assert loaded.attempt_id == "atm-2"


def test_bulk_final_status_is_completed(tmp_state_store):
    claim = make_claim("atm-3", [make_entity_payload(EntityType.PROJECT, "p1")])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    attempt = orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)

    assert attempt.status == AttemptStatus.COMPLETED


def test_bulk_with_only_filter_claims_correct_types(tmp_state_store):
    """--only samples should pass entity_types=[sample] to Canopy."""
    claim = make_claim("atm-4", [make_entity_payload(EntityType.SAMPLE, "s1")])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    orchestrator.run_bulk("9606", only=EntityType.SAMPLE, submission_mode=SubmissionMode.NORMAL)

    canopy.claim_by_tax_id.assert_called_once_with("9606", entity_types=[EntityType.SAMPLE])


def test_bulk_only_disables_state_fallback(tmp_state_store):
    """--only mode must pass allow_state_fallback=False to SubmissionService."""
    claim = make_claim("atm-5", [make_entity_payload(EntityType.SAMPLE, "s1")])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    orchestrator.run_bulk("9606", only=EntityType.SAMPLE, submission_mode=SubmissionMode.NORMAL)

    call_kwargs = submission_svc.submit_entity.call_args
    assert call_kwargs.kwargs.get("allow_state_fallback") is False or \
           (call_kwargs.args and not call_kwargs.args[3] if len(call_kwargs.args) > 3 else False)


def test_bulk_prerequisite_missing_propagates(tmp_state_store):
    """PrerequisiteMissingError must propagate from run_bulk without being swallowed."""
    claim = make_claim("atm-6", [make_entity_payload(EntityType.SAMPLE, "s1")])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim

    submission_svc = MagicMock()
    submission_svc.submit_entity.side_effect = PrerequisiteMissingError(
        "sample", "s1", ["project_accession"]
    )

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    with pytest.raises(PrerequisiteMissingError):
        orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)


# ---------------------------------------------------------------------------
# Targeted submission
# ---------------------------------------------------------------------------


def test_targeted_disables_state_fallback(tmp_state_store):
    claim = make_claim("atm-7", [make_entity_payload(EntityType.PROJECT, "p1")])
    canopy = MagicMock()
    canopy.claim_entity.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    orchestrator.run_targeted(EntityType.PROJECT, "p1")

    call_kwargs = submission_svc.submit_entity.call_args
    assert call_kwargs.kwargs.get("allow_state_fallback") is False


def test_targeted_mode_is_stored_in_state(tmp_state_store):
    claim = make_claim("atm-8", [make_entity_payload(EntityType.PROJECT, "p1")])
    canopy = MagicMock()
    canopy.claim_entity.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    attempt = orchestrator.run_targeted(EntityType.PROJECT, "p1")

    assert attempt.mode == AttemptMode.TARGETED


# ---------------------------------------------------------------------------
# Resume service
# ---------------------------------------------------------------------------


def test_resume_skips_succeeded_entities(tmp_state_store):
    """Resume must not re-submit entities already in SUCCEEDED state."""
    # Create a persisted attempt with one succeeded + one pending entity
    state = AttemptState(
        attempt_id="resume-1",
        tax_id="9606",
        mode=AttemptMode.BULK,
        status=AttemptStatus.IN_PROGRESS,
    )
    proj = EntitySubmissionState(
        entity_id="p1",
        entity_type=EntityType.PROJECT,
        raw_payload={"title": "T", "description": "D"},
    )
    proj.mark_succeeded("PRJEB999")
    sample = EntitySubmissionState(
        entity_id="s1",
        entity_type=EntityType.SAMPLE,
        raw_payload={"title": "T", "tax_id": "9606", "scientific_name": "Homo sapiens"},
    )
    state.entities[EntityType.PROJECT].append(proj)
    state.entities[EntityType.SAMPLE].append(sample)
    tmp_state_store.save(state)

    submission_svc = make_mock_submission_service()
    resume_svc = ResumeService(tmp_state_store, submission_svc)
    resume_svc.resume("resume-1")

    # Only s1 should have been submitted; p1 was already succeeded
    calls = submission_svc.submit_entity.call_args_list
    assert len(calls) == 1
    submitted_entity = calls[0].args[0] if calls[0].args else calls[0].kwargs["entity"]
    assert submitted_entity.entity_id == "s1"


def test_resume_already_completed_returns_immediately(tmp_state_store):
    """Resuming an already-completed attempt is a no-op."""
    state = AttemptState(
        attempt_id="resume-2",
        tax_id="9606",
        mode=AttemptMode.BULK,
        status=AttemptStatus.COMPLETED,
    )
    tmp_state_store.save(state)

    submission_svc = make_mock_submission_service()
    resume_svc = ResumeService(tmp_state_store, submission_svc)
    resume_svc.resume("resume-2")

    submission_svc.submit_entity.assert_not_called()


def test_resume_not_found_raises(tmp_state_store):
    submission_svc = make_mock_submission_service()
    resume_svc = ResumeService(tmp_state_store, submission_svc)
    with pytest.raises(AttemptNotFoundError):
        resume_svc.resume("nonexistent-id")


def test_resume_submitted_state_is_retried(tmp_state_store):
    """SUBMITTED (crash checkpoint) is not terminal — must be re-attempted."""
    state = AttemptState(
        attempt_id="resume-3",
        tax_id="9606",
        mode=AttemptMode.BULK,
        status=AttemptStatus.IN_PROGRESS,
    )
    proj = EntitySubmissionState(
        entity_id="p1",
        entity_type=EntityType.PROJECT,
        raw_payload={"title": "T", "description": "D"},
    )
    proj.mark_submitted()  # crashed mid-flight
    state.entities[EntityType.PROJECT].append(proj)
    tmp_state_store.save(state)

    submission_svc = make_mock_submission_service()
    resume_svc = ResumeService(tmp_state_store, submission_svc)
    resume_svc.resume("resume-3")

    # Should have been re-submitted
    submission_svc.submit_entity.assert_called_once()


def test_resume_final_status_written(tmp_state_store):
    state = AttemptState(
        attempt_id="resume-4",
        tax_id="9606",
        mode=AttemptMode.BULK,
        status=AttemptStatus.IN_PROGRESS,
    )
    proj = EntitySubmissionState(
        entity_id="p1",
        entity_type=EntityType.PROJECT,
        raw_payload={"title": "T", "description": "D"},
    )
    state.entities[EntityType.PROJECT].append(proj)
    tmp_state_store.save(state)

    submission_svc = make_mock_submission_service()
    resume_svc = ResumeService(tmp_state_store, submission_svc)
    resume_svc.resume("resume-4")

    loaded = tmp_state_store.load("resume-4")
    assert loaded.status == AttemptStatus.COMPLETED
