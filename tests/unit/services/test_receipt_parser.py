"""Tests for ReceiptParser — ENA XML and JSON receipt parsing."""

from __future__ import annotations

from broker.enums import EntityType
from broker.services.receipt_parser import ReceiptParser


def parser() -> ReceiptParser:
    return ReceiptParser()


# ---------------------------------------------------------------------------
# XML receipts — success
# ---------------------------------------------------------------------------

PROJECT_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<RECEIPT receiptDate="2024-01-15" submissionFile="sub.xml" success="true">
  <PROJECT accession="PRJEB12345" alias="broker-project-p1" status="PRIVATE"/>
  <SUBMISSION accession="ERA999999" alias="sub-1"/>
  <MESSAGES><INFO>Submission has been committed.</INFO></MESSAGES>
  <ACTIONS>ADD</ACTIONS>
</RECEIPT>"""

SAMPLE_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<RECEIPT success="true">
  <SAMPLE accession="ERS111111" alias="broker-sample-s1" status="PRIVATE">
    <EXT_ID accession="SAMEA999999" type="biosample"/>
  </SAMPLE>
  <SUBMISSION accession="ERA000001" alias="sub-2"/>
  <MESSAGES/>
</RECEIPT>"""

EXPERIMENT_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<RECEIPT success="true">
  <EXPERIMENT accession="ERX222222" alias="broker-experiment-x1" status="PRIVATE"/>
  <SUBMISSION accession="ERA000002" alias="sub-3"/>
</RECEIPT>"""

RUN_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<RECEIPT success="true">
  <RUN accession="ERR333333" alias="broker-run-r1" status="PRIVATE"/>
  <SUBMISSION accession="ERA000003" alias="sub-4"/>
</RECEIPT>"""

FAILURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<RECEIPT success="false">
  <MESSAGES>
    <ERROR>Invalid alias format.</ERROR>
    <ERROR>Study reference not found.</ERROR>
  </MESSAGES>
</RECEIPT>"""


def test_parse_project_success():
    result = parser().parse(PROJECT_SUCCESS_XML, "p1", EntityType.PROJECT)
    assert result.success is True
    assert result.accessions.primary_accession == "PRJEB12345"
    assert result.accessions.alias == "broker-project-p1"
    assert result.accessions.biosample_accession is None
    assert result.raw_receipt == PROJECT_SUCCESS_XML


def test_parse_sample_success_with_biosample():
    result = parser().parse(SAMPLE_SUCCESS_XML, "s1", EntityType.SAMPLE)
    assert result.success is True
    assert result.accessions.primary_accession == "ERS111111"
    assert result.accessions.biosample_accession == "SAMEA999999"


def test_parse_experiment_success():
    result = parser().parse(EXPERIMENT_SUCCESS_XML, "x1", EntityType.EXPERIMENT)
    assert result.success is True
    assert result.accessions.primary_accession == "ERX222222"


def test_parse_run_success():
    result = parser().parse(RUN_SUCCESS_XML, "r1", EntityType.RUN)
    assert result.success is True
    assert result.accessions.primary_accession == "ERR333333"


def test_parse_failure_extracts_errors():
    result = parser().parse(FAILURE_XML, "p1", EntityType.PROJECT)
    assert result.success is False
    assert "Invalid alias format" in result.error_message
    assert "Study reference not found" in result.error_message
    assert result.accessions is None


def test_parse_malformed_xml_returns_failure():
    result = parser().parse("<RECEIPT success='true'><UNCLOSED>", "p1", EntityType.PROJECT)
    assert result.success is False
    assert "could not be parsed" in result.error_message.lower()


def test_parse_success_missing_entity_element():
    # success=true but no PROJECT element — should return failure with clear message
    xml = "<RECEIPT success='true'><SUBMISSION accession='ERA1'/></RECEIPT>"
    result = parser().parse(xml, "p1", EntityType.PROJECT)
    assert result.success is False
    assert "PROJECT" in result.error_message


def test_raw_receipt_preserved_verbatim():
    result = parser().parse(FAILURE_XML, "p1", EntityType.PROJECT)
    assert result.raw_receipt == FAILURE_XML


# ---------------------------------------------------------------------------
# JSON receipts
# ---------------------------------------------------------------------------

JSON_SUCCESS = '{"accession": "PRJEB99999", "primaryAccession": "PRJEB99999"}'
JSON_WITH_BIOSAMPLE = '{"accession": "ERS999", "bioSampleAccession": "SAMEA001"}'
JSON_FAILURE = '{"errors": ["Bad request", "Missing field"]}'


def test_parse_json_success():
    result = parser().parse(JSON_SUCCESS, "p1", EntityType.PROJECT)
    assert result.success is True
    assert result.accessions.primary_accession == "PRJEB99999"


def test_parse_json_with_biosample():
    result = parser().parse(JSON_WITH_BIOSAMPLE, "s1", EntityType.SAMPLE)
    assert result.success is True
    assert result.accessions.biosample_accession == "SAMEA001"


def test_parse_json_failure():
    result = parser().parse(JSON_FAILURE, "p1", EntityType.PROJECT)
    assert result.success is False
    assert "Bad request" in result.error_message


def test_parse_invalid_json_returns_failure():
    result = parser().parse("{not: valid}", "p1", EntityType.PROJECT)
    assert result.success is False
    assert "could not be parsed" in result.error_message.lower()
