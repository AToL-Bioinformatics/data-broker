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
from broker.models.canopy import CanopyEntity, CanopyEntityPrerequisites, ClaimResponse
from broker.services.orchestrator import Orchestrator
from broker.services.resume_service import ResumeService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_entity(entity_type: EntityType, entity_id: str) -> CanopyEntity:
    return CanopyEntity(
        type=entity_type,
        id=entity_id,
        payload={"title": f"T-{entity_id}", "description": "D"},
    )


def make_entity_payload(entity_type: EntityType, entity_id: str) -> tuple[EntityType, CanopyEntity]:
    """Kept for backward compatibility with test call sites."""
    return entity_type, make_entity(entity_type, entity_id)


def make_claim(
    attempt_id: str, entities: list[tuple[EntityType, CanopyEntity]]
) -> ClaimResponse:
    """Build a ClaimResponse from a list of (entity_type, CanopyEntity) pairs."""
    return ClaimResponse(
        attempt_id=attempt_id,
        entities=[e for _, e in entities],
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


def make_report_service():
    return MagicMock()


def make_orchestrator(canopy_client, submission_service, state_store, report_service=None):
    return Orchestrator(
        canopy_client=canopy_client,
        submission_service=submission_service,
        state_store=state_store,
        report_service=report_service or make_report_service(),
    )


def make_resume_service(state_store, submission_service, canopy_client=None, report_service=None):
    return ResumeService(
        state_store=state_store,
        submission_service=submission_service,
        canopy_client=canopy_client or MagicMock(),
        report_service=report_service or make_report_service(),
    )


# ---------------------------------------------------------------------------
# _build_attempt_state — entity-level field propagation
# ---------------------------------------------------------------------------


def test_entity_root_fields_merged_into_raw_payload(tmp_state_store):
    """tax_id and scientific_name at entity root level must appear in raw_payload.

    Canopy returns both fields on the CanopyEntity object itself, not inside
    payload.  _build_attempt_state must merge them in so the sample XML builder
    can find them.
    """
    entity = CanopyEntity(
        type=EntityType.SAMPLE,
        id="s1",
        tax_id="9606",
        scientific_name="Homo sapiens",
        payload={"title": "Homo sapiens blood"},
    )
    claim = ClaimResponse(attempt_id="atm-tx", entities=[entity])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    attempt = orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)

    sample_entity = attempt.entities[EntityType.SAMPLE][0]
    assert sample_entity.raw_payload["tax_id"] == "9606"
    assert sample_entity.raw_payload["scientific_name"] == "Homo sapiens"


def test_entity_payload_fields_not_overwritten(tmp_state_store):
    """If tax_id / scientific_name are already inside payload, payload wins."""
    entity = CanopyEntity(
        type=EntityType.SAMPLE,
        id="s2",
        tax_id="9606",
        scientific_name="Homo sapiens",
        payload={"title": "T", "tax_id": "10090", "scientific_name": "Mus musculus"},
    )
    claim = ClaimResponse(attempt_id="atm-tx2", entities=[entity])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    attempt = orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)

    sample_entity = attempt.entities[EntityType.SAMPLE][0]
    assert sample_entity.raw_payload["tax_id"] == "10090"
    assert sample_entity.raw_payload["scientific_name"] == "Mus musculus"


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
    resume_svc = make_resume_service(tmp_state_store, submission_svc)
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
    resume_svc = make_resume_service(tmp_state_store, submission_svc)
    resume_svc.resume("resume-2")

    submission_svc.submit_entity.assert_not_called()


def test_resume_not_found_raises(tmp_state_store):
    submission_svc = make_mock_submission_service()
    resume_svc = make_resume_service(tmp_state_store, submission_svc)
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
    resume_svc = make_resume_service(tmp_state_store, submission_svc)
    resume_svc.resume("resume-3")

    # Should have been re-submitted
    submission_svc.submit_entity.assert_called_once()


# ---------------------------------------------------------------------------
# Batch submission
# ---------------------------------------------------------------------------


def test_batch_calls_claim_batch(tmp_state_store):
    """run_batch must call claim_batch with the provided entity IDs."""
    claim = make_claim("atm-b1", [make_entity_payload(EntityType.SAMPLE, "s1")])
    canopy = MagicMock()
    canopy.claim_batch.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    orchestrator.run_batch(sample_ids=["s1", "s2"])

    canopy.claim_batch.assert_called_once_with(
        project_ids=None,
        sample_ids=["s1", "s2"],
        experiment_ids=None,
        run_ids=None,
    )


def test_batch_mode_stored_as_targeted(tmp_state_store):
    """Batch attempts are stored with TARGETED mode — no silent state fallback."""
    claim = make_claim("atm-b2", [make_entity_payload(EntityType.PROJECT, "p1")])
    canopy = MagicMock()
    canopy.claim_batch.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    attempt = orchestrator.run_batch(project_ids=["p1"])

    assert attempt.mode == AttemptMode.TARGETED


def test_batch_disables_state_fallback(tmp_state_store):
    """Batch mode passes allow_state_fallback=False to SubmissionService."""
    claim = make_claim("atm-b3", [make_entity_payload(EntityType.SAMPLE, "s1")])
    canopy = MagicMock()
    canopy.claim_batch.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    orchestrator.run_batch(sample_ids=["s1"])

    call_kwargs = submission_svc.submit_entity.call_args
    assert call_kwargs.kwargs.get("allow_state_fallback") is False


# ---------------------------------------------------------------------------
# Finalise on error
# ---------------------------------------------------------------------------


def test_bulk_error_calls_finalise(tmp_state_store):
    """run_bulk must call finalise_claim when _run_entities raises."""
    claim = make_claim("atm-fin1", [make_entity_payload(EntityType.PROJECT, "p1")])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim

    submission_svc = MagicMock()
    submission_svc.submit_entity.side_effect = RuntimeError("boom")

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    with pytest.raises(RuntimeError):
        orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)

    canopy.finalise_claim.assert_called_once_with("atm-fin1")


def test_bulk_error_reraises_original_exception(tmp_state_store):
    """run_bulk must re-raise the original exception after calling finalise."""
    claim = make_claim("atm-fin2", [make_entity_payload(EntityType.PROJECT, "p1")])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim

    submission_svc = MagicMock()
    submission_svc.submit_entity.side_effect = PrerequisiteMissingError(
        "sample", "s1", ["project_accession"]
    )

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    with pytest.raises(PrerequisiteMissingError):
        orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)

    canopy.finalise_claim.assert_called_once()


def test_targeted_error_calls_finalise(tmp_state_store):
    """run_targeted must call finalise_claim when submission raises."""
    claim = make_claim("atm-fin3", [make_entity_payload(EntityType.SAMPLE, "s1")])
    canopy = MagicMock()
    canopy.claim_entity.return_value = claim

    submission_svc = MagicMock()
    submission_svc.submit_entity.side_effect = RuntimeError("network down")

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    with pytest.raises(RuntimeError):
        orchestrator.run_targeted(EntityType.SAMPLE, "s1")

    canopy.finalise_claim.assert_called_once_with("atm-fin3")


def test_batch_error_calls_finalise(tmp_state_store):
    """run_batch must call finalise_claim when submission raises."""
    claim = make_claim("atm-fin4", [make_entity_payload(EntityType.SAMPLE, "s1")])
    canopy = MagicMock()
    canopy.claim_batch.return_value = claim

    submission_svc = MagicMock()
    submission_svc.submit_entity.side_effect = RuntimeError("uh oh")

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    with pytest.raises(RuntimeError):
        orchestrator.run_batch(sample_ids=["s1"])

    canopy.finalise_claim.assert_called_once_with("atm-fin4")


def test_successful_run_calls_finalise(tmp_state_store):
    """finalise_claim must be called on success too — the finally block always runs."""
    claim = make_claim("atm-fin5", [make_entity_payload(EntityType.PROJECT, "p1")])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim
    submission_svc = make_mock_submission_service()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store)
    orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)

    canopy.finalise_claim.assert_called_once_with("atm-fin5")


def test_successful_run_calls_report_attempt(tmp_state_store):
    """report_service.report_attempt must be called after every successful run."""
    claim = make_claim("atm-fin6", [make_entity_payload(EntityType.PROJECT, "p1")])
    canopy = MagicMock()
    canopy.claim_by_tax_id.return_value = claim
    submission_svc = make_mock_submission_service()
    report_svc = MagicMock()

    orchestrator = make_orchestrator(canopy, submission_svc, tmp_state_store, report_service=report_svc)
    orchestrator.run_bulk("9606", only=None, submission_mode=SubmissionMode.NORMAL)

    report_svc.report_attempt.assert_called_once()


def test_resume_reports_all_entities_including_terminal(tmp_state_store):
    """resume must report ALL entities (including already-SUCCEEDED) to Canopy.

    This covers the crash window where an entity was marked SUCCEEDED locally
    but the per-entity report never fired.  Resume re-reports everything so
    Canopy always gets a complete picture.
    """
    state = AttemptState(
        attempt_id="resume-report-1",
        tax_id="9606",
        mode=AttemptMode.BULK,
        status=AttemptStatus.IN_PROGRESS,
    )
    proj = EntitySubmissionState(
        entity_id="p1",
        entity_type=EntityType.PROJECT,
        raw_payload={"title": "T", "description": "D"},
    )
    proj.mark_succeeded("PRJEB999")  # already terminal before resume
    sample = EntitySubmissionState(
        entity_id="s1",
        entity_type=EntityType.SAMPLE,
        raw_payload={"title": "T", "tax_id": "9606", "scientific_name": "Homo sapiens"},
    )
    state.entities[EntityType.PROJECT].append(proj)
    state.entities[EntityType.SAMPLE].append(sample)
    tmp_state_store.save(state)

    canopy = MagicMock()
    report_svc = MagicMock()
    submission_svc = make_mock_submission_service()
    resume_svc = make_resume_service(tmp_state_store, submission_svc, canopy_client=canopy, report_service=report_svc)
    resume_svc.resume("resume-report-1")

    # report_attempt called once, covering both entities
    report_svc.report_attempt.assert_called_once()
    # finalise called to close the lease
    canopy.finalise_claim.assert_called_once_with("resume-report-1")


def test_resume_calls_finalise_even_on_error(tmp_state_store):
    """resume must finalise even when submission raises."""
    state = AttemptState(
        attempt_id="resume-err-1",
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

    canopy = MagicMock()
    report_svc = MagicMock()
    submission_svc = MagicMock()
    submission_svc.submit_entity.side_effect = RuntimeError("network failure")

    resume_svc = make_resume_service(tmp_state_store, submission_svc, canopy_client=canopy, report_service=report_svc)
    with pytest.raises(RuntimeError):
        resume_svc.resume("resume-err-1")

    canopy.finalise_claim.assert_called_once_with("resume-err-1")
    report_svc.report_attempt.assert_called_once()


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
    resume_svc = make_resume_service(tmp_state_store, submission_svc)
    resume_svc.resume("resume-4")

    loaded = tmp_state_store.load("resume-4")
    assert loaded.status == AttemptStatus.COMPLETED
