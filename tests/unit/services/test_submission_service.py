"""Tests for SubmissionService — checkpoint ordering and lifecycle."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from broker.enums import AttemptMode, EntitySubmissionStatus, EntityType, SubmissionMode
from broker.errors import PrerequisiteMissingError
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.models.ena import ENAAccessions, ENASubmissionResult
from broker.services.submission_service import SubmissionService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_attempt(
    submission_mode: SubmissionMode = SubmissionMode.NORMAL,
    hold_until_date: str | None = None,
) -> AttemptState:
    state = AttemptState(
        attempt_id="atm-1",
        tax_id="9606",
        mode=AttemptMode.BULK,
        submission_mode=submission_mode,
        hold_until_date=hold_until_date,
    )
    return state


def make_project_entity() -> EntitySubmissionState:
    return EntitySubmissionState(
        entity_id="p1",
        entity_type=EntityType.PROJECT,
        raw_payload={"title": "Valid ENA project title", "description": "Desc"},
    )


def success_result(entity_id: str = "p1", accession: str = "PRJEB1") -> ENASubmissionResult:
    return ENASubmissionResult(
        entity_id=entity_id,
        success=True,
        accessions=ENAAccessions(primary_accession=accession),
        raw_receipt="<RECEIPT success='true'/>",
        http_status=200,
    )


def failure_result(entity_id: str = "p1", msg: str = "ENA error") -> ENASubmissionResult:
    return ENASubmissionResult(
        entity_id=entity_id,
        success=False,
        raw_receipt="<RECEIPT success='false'/>",
        error_message=msg,
        http_status=400,
    )


def make_service(
    ena_result: ENASubmissionResult | None = None,
) -> tuple[SubmissionService, dict]:
    """Return (SubmissionService, mocks_dict)."""
    mocks = {
        "ena_client": MagicMock(),
        "transform": MagicMock(),
        "prereq_validator": MagicMock(),
        "receipt_parser": MagicMock(),
        "state_store": MagicMock(),
        "receipt_store": MagicMock(),
    }
    # Default transform output
    mocks["transform"].to_project_xml.return_value = "<PROJECT_SET/>"
    mocks["transform"].to_sample_xml.return_value = "<SAMPLE_SET/>"
    mocks["transform"].to_experiment_xml.return_value = "<EXPERIMENT_SET/>"
    mocks["transform"].to_run_xml.return_value = "<RUN_SET/>"
    mocks["transform"].build_submission_xml.return_value = "<SUBMISSION/>"

    # Default ENA result
    if ena_result is None:
        ena_result = success_result()
    mocks["ena_client"].submit_project.return_value = ena_result
    mocks["ena_client"].submit_sample.return_value = ena_result
    mocks["ena_client"].submit_experiment.return_value = ena_result
    mocks["ena_client"].submit_run.return_value = ena_result

    # prereq_validator.resolve_prerequisites returns the entity unchanged by default
    mocks["prereq_validator"].resolve_prerequisites.side_effect = lambda entity, **kwargs: entity

    svc = SubmissionService(
        ena_client=mocks["ena_client"],
        transform_service=mocks["transform"],
        prerequisite_validator=mocks["prereq_validator"],
        receipt_parser=mocks["receipt_parser"],
        state_store=mocks["state_store"],
        receipt_store=mocks["receipt_store"],
    )
    return svc, mocks


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_submit_project_success():
    svc, mocks = make_service(success_result("p1", "PRJEB999"))
    attempt = make_attempt()
    entity = make_project_entity()
    attempt.entities[EntityType.PROJECT].append(entity)

    result = svc.submit_entity(entity, attempt)

    assert result.status == EntitySubmissionStatus.SUCCEEDED
    assert result.ena_accession == "PRJEB999"


def test_state_saved_before_ena_call():
    """SUBMITTED checkpoint must be written before the ENA POST."""
    svc, mocks = make_service()
    attempt = make_attempt()
    entity = make_project_entity()
    attempt.entities[EntityType.PROJECT].append(entity)

    save_calls_before_ena = []

    def track_ena_call(*args, **kwargs):
        # Record how many times state_store.save was called before this ENA call
        save_calls_before_ena.append(mocks["state_store"].save.call_count)
        return success_result()

    mocks["ena_client"].submit_project.side_effect = track_ena_call

    svc.submit_entity(entity, attempt)

    # state_store.save should have been called at least once before the ENA call
    assert save_calls_before_ena[0] >= 1, "State not saved before ENA call"


def test_state_saved_after_ena_call():
    """Outcome (SUCCEEDED/FAILED) must be written after the ENA POST."""
    svc, mocks = make_service()
    attempt = make_attempt()
    entity = make_project_entity()
    attempt.entities[EntityType.PROJECT].append(entity)

    svc.submit_entity(entity, attempt)

    # Must be called at least twice: once for SUBMITTED, once for outcome
    assert mocks["state_store"].save.call_count >= 2


def test_receipt_stored_verbatim():
    raw = "<RECEIPT success='true'>raw verbatim</RECEIPT>"
    svc, mocks = make_service(
        ENASubmissionResult(
            entity_id="p1",
            success=True,
            accessions=ENAAccessions(primary_accession="PRJEB1"),
            raw_receipt=raw,
        )
    )
    attempt = make_attempt()
    entity = make_project_entity()
    attempt.entities[EntityType.PROJECT].append(entity)

    svc.submit_entity(entity, attempt)

    mocks["receipt_store"].save.assert_called_once()
    _, kwargs_or_pos = mocks["receipt_store"].save.call_args
    # Either positional or keyword
    call_args = mocks["receipt_store"].save.call_args
    raw_passed = call_args[0][3] if call_args[0] else call_args[1].get("raw_receipt")
    assert raw_passed == raw


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


def test_submit_project_failure():
    svc, mocks = make_service(failure_result("p1", "Alias taken"))
    attempt = make_attempt()
    entity = make_project_entity()
    attempt.entities[EntityType.PROJECT].append(entity)

    result = svc.submit_entity(entity, attempt)

    assert result.status == EntitySubmissionStatus.FAILED
    assert "Alias taken" in result.error_message


def test_submit_project_failure_logs_xml_and_receipt(caplog):
    svc, mocks = make_service(
        ENASubmissionResult(
            entity_id="p1",
            success=False,
            raw_receipt="<RECEIPT success='false'><MESSAGES><ERROR>Alias taken</ERROR></MESSAGES></RECEIPT>",
            error_message="Alias taken",
            http_status=400,
        )
    )
    attempt = make_attempt()
    entity = make_project_entity()
    attempt.entities[EntityType.PROJECT].append(entity)

    with caplog.at_level("WARNING"):
        svc.submit_entity(entity, attempt)

    log_text = caplog.text
    assert "Failed: project p1 — Alias taken" in log_text
    assert "Submission XML for failed project p1:" in log_text
    assert "<SUBMISSION/>" in log_text
    assert "Entity XML for failed project p1:" in log_text
    assert "<PROJECT_SET/>" in log_text
    assert "ENA receipt for failed project p1:" in log_text
    assert "<RECEIPT success='false'>" in log_text


def test_prerequisite_missing_does_not_save_state():
    """If prerequisite validation fails, nothing should be persisted."""
    svc, mocks = make_service()
    mocks["prereq_validator"].resolve_prerequisites.side_effect = PrerequisiteMissingError(
        "sample", "s1", ["project_accession"]
    )
    attempt = make_attempt()
    entity = EntitySubmissionState(
        entity_id="s1",
        entity_type=EntityType.SAMPLE,
        raw_payload={},
    )
    attempt.entities[EntityType.SAMPLE].append(entity)

    with pytest.raises(PrerequisiteMissingError):
        svc.submit_entity(entity, attempt)

    # State must NOT have been saved — validation failed before any checkpoint
    mocks["state_store"].save.assert_not_called()
    mocks["ena_client"].submit_project.assert_not_called()
    mocks["ena_client"].submit_sample.assert_not_called()


def test_experiment_missing_resolved_accessions_raises_explicit_error():
    """Guard against prerequisite resolver regressions without relying on assert."""
    svc, mocks = make_service()
    attempt = make_attempt()
    entity = EntitySubmissionState(
        entity_id="x1",
        entity_type=EntityType.EXPERIMENT,
        raw_payload={
            "title": "T",
            "library_strategy": "WGS",
            "library_source": "GENOMIC",
            "library_selection": "RANDOM",
            "library_layout": "PAIRED",
            "platform": "ILLUMINA",
            "instrument_model": "HiSeq",
        },
        sample_accession="ERS1",
    )
    attempt.entities[EntityType.EXPERIMENT].append(entity)

    with pytest.raises(PrerequisiteMissingError) as exc_info:
        svc.submit_entity(entity, attempt)

    assert exc_info.value.missing == ["project_accession"]
    mocks["state_store"].save.assert_called_once_with(attempt)
    mocks["transform"].to_experiment_xml.assert_not_called()
    mocks["ena_client"].submit_experiment.assert_not_called()


def test_run_missing_resolved_accession_raises_explicit_error():
    """Run submissions must fail clearly if experiment_accession is still absent."""
    svc, mocks = make_service()
    attempt = make_attempt()
    entity = EntitySubmissionState(
        entity_id="r1",
        entity_type=EntityType.RUN,
        raw_payload={
            "files": [
                {
                    "filename": "reads.fastq.gz",
                    "filetype": "fastq",
                    "checksum": "abc123",
                    "checksum_method": "MD5",
                }
            ]
        },
    )
    attempt.entities[EntityType.RUN].append(entity)

    with pytest.raises(PrerequisiteMissingError) as exc_info:
        svc.submit_entity(entity, attempt)

    assert exc_info.value.missing == ["experiment_accession"]
    mocks["state_store"].save.assert_called_once_with(attempt)
    mocks["transform"].to_run_xml.assert_not_called()
    mocks["ena_client"].submit_run.assert_not_called()


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


def test_dry_run_marks_skipped():
    svc, mocks = make_service()
    attempt = make_attempt(submission_mode=SubmissionMode.DRY_RUN)
    entity = make_project_entity()
    attempt.entities[EntityType.PROJECT].append(entity)

    result = svc.submit_entity(entity, attempt)

    assert result.status == EntitySubmissionStatus.SKIPPED


def test_dry_run_does_not_call_ena():
    svc, mocks = make_service()
    attempt = make_attempt(submission_mode=SubmissionMode.DRY_RUN)
    entity = make_project_entity()
    attempt.entities[EntityType.PROJECT].append(entity)

    svc.submit_entity(entity, attempt)

    mocks["ena_client"].submit_project.assert_not_called()
    mocks["ena_client"].submit_sample.assert_not_called()
    mocks["ena_client"].submit_experiment.assert_not_called()
    mocks["ena_client"].submit_run.assert_not_called()


# ---------------------------------------------------------------------------
# HOLD date — only applies to project and sample
# ---------------------------------------------------------------------------


def test_hold_passed_for_project():
    svc, mocks = make_service()
    attempt = make_attempt(hold_until_date="2026-01-01")
    entity = make_project_entity()
    attempt.entities[EntityType.PROJECT].append(entity)

    svc.submit_entity(entity, attempt)

    mocks["transform"].build_submission_xml.assert_called_with(hold_until_date="2026-01-01")


def test_hold_not_passed_for_experiment():
    svc, mocks = make_service()
    attempt = make_attempt(hold_until_date="2026-01-01")
    entity = EntitySubmissionState(
        entity_id="x1",
        entity_type=EntityType.EXPERIMENT,
        raw_payload={
            "title": "T",
            "library_strategy": "WGS",
            "library_source": "GENOMIC",
            "library_selection": "RANDOM",
            "library_layout": "PAIRED",
            "platform": "ILLUMINA",
            "instrument_model": "HiSeq",
        },
        project_accession="PRJEB1",
        sample_accession="ERS1",
    )
    attempt.entities[EntityType.EXPERIMENT].append(entity)

    svc.submit_entity(entity, attempt)

    mocks["transform"].build_submission_xml.assert_called_with(hold_until_date=None)
