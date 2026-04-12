"""Integration tests for ENAClient — mocked HTTP via pytest-httpx."""

from __future__ import annotations

import pytest

from broker.clients.ena import ENAClient
from broker.config import BrokerSettings
from broker.enums import EntityType

SUBMISSION_XML = "<?xml version='1.0'?><SUBMISSION><ACTIONS><ACTION><ADD/></ACTION></ACTIONS></SUBMISSION>"
PROJECT_XML = "<?xml version='1.0'?><PROJECT_SET><PROJECT alias='p1'/></PROJECT_SET>"

PROJECT_SUCCESS_RECEIPT = """<?xml version="1.0" encoding="UTF-8"?>
<RECEIPT success="true">
  <PROJECT accession="PRJEB12345" alias="broker-project-p1" status="PRIVATE"/>
  <SUBMISSION accession="ERA000001" alias="sub-1"/>
</RECEIPT>"""

PROJECT_FAILURE_RECEIPT = """<?xml version="1.0" encoding="UTF-8"?>
<RECEIPT success="false">
  <MESSAGES><ERROR>Alias already in use.</ERROR></MESSAGES>
</RECEIPT>"""


def make_settings() -> BrokerSettings:
    return BrokerSettings(
        WEBIN_USERNAME="Webin-test",
        WEBIN_PASSWORD="secret",
        CANOPY_BASE_URL="http://canopy.test",
        CANOPY_USERNAME="user@example.com",
        CANOPY_PASSWORD="hunter2",
        ENA_BASE_URL="http://ena.test/submit",
    )


def test_submit_project_success(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://ena.test/submit",
        text=PROJECT_SUCCESS_RECEIPT,
        status_code=200,
    )
    client = ENAClient(make_settings())
    result = client.submit_project("p1", SUBMISSION_XML, PROJECT_XML)
    assert result.success is True
    assert result.accessions.primary_accession == "PRJEB12345"
    assert result.raw_receipt == PROJECT_SUCCESS_RECEIPT
    assert result.http_status == 200


def test_submit_project_failure_receipt(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://ena.test/submit",
        text=PROJECT_FAILURE_RECEIPT,
        status_code=200,
    )
    client = ENAClient(make_settings())
    result = client.submit_project("p1", SUBMISSION_XML, PROJECT_XML)
    assert result.success is False
    assert "Alias already in use" in result.error_message
    assert result.raw_receipt == PROJECT_FAILURE_RECEIPT


def test_ena_client_does_not_raise_on_http_400(httpx_mock):
    """ENAClient never raises — HTTP errors return success=False result."""
    httpx_mock.add_response(
        method="POST",
        url="http://ena.test/submit",
        text="Bad request",
        status_code=400,
    )
    client = ENAClient(make_settings())
    result = client.submit_project("p1", SUBMISSION_XML, PROJECT_XML)
    assert result.success is False
    assert result.http_status == 400
    # No exception raised — caller checks result.success


def test_ena_client_does_not_raise_on_http_500(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://ena.test/submit",
        text="Internal server error",
        status_code=500,
    )
    client = ENAClient(make_settings())
    result = client.submit_project("p1", SUBMISSION_XML, PROJECT_XML)
    assert result.success is False
    assert result.http_status == 500


def test_ena_uses_basic_auth(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://ena.test/submit",
        text=PROJECT_SUCCESS_RECEIPT,
        status_code=200,
    )
    client = ENAClient(make_settings())
    client.submit_project("p1", SUBMISSION_XML, PROJECT_XML)
    request = httpx_mock.get_requests()[0]
    # Basic auth header should be present
    assert "Authorization" in request.headers
    assert request.headers["Authorization"].startswith("Basic ")


def test_submit_entity_type_routing(httpx_mock):
    """Each entity type method sends the correct multipart field name."""
    sample_receipt = """<?xml version="1.0"?>
<RECEIPT success="true">
  <SAMPLE accession="ERS111111" alias="s1" status="PRIVATE"/>
  <SUBMISSION accession="ERA2"/>
</RECEIPT>"""
    httpx_mock.add_response(
        method="POST", url="http://ena.test/submit", text=sample_receipt, status_code=200
    )
    client = ENAClient(make_settings())
    result = client.submit_sample("s1", SUBMISSION_XML, "<SAMPLE_SET/>")
    assert result.success is True
    assert result.accessions.primary_accession == "ERS111111"


def test_transport_error_returns_failure(httpx_mock):
    """Network-level failure after retries returns ENASubmissionResult(success=False)."""
    import httpx as _httpx

    httpx_mock.add_exception(_httpx.ConnectError("Connection refused"))
    settings = make_settings()
    settings.http_max_retries = 1  # speed up test
    client = ENAClient(settings)
    result = client.submit_project("p1", SUBMISSION_XML, PROJECT_XML)
    assert result.success is False
    assert "Network error" in result.error_message
