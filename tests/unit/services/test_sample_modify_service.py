from __future__ import annotations

from unittest.mock import MagicMock

from lxml import etree

from broker.models.canopy import SampleModifyItem
from broker.models.ena import ENASubmissionResult
from broker.services.sample_modify_service import SampleModifyService
from broker.services.transform_service import TransformService


def make_item(**payload_overrides) -> SampleModifyItem:
    payload = {
        "title": "Sample 102.100.100/1 for Homo sapiens",
        "sex": "female",
        "sample derived from": "SAMEA999",
    }
    payload.update(payload_overrides)
    return SampleModifyItem.model_validate(
        {
            "accession": "ERS111",
            "biosample_accession": "SAMEA111",
            "entity": {
                "type": "sample",
                "id": "s1",
                "taxon_id": "9606",
                "scientific_name": "Homo sapiens",
                "payload": payload,
            },
        }
    )


def make_service(report_to_canopy: bool = True, items=None):
    canopy, ena = MagicMock(), MagicMock()
    canopy.list_sample_modify_pending.return_value = items or [make_item()]
    ena.submit_sample.return_value = ENASubmissionResult(entity_id="s1", success=True)
    service = SampleModifyService(
        canopy_client=canopy,
        ena_client=ena,
        transform_service=TransformService(webin_account="Webin-1"),
        report_to_canopy=report_to_canopy,
    )
    return service, canopy, ena


def _attrs(sample_xml: str) -> dict[str, str]:
    root = etree.fromstring(sample_xml.encode())
    return {a.findtext("TAG"): a.findtext("VALUE") for a in root.iter("SAMPLE_ATTRIBUTE")}


def test_modify_submits_full_sample_with_accession_and_reports_back():
    service, canopy, ena = make_service()

    [result] = service.process_pending(tax_id="9606", limit=5)

    canopy.list_sample_modify_pending.assert_called_once_with(tax_id="9606", limit=5)
    _, submission_xml, sample_xml = ena.submit_sample.call_args.args
    assert "<MODIFY/>" in submission_xml
    sample = etree.fromstring(sample_xml.encode()).find("SAMPLE")
    assert sample.get("accession") == "ERS111"
    assert sample.findtext("SAMPLE_NAME/TAXON_ID") == "9606"
    attrs = _attrs(sample_xml)
    assert attrs["sample derived from"] == "SAMEA999"
    assert attrs["sex"] == "female"  # rest of the record kept: MODIFY replaces it all
    canopy.report_sample_modified.assert_called_once_with("s1", {"sample derived from": "SAMEA999"})
    assert (result.ena_updated, result.reported, result.error) == (True, True, None)


def test_dry_run_builds_xml_but_sends_nothing():
    service, canopy, ena = make_service()

    [result] = service.process_pending(dry_run=True)

    assert "SAMEA999" in result.sample_xml
    ena.submit_sample.assert_not_called()
    canopy.report_sample_modified.assert_not_called()


def test_ena_failure_is_not_reported_to_canopy():
    service, canopy, ena = make_service()
    ena.submit_sample.return_value = ENASubmissionResult(
        entity_id="s1", success=False, error_message="unknown accession"
    )

    [result] = service.process_pending()

    assert result.ena_updated is False
    assert "unknown accession" in result.error
    canopy.report_sample_modified.assert_not_called()


def test_dev_mode_updates_ena_without_reporting_to_canopy():
    service, canopy, ena = make_service(report_to_canopy=False)

    [result] = service.process_pending()

    ena.submit_sample.assert_called_once()
    canopy.report_sample_modified.assert_not_called()
    assert (result.ena_updated, result.reported) == (True, False)


def test_bad_sample_is_skipped_and_the_rest_still_processed():
    service, canopy, ena = make_service(items=[make_item(title=None), make_item()])

    bad, good = service.process_pending()

    assert "Could not build MODIFY XML" in bad.error
    assert good.ena_updated is True
    assert ena.submit_sample.call_count == 1


def test_modify_xml_without_tolid_adds_no_tolid_attribute():
    xml = TransformService(webin_account="Webin-1").to_sample_modify_xml(
        payload=SampleModifyService._to_payload(make_item()), ena_accession="ERS111"
    )
    assert "tolid" not in _attrs(xml)
