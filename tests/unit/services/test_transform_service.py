"""Tests for TransformService — ENA XML generation."""

from __future__ import annotations

import pytest
from lxml import etree

from broker.enums import EntityType
from broker.models.canopy import CanopyEntityPayload
from broker.services.transform_service import TransformError, TransformService

CENTER = "Webin-12345"


def make_service() -> TransformService:
    return TransformService(webin_account=CENTER)


def make_payload(entity_type: EntityType, entity_id: str, data: dict) -> CanopyEntityPayload:
    return CanopyEntityPayload(entity_id=entity_id, entity_type=entity_type, data=data)


def parse_xml(xml_str: str) -> etree._Element:
    return etree.fromstring(xml_str.encode("utf-8"))


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


def test_project_xml_basic():
    svc = make_service()
    payload = make_payload(
        EntityType.PROJECT, "p1",
        {"title": "My Project", "description": "A genomics project"},
    )
    xml = svc.to_project_xml(payload)
    root = parse_xml(xml)
    assert root.tag == "PROJECT_SET"
    project = root.find("PROJECT")
    assert project is not None
    assert project.get("alias") == "broker-project-p1"
    assert project.get("center_name") == CENTER
    assert project.find("TITLE").text == "My Project"
    assert project.find("DESCRIPTION").text == "A genomics project"
    assert project.find("SUBMISSION_PROJECT/SEQUENCING_PROJECT") is not None


def test_project_xml_missing_title_raises():
    svc = make_service()
    payload = make_payload(EntityType.PROJECT, "p1", {"description": "no title"})
    with pytest.raises(TransformError, match="title"):
        svc.to_project_xml(payload)


def test_project_xml_xml_injection_escaped():
    svc = make_service()
    payload = make_payload(
        EntityType.PROJECT, "p1",
        {"title": "<script>alert('xss')</script>", "description": "safe"},
    )
    xml = svc.to_project_xml(payload)
    # XML characters must be escaped, not injected as markup
    assert "<script>" not in xml
    assert "&lt;script&gt;" in xml


# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------


def test_sample_xml_basic():
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE, "s1",
        {
            "title": "Blood sample",
            "tax_id": "9606",
            "scientific_name": "Homo sapiens",
        },
    )
    xml = svc.to_sample_xml(payload)
    root = parse_xml(xml)
    assert root.tag == "SAMPLE_SET"
    sample = root.find("SAMPLE")
    assert sample.find("SAMPLE_NAME/TAXON_ID").text == "9606"
    assert sample.find("SAMPLE_NAME/SCIENTIFIC_NAME").text == "Homo sapiens"


def test_sample_xml_with_attributes():
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE, "s1",
        {
            "title": "T",
            "tax_id": "9606",
            "scientific_name": "Homo sapiens",
            "sample_attributes": [
                {"tag": "tissue", "value": "blood"},
                {"tag": "age", "value": "30", "units": "years"},
            ],
        },
    )
    xml = svc.to_sample_xml(payload)
    root = parse_xml(xml)
    attrs = root.findall("SAMPLE/SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE")
    assert len(attrs) == 2
    assert attrs[0].find("TAG").text == "tissue"
    assert attrs[1].find("UNITS").text == "years"


def test_sample_xml_missing_tax_id_raises():
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE, "s1",
        {"title": "T", "scientific_name": "Homo sapiens"},
    )
    with pytest.raises(TransformError, match="tax_id"):
        svc.to_sample_xml(payload)


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def test_experiment_xml_basic():
    svc = make_service()
    payload = make_payload(
        EntityType.EXPERIMENT, "x1",
        {
            "title": "WGS experiment",
            "library_strategy": "WGS",
            "library_source": "GENOMIC",
            "library_selection": "RANDOM",
            "library_layout": "PAIRED",
            "platform": "ILLUMINA",
            "instrument_model": "Illumina HiSeq 2500",
        },
    )
    xml = svc.to_experiment_xml(payload, "PRJEB12345", "ERS111111")
    root = parse_xml(xml)
    assert root.tag == "EXPERIMENT_SET"
    exp = root.find("EXPERIMENT")
    assert exp.find("STUDY_REF").get("accession") == "PRJEB12345"
    assert exp.find("DESIGN/SAMPLE_DESCRIPTOR").get("accession") == "ERS111111"
    assert exp.find("DESIGN/LIBRARY_DESCRIPTOR/LIBRARY_STRATEGY").text == "WGS"
    assert exp.find("DESIGN/LIBRARY_DESCRIPTOR/LIBRARY_LAYOUT/PAIRED") is not None
    assert exp.find("PLATFORM/ILLUMINA/INSTRUMENT_MODEL").text == "Illumina HiSeq 2500"


def test_experiment_xml_missing_platform_raises():
    svc = make_service()
    payload = make_payload(
        EntityType.EXPERIMENT, "x1",
        {
            "title": "T",
            "library_strategy": "WGS",
            "library_source": "GENOMIC",
            "library_selection": "RANDOM",
            "library_layout": "PAIRED",
            "instrument_model": "HiSeq",
        },
    )
    with pytest.raises(TransformError, match="platform"):
        svc.to_experiment_xml(payload, "PRJEB1", "ERS1")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def test_run_xml_basic():
    svc = make_service()
    payload = make_payload(
        EntityType.RUN, "r1",
        {
            "files": [
                {
                    "filename": "sample.fastq.gz",
                    "filetype": "fastq",
                    "checksum": "abc123",
                    "checksum_method": "MD5",
                }
            ]
        },
    )
    xml = svc.to_run_xml(payload, "ERX111111")
    root = parse_xml(xml)
    assert root.tag == "RUN_SET"
    run = root.find("RUN")
    assert run.find("EXPERIMENT_REF").get("accession") == "ERX111111"
    file_el = run.find("DATA_BLOCK/FILES/FILE")
    assert file_el.get("filename") == "sample.fastq.gz"
    assert file_el.get("checksum") == "abc123"


def test_run_xml_missing_files_raises():
    svc = make_service()
    payload = make_payload(EntityType.RUN, "r1", {"files": []})
    with pytest.raises(TransformError, match="files"):
        svc.to_run_xml(payload, "ERX1")


# ---------------------------------------------------------------------------
# Submission XML + HOLD
# ---------------------------------------------------------------------------


def test_submission_xml_no_hold():
    svc = make_service()
    xml = svc.build_submission_xml()
    root = parse_xml(xml)
    assert root.tag == "SUBMISSION"
    actions = root.findall("ACTIONS/ACTION")
    assert len(actions) == 1
    assert actions[0].find("ADD") is not None


def test_submission_xml_with_hold():
    svc = make_service()
    xml = svc.build_submission_xml(hold_until_date="2026-01-01")
    root = parse_xml(xml)
    actions = root.findall("ACTIONS/ACTION")
    assert len(actions) == 2
    hold = None
    for a in actions:
        h = a.find("HOLD")
        if h is not None:
            hold = h
    assert hold is not None
    assert hold.get("HoldUntilDate") == "2026-01-01"
