from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from broker.clients.tolid import ToLIDLookupResult
from broker.enums import ToLIDStatus
from broker.models.canopy import ToLIDWorkItem
from broker.services.tolid_service import ToLIDService


def make_item(**overrides) -> ToLIDWorkItem:
    base = {
        "sample_id": "s1",
        "specimen_id": "ERS123",
        "taxon_id": "9606",
        "scientific_name": "Homo sapiens",
        "status": "not_requested",
        "request_id": None,
        "tolid": None,
        "last_requested_at": None,
        "sample_payload": {
            "title": "Sample ERS123 for Homo sapiens",
            "tax_id": "9606",
            "scientific_name": "Homo sapiens",
        },
    }
    base.update(overrides)
    return ToLIDWorkItem.model_validate(base)


def make_service() -> tuple[ToLIDService, dict[str, MagicMock]]:
    mocks = {
        "canopy": MagicMock(),
        "tolid": MagicMock(),
        "ena": MagicMock(),
        "transform": MagicMock(),
    }
    service = ToLIDService(
        canopy_client=mocks["canopy"],
        tolid_client=mocks["tolid"],
        ena_client=mocks["ena"],
        transform_service=mocks["transform"],
    )
    return service, mocks


def test_process_sample_accession_reports_pending():
    service, mocks = make_service()
    item = make_item()
    mocks["canopy"].get_tolid_by_specimen_accession.return_value = item
    mocks["tolid"].request_tolid.return_value = ToLIDLookupResult(
        status="pending",
        request_id="10306",
        pending_status="Pending",
    )

    result = service.process_sample_accession("ERS123", update_ena=False)

    assert result.status == ToLIDStatus.PENDING
    assert result.request_id == "10306"
    mocks["canopy"].report_tolid.assert_called_once()
    payload = mocks["canopy"].report_tolid.call_args.args[1]
    assert payload.status == ToLIDStatus.PENDING
    assert payload.request_id == "10306"


def test_process_sample_accession_reports_assignment_without_ena_update():
    service, mocks = make_service()
    item = make_item()
    mocks["canopy"].get_tolid_by_specimen_accession.return_value = item
    mocks["tolid"].request_tolid.return_value = ToLIDLookupResult(
        status="assigned",
        tolid="mMacGis1",
    )

    result = service.process_sample_accession("ERS123", update_ena=False)

    assert result.status == ToLIDStatus.ASSIGNED
    assert result.tolid == "mMacGis1"
    mocks["ena"].submit_sample.assert_not_called()
    payload = mocks["canopy"].report_tolid.call_args.args[1]
    assert payload.status == ToLIDStatus.ASSIGNED
    assert payload.tolid == "mMacGis1"


def test_process_sample_accession_error_does_not_persist_failed_status():
    service, mocks = make_service()
    item = make_item()
    mocks["canopy"].get_tolid_by_specimen_accession.return_value = item
    mocks["tolid"].request_tolid.return_value = ToLIDLookupResult(
        status="error",
        error="HTTP 503",
    )

    result = service.process_sample_accession("ERS123", update_ena=False)

    assert result.status == ToLIDStatus.NOT_REQUESTED
    assert result.error == "HTTP 503"
    mocks["canopy"].report_tolid.assert_not_called()


def test_process_pending_retries_all_returned_pending_items():
    service, mocks = make_service()
    now = datetime.now(timezone.utc)
    item = make_item(
        status="pending",
        request_id="10306",
        last_requested_at=now,
    )
    mocks["canopy"].list_pending_tolids.return_value = [item]
    mocks["tolid"].request_tolid.return_value = ToLIDLookupResult(
        status="pending",
        request_id="10306",
        pending_status="Pending",
    )

    results = service.process_pending(update_ena=False, now=now)

    assert len(results) == 1
    assert results[0].skipped is False
    assert results[0].status == ToLIDStatus.PENDING
    mocks["tolid"].request_tolid.assert_called_once()
    mocks["canopy"].report_tolid.assert_called_once()


def test_process_pending_reports_assignment():
    service, mocks = make_service()
    now = datetime.now(timezone.utc)
    item = make_item(
        status="pending",
        request_id="10306",
        last_requested_at=now,
    )
    mocks["canopy"].list_pending_tolids.return_value = [item]
    mocks["tolid"].request_tolid.return_value = ToLIDLookupResult(
        status="assigned",
        tolid="mMacGis1",
    )

    results = service.process_pending(update_ena=False, now=now)

    assert len(results) == 1
    assert results[0].status == ToLIDStatus.ASSIGNED
    mocks["tolid"].request_tolid.assert_called_once_with(
        specimen_id="ERS123",
        taxonomy_id="9606",
        confirmation_name="Homo sapiens",
    )
    payload = mocks["canopy"].report_tolid.call_args.args[1]
    assert payload.status == ToLIDStatus.ASSIGNED
    assert payload.last_requested_at == now


def test_process_sample_accession_uses_sample_payload_for_ena_modify():
    service, mocks = make_service()
    item = make_item()
    mocks["canopy"].get_tolid_by_specimen_accession.return_value = item
    mocks["tolid"].request_tolid.return_value = ToLIDLookupResult(
        status="assigned",
        tolid="mMacGis1",
    )
    mocks["transform"].to_sample_modify_xml.return_value = "<SAMPLE_SET/>"
    mocks["transform"].build_submission_xml.return_value = "<SUBMISSION/>"
    mocks["ena"].submit_sample.return_value = MagicMock(success=True, error_message=None)

    result = service.process_sample_accession("ERS123", update_ena=True)

    assert result.ena_updated is True
    mocks["transform"].to_sample_modify_xml.assert_called_once()
    modify_payload = mocks["transform"].to_sample_modify_xml.call_args.kwargs["payload"]
    assert modify_payload.data["title"] == "Sample ERS123 for Homo sapiens"
    mocks["canopy"].get_tolid.assert_not_called()


def test_process_sample_accession_synthesises_title_for_ena_modify_when_missing():
    service, mocks = make_service()
    item = make_item(
        kind="specimen",
        sample_payload={"tax_id": "9606", "scientific_name": "Homo sapiens", "kind": "specimen"},
    )
    mocks["canopy"].get_tolid_by_specimen_accession.return_value = item
    mocks["tolid"].request_tolid.return_value = ToLIDLookupResult(
        status="assigned",
        tolid="mMacGis1",
    )
    mocks["transform"].to_sample_modify_xml.return_value = "<SAMPLE_SET/>"
    mocks["transform"].build_submission_xml.return_value = "<SUBMISSION/>"
    mocks["ena"].submit_sample.return_value = MagicMock(success=True, error_message=None)

    result = service.process_sample_accession("ERS123", update_ena=True)

    assert result.ena_updated is True
    modify_payload = mocks["transform"].to_sample_modify_xml.call_args.kwargs["payload"]
    assert modify_payload.data["title"] == "Specimen ERS123 for Homo sapiens"
