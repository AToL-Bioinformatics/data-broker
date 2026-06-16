from __future__ import annotations

from unittest.mock import MagicMock

from broker.clients.tolid import ToLIDLookupResult
from broker.enums import AttemptMode, AttemptStatus, EntitySubmissionStatus, EntityType
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.services.tolid_service import ToLIDService


def make_attempt() -> AttemptState:
    return AttemptState(
        attempt_id="atm-tolid-1",
        tax_id="9606",
        mode=AttemptMode.BULK,
        status=AttemptStatus.IN_PROGRESS,
    )


def make_sample() -> EntitySubmissionState:
    entity = EntitySubmissionState(
        entity_id="s1",
        entity_type=EntityType.SAMPLE,
        status=EntitySubmissionStatus.SUCCEEDED,
        ena_accession="ERS123",
        raw_payload={
            "kind": "specimen",
            "tax_id": "9606",
            "scientific_name": "Homo sapiens",
        },
    )
    return entity


def make_service() -> tuple[ToLIDService, dict[str, MagicMock]]:
    mocks = {
        "tolid_client": MagicMock(),
        "ena_client": MagicMock(),
        "transform": MagicMock(),
        "state_store": MagicMock(),
    }
    service = ToLIDService(
        tolid_client=mocks["tolid_client"],
        ena_client=mocks["ena_client"],
        transform_service=mocks["transform"],
        state_store=mocks["state_store"],
    )
    return service, mocks


def test_process_one_stores_pending_request_state():
    service, mocks = make_service()
    attempt = make_attempt()
    entity = make_sample()
    attempt.entities[EntityType.SAMPLE].append(entity)

    mocks["tolid_client"].request_tolid.return_value = ToLIDLookupResult(
        status="pending",
        request_id="10306",
        pending_status="Pending",
    )

    result = service._process_one(entity, attempt, update_ena=False)

    assert result.pending is True
    assert result.pending_request_id == "10306"
    assert entity.tolid is None
    assert entity.tolid_request_id == "10306"
    assert entity.tolid_status == "Pending"
    mocks["state_store"].save.assert_called_once_with(attempt)
    mocks["ena_client"].submit_sample.assert_not_called()


def test_process_one_polls_existing_request_and_assigns_tolid():
    service, mocks = make_service()
    attempt = make_attempt()
    entity = make_sample()
    entity.tolid_request_id = "10306"
    entity.tolid_status = "Pending"
    attempt.entities[EntityType.SAMPLE].append(entity)

    mocks["tolid_client"].poll_tolid_request.return_value = ToLIDLookupResult(
        status="assigned",
        tolid="mMacGis1",
    )

    result = service._process_one(entity, attempt, update_ena=False)

    assert result.pending is False
    assert result.tolid == "mMacGis1"
    assert entity.tolid == "mMacGis1"
    assert entity.tolid_request_id is None
    assert entity.tolid_status is None
    mocks["tolid_client"].request_tolid.assert_not_called()
    mocks["tolid_client"].poll_tolid_request.assert_called_once_with("10306")
    mocks["state_store"].save.assert_called_once_with(attempt)
