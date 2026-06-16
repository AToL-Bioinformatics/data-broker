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


def test_project_xml_title_appends_taxon_id_suffix():
    svc = make_service()
    payload = make_payload(
        EntityType.PROJECT,
        "p1",
        {
            "title": "Short project title",
            "taxon_id": 1931064,
            "description": "A genomics project",
        },
    )
    xml = svc.to_project_xml(payload)
    root = parse_xml(xml)
    project = root.find("PROJECT")
    assert project is not None
    assert project.find("TITLE").text == "Short project title (1931064)"


def test_project_xml_title_does_not_duplicate_taxon_id_suffix():
    svc = make_service()
    payload = make_payload(
        EntityType.PROJECT,
        "p1",
        {
            "title": "Short project title (1931064)",
            "taxon_id": 1931064,
            "description": "A genomics project",
        },
    )
    xml = svc.to_project_xml(payload)
    root = parse_xml(xml)
    project = root.find("PROJECT")
    assert project is not None
    assert project.find("TITLE").text == "Short project title (1931064)"


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


def test_sample_xml_accepts_taxon_id_alias():
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE,
        "s1",
        {
            "title": "Blood sample",
            "taxon_id": "9606",
            "scientific_name": "Homo sapiens",
        },
    )
    xml = svc.to_sample_xml(payload)
    root = parse_xml(xml)
    sample = root.find("SAMPLE")
    assert sample is not None
    assert sample.find("SAMPLE_NAME/TAXON_ID").text == "9606"
    assert sample.find("SAMPLE_NAME/SCIENTIFIC_NAME").text == "Homo sapiens"


def test_sample_xml_title_not_appended_with_taxon_id():
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE,
        "s1",
        {
            "title": "Blood sample",
            "tax_id": "9606",
            "scientific_name": "Homo sapiens",
        },
    )
    xml = svc.to_sample_xml(payload)
    root = parse_xml(xml)
    sample = root.find("SAMPLE")
    assert sample is not None
    assert sample.find("TITLE").text == "Blood sample"


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
    tag_map = {a.find("TAG").text: a for a in attrs}
    # Explicitly supplied attributes are present
    assert tag_map["tissue"].find("VALUE").text == "blood"
    assert tag_map["age"].find("UNITS").text == "years"
    # Required defaults are also injected
    assert "project name" in tag_map
    assert "lifestage" in tag_map


def test_sample_xml_uses_flat_canopy_sample_fields_as_attributes():
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE,
        "s1",
        {
            "title": "Sample 102.100.100/460031 for Manorina melanotis",
            "tax_id": "1931064",
            "scientific_name": "Manorina melanotis",
            "sex": "female",
            "habitat": "",
            "lifestage": "adult",
            "collected_by": "Rohan Clarke",
            "organism part": "Blood sample",
            "collection date": "1998-09-25",
            "sample collection method": "Mist netting",
            "geographic location (country and/or sea)": "Australia",
            "geographic location (region and locality)": "Taylorville, site TAY06",
        },
    )
    xml = svc.to_sample_xml(payload)
    root = parse_xml(xml)
    attrs = root.findall("SAMPLE/SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE")
    tag_map = {a.find("TAG").text: a.find("VALUE").text for a in attrs}

    assert tag_map["sex"] == "female"
    assert tag_map["lifestage"] == "adult"
    assert tag_map["collected_by"] == "Rohan Clarke"
    assert tag_map["organism part"] == "Blood sample"
    assert tag_map["collection date"] == "1998-09-25"
    assert tag_map["collection method"] == "Mist netting"
    assert tag_map["geographic location (country and/or sea)"] == "Australia"
    assert tag_map["geographic location (region and locality)"] == "Taylorville, site TAY06"


def test_sample_xml_missing_tax_id_raises():
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE, "s1",
        {"title": "T", "scientific_name": "Homo sapiens"},
    )
    with pytest.raises(TransformError, match="tax_id' or 'taxon_id"):
        svc.to_sample_xml(payload)


def test_sample_missing_required_attributes_filled_with_defaults():
    """Missing required ATOL attributes must be injected with broker defaults."""
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE, "s1",
        {"title": "T", "tax_id": "9606", "scientific_name": "Homo sapiens"},
    )
    xml = svc.to_sample_xml(payload)
    root = parse_xml(xml)
    attrs = root.findall("SAMPLE/SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE")
    tag_map = {a.find("TAG").text: a.find("VALUE").text for a in attrs}

    not_provided_tags = [
        "lifestage", "organism part", "collected_by",
        "geographic location (region and locality)", "habitat", "sex",
        "collection method", "collecting institution",
        "collection date", "geographic location (country and/or sea)",
    ]
    for tag in not_provided_tags:
        assert tag in tag_map, f"Required attribute '{tag}' missing from XML"
        assert tag_map[tag] == "not provided", (
            f"Expected 'not provided' for '{tag}', got '{tag_map[tag]}'"
        )


def test_sample_project_name_always_atol_genome_engine():
    """'project name' must always be 'atol_genome_engine', even if payload differs."""
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE, "s1",
        {
            "title": "T",
            "tax_id": "9606",
            "scientific_name": "Homo sapiens",
            "sample_attributes": [
                {"tag": "project name", "value": "some_other_project"},
            ],
        },
    )
    xml = svc.to_sample_xml(payload)
    root = parse_xml(xml)
    attrs = root.findall("SAMPLE/SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE")
    tag_map = {a.find("TAG").text: a.find("VALUE").text for a in attrs}
    assert tag_map["project name"] == "atol_genome_engine"


def test_sample_existing_attributes_not_overwritten():
    """Provided values for required attributes must not be replaced with defaults."""
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE, "s1",
        {
            "title": "T",
            "tax_id": "9606",
            "scientific_name": "Homo sapiens",
            "sample_attributes": [
                {"tag": "sex", "value": "male"},
                {"tag": "lifestage", "value": "adult"},
            ],
        },
    )
    xml = svc.to_sample_xml(payload)
    root = parse_xml(xml)
    attrs = root.findall("SAMPLE/SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE")
    tag_map = {a.find("TAG").text: a.find("VALUE").text for a in attrs}
    assert tag_map["sex"] == "male"
    assert tag_map["lifestage"] == "adult"


def test_sample_required_attributes_case_insensitive():
    """Tag matching must be case-insensitive ('Lifestage' == 'lifestage')."""
    svc = make_service()
    payload = make_payload(
        EntityType.SAMPLE, "s1",
        {
            "title": "T",
            "tax_id": "9606",
            "scientific_name": "Homo sapiens",
            "sample_attributes": [{"tag": "Lifestage", "value": "juvenile"}],
        },
    )
    xml = svc.to_sample_xml(payload)
    root = parse_xml(xml)
    attrs = root.findall("SAMPLE/SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE")
    # Collect all lifestage-like entries (original casing preserved)
    lifestage_vals = [
        a.find("VALUE").text for a in attrs
        if a.find("TAG").text.lower() == "lifestage"
    ]
    assert lifestage_vals == ["juvenile"], (
        "Lifestage should not be duplicated by the default injection"
    )


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


def test_experiment_xml_missing_library_fields_use_defaults():
    """Missing library fields must be filled with 'missing:not provided' (layout → PAIRED)."""
    svc = make_service()
    payload = make_payload(
        EntityType.EXPERIMENT, "x1",
        {
            "title": "T",
            "platform": "ILLUMINA",
            "instrument_model": "HiSeq",
            # all library fields intentionally absent
        },
    )
    xml = svc.to_experiment_xml(payload, "PRJEB1", "ERS1")
    root = parse_xml(xml)
    lib = root.find("EXPERIMENT/DESIGN/LIBRARY_DESCRIPTOR")
    assert lib.find("LIBRARY_NAME").text == "missing:not provided"
    assert lib.find("LIBRARY_STRATEGY").text == "missing:not provided"
    assert lib.find("LIBRARY_SOURCE").text == "missing:not provided"
    assert lib.find("LIBRARY_SELECTION").text == "missing:not provided"
    assert lib.find("LIBRARY_LAYOUT/PAIRED") is not None  # layout defaults to PAIRED
    assert lib.find("LIBRARY_CONSTRUCTION_PROTOCOL").text == "missing:not provided"


def test_experiment_xml_provided_library_fields_not_overwritten():
    """Provided library fields must be used as-is, not replaced with defaults."""
    svc = make_service()
    payload = make_payload(
        EntityType.EXPERIMENT, "x1",
        {
            "title": "T",
            "platform": "ILLUMINA",
            "instrument_model": "HiSeq",
            "library_name": "lib-001",
            "library_strategy": "RNA-Seq",
            "library_source": "TRANSCRIPTOMIC",
            "library_selection": "cDNA",
            "library_layout": "SINGLE",
            "library_construction_protocol": "Standard TruSeq protocol",
        },
    )
    xml = svc.to_experiment_xml(payload, "PRJEB1", "ERS1")
    root = parse_xml(xml)
    lib = root.find("EXPERIMENT/DESIGN/LIBRARY_DESCRIPTOR")
    assert lib.find("LIBRARY_NAME").text == "lib-001"
    assert lib.find("LIBRARY_STRATEGY").text == "RNA-Seq"
    assert lib.find("LIBRARY_SOURCE").text == "TRANSCRIPTOMIC"
    assert lib.find("LIBRARY_SELECTION").text == "cDNA"
    assert lib.find("LIBRARY_LAYOUT/SINGLE") is not None
    assert lib.find("LIBRARY_CONSTRUCTION_PROTOCOL").text == "Standard TruSeq protocol"


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
