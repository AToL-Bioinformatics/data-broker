"""Integration tests for CanopyClient — mocked HTTP via pytest-httpx."""

from __future__ import annotations

import json

import pytest

from broker.clients.canopy import CanopyClient
from broker.config import BrokerSettings
from broker.enums import EntityType
from broker.errors import CanopyError
from broker.models.canopy import (
    ClaimResponse,
    ReportBatchPayload,
    ReportResult,
    ToLIDReportPayload,
)


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


def make_settings() -> BrokerSettings:
    return BrokerSettings(
        WEBIN_USERNAME="Webin-test",
        WEBIN_PASSWORD="secret",
        CANOPY_BASE_URL="http://canopy.test",
        CANOPY_USERNAME="user@example.com",
        CANOPY_PASSWORD="hunter2",
    )


LOGIN_RESPONSE = {
    "access_token": "tok-access-abc",
    "refresh_token": "tok-refresh-xyz",
}

REFRESH_RESPONSE = {
    "access_token": "tok-access-NEW",
    "refresh_token": "tok-refresh-NEW",
}

# Flat entity list per new contract
CLAIM_RESPONSE = {
    "attempt_id": "atm-123",
    "tax_id": "9606",
    "scope": "full",
    "entities": [
        {
            "type": "project",
            "id": "p1",
            "tax_id": "9606",
            "payload": {"title": "Test", "description": "Desc"},
            "prerequisites": None,
            "files": [],
        }
    ],
}

VALIDATION_RESPONSE = {
    "entity_type": "project",
    "entity_id": "p1",
    "valid": True,
    "issues": [],
    "resolved_prerequisites": {},
}

REPORT_RESPONSE = {"updated_count": 1}
TOLID_LIST_RESPONSE = {
    "items": [
        {
            "sample_id": "s1",
            "specimen_id": "ERS123",
            "taxon_id": "9606",
            "scientific_name": "Homo sapiens",
            "status": "pending",
            "request_id": "10306",
            "tolid": None,
            "last_requested_at": "2026-06-15T12:00:00Z",
        }
    ]
}


def add_login_mock(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/auth/login",
        json=LOGIN_RESPONSE,
        status_code=200,
    )


def make_report_payload(attempt_id: str = "atm-1") -> ReportBatchPayload:
    return ReportBatchPayload(
        tax_id="9606",
        results=[
            ReportResult(
                entity_type=EntityType.PROJECT,
                entity_id="p1",
                status="accepted",
                accession="PRJEB1",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Login behaviour
# ---------------------------------------------------------------------------


def test_login_called_on_first_request(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_by_tax_id("9606")
    assert httpx_mock.get_requests()[0].url.path == "/auth/login"


def test_login_uses_username_password(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_by_tax_id("9606")
    login_req = httpx_mock.get_requests()[0]
    assert login_req.headers["content-type"] == "application/x-www-form-urlencoded"
    from urllib.parse import parse_qs
    body = parse_qs(login_req.content.decode())
    assert body["username"] == ["user@example.com"]
    assert body["password"] == ["hunter2"]


def test_login_not_repeated_on_subsequent_requests(httpx_mock):
    add_login_mock(httpx_mock)
    # Both calls go to the same endpoint (tax_id is now in the body)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_by_tax_id("9606")
    client.claim_by_tax_id("10090")
    login_calls = [r for r in httpx_mock.get_requests() if r.url.path == "/auth/login"]
    assert len(login_calls) == 1


def test_login_failure_raises_canopy_error(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/auth/login",
        text="Invalid credentials",
        status_code=401,
    )
    client = CanopyClient(make_settings())
    with pytest.raises(CanopyError) as exc_info:
        client.claim_by_tax_id("9606")
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Token refresh on 401
# ---------------------------------------------------------------------------


def test_401_triggers_token_refresh(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", status_code=401
    )
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/auth/refresh", json=REFRESH_RESPONSE
    )
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    result = client.claim_by_tax_id("9606")
    assert result.attempt_id == "atm-123"
    refresh_calls = [r for r in httpx_mock.get_requests() if r.url.path == "/auth/refresh"]
    assert len(refresh_calls) == 1
    body = json.loads(refresh_calls[0].content)
    assert body["refresh_token"] == "tok-refresh-xyz"


def test_401_with_refresh_failure_falls_back_to_relogin(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", status_code=401
    )
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/auth/refresh", status_code=401
    )
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/auth/login", json=LOGIN_RESPONSE
    )
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    result = client.claim_by_tax_id("9606")
    assert result.attempt_id == "atm-123"
    login_calls = [r for r in httpx_mock.get_requests() if r.url.path == "/auth/login"]
    assert len(login_calls) == 2


def test_access_token_sent_as_bearer(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_by_tax_id("9606")
    claim_req = httpx_mock.get_requests()[1]  # [0]=login, [1]=claim
    assert claim_req.headers["Authorization"] == "Bearer tok-access-abc"


# ---------------------------------------------------------------------------
# Claim endpoints
# ---------------------------------------------------------------------------


def test_claim_by_tax_id_success(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    result = client.claim_by_tax_id("9606")
    assert isinstance(result, ClaimResponse)


def test_claim_by_tax_id_accepts_taxon_id_alias_in_response(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/claims/ready",
        json={
            "attempt_id": "atm-124",
            "taxon_id": "9606",
            "scope": "full",
            "entities": [
                {
                    "type": "sample",
                    "id": "s1",
                    "taxon_id": "9606",
                    "scientific_name": "Homo sapiens",
                    "payload": {"title": "Sample"},
                    "prerequisites": None,
                    "files": [],
                }
            ],
        },
    )
    client = CanopyClient(make_settings())
    result = client.claim_by_tax_id("9606")
    assert result.tax_id == "9606"
    assert result.entities[0].tax_id == "9606"
    assert result.attempt_id == "atm-124"
    assert result.entities[0].id == "s1"


def test_claim_by_tax_id_body_contains_tax_id(httpx_mock):
    """tax_id is sent in the request body (not the URL)."""
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_by_tax_id("10090")
    claim_req = httpx_mock.get_requests()[1]
    body = json.loads(claim_req.content)
    assert body["taxon_id"] == "10090"


def test_claim_by_tax_id_with_entity_type_filter(httpx_mock):
    """entity_types filter is included in the request body when --only is used."""
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/ready", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_by_tax_id("9606", entity_types=[EntityType.PROJECT])
    claim_req = httpx_mock.get_requests()[1]
    body = json.loads(claim_req.content)
    assert "entity_types" in body
    assert "project" in body["entity_types"]


def test_claim_entity_success(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/entity", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    result = client.claim_entity(EntityType.PROJECT, "p1")
    assert result.attempt_id == "atm-123"


def test_claim_entity_body(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/entity", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_entity(EntityType.SAMPLE, "s1")
    req = httpx_mock.get_requests()[1]
    body = json.loads(req.content)
    assert body["entity_type"] == "sample"
    assert body["entity_id"] == "s1"


def test_claim_batch_success(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/batch", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    result = client.claim_batch(sample_ids=["s1", "s2"], experiment_ids=["x1"])
    assert result.attempt_id == "atm-123"


def test_claim_batch_body(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/claims/batch", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_batch(sample_ids=["s1", "s2"], experiment_ids=["x1"])
    req = httpx_mock.get_requests()[1]
    body = json.loads(req.content)
    assert body["sample_ids"] == ["s1", "s2"]
    assert body["experiment_ids"] == ["x1"]
    assert "project_ids" not in body  # empty lists are omitted


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_entity(httpx_mock):
    """validate_entity now uses POST /broker/validation."""
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/validation",
        json=VALIDATION_RESPONSE,
    )
    client = CanopyClient(make_settings())
    result = client.validate_entity(EntityType.PROJECT, "p1")
    assert result.valid is True
    assert result.entity_id == "p1"
    # Verify the request body
    req = httpx_mock.get_requests()[1]
    body = json.loads(req.content)
    assert body["entity_type"] == "project"
    assert body["entity_id"] == "p1"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_canopy_4xx_raises_canopy_error(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/claims/ready",
        text="Not found",
        status_code=404,
    )
    client = CanopyClient(make_settings())
    with pytest.raises(CanopyError) as exc_info:
        client.claim_by_tax_id("9606")
    assert exc_info.value.status_code == 404


def test_canopy_5xx_raises_canopy_error(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/claims/ready",
        text="Internal error",
        status_code=500,
    )
    client = CanopyClient(make_settings())
    with pytest.raises(CanopyError) as exc_info:
        client.claim_by_tax_id("9606")
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_report_outcome_success(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/reports/atm-1",
        json=REPORT_RESPONSE,
    )
    client = CanopyClient(make_settings())
    client.report_outcome("atm-1", make_report_payload("atm-1"))


def test_report_outcome_attempt_id_in_url(httpx_mock):
    """attempt_id must appear in the URL path, not the body."""
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/reports/atm-xyz",
        json=REPORT_RESPONSE,
    )
    client = CanopyClient(make_settings())
    client.report_outcome("atm-xyz", make_report_payload("atm-xyz"))
    report_req = [r for r in httpx_mock.get_requests() if "reports" in str(r.url)][0]
    assert "atm-xyz" in str(report_req.url)
    body = json.loads(report_req.content)
    assert "attempt_id" not in body  # attempt_id is path-only


def test_report_outcome_body_shape(httpx_mock):
    """Report body must have tax_id + results array with correct status values."""
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/broker/reports/atm-1", json=REPORT_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.report_outcome("atm-1", make_report_payload("atm-1"))
    report_req = [r for r in httpx_mock.get_requests() if "reports" in str(r.url)][0]
    body = json.loads(report_req.content)
    assert "results" in body
    assert body["results"][0]["status"] == "accepted"
    assert body["results"][0]["entity_type"] == "project"


def test_report_outcome_failure_does_not_raise(httpx_mock):
    """report_outcome logs a warning on failure but never raises."""
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/reports/atm-1",
        text="Service unavailable",
        status_code=503,
    )
    client = CanopyClient(make_settings())
    client.report_outcome("atm-1", make_report_payload("atm-1"))  # must not raise


# ---------------------------------------------------------------------------
# ToLID endpoints
# ---------------------------------------------------------------------------


def test_get_tolid_by_specimen_accession(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url="http://canopy.test/broker/tolids/by-specimen-accession/ERS123",
        json=TOLID_LIST_RESPONSE["items"][0],
    )
    client = CanopyClient(make_settings())
    item = client.get_tolid_by_specimen_accession("ERS123")
    assert item.sample_id == "s1"
    assert item.tax_id == "9606"


def test_list_pending_tolids(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url="http://canopy.test/broker/tolids/pending?sample_id=s1",
        json=TOLID_LIST_RESPONSE,
    )
    client = CanopyClient(make_settings())
    items = client.list_pending_tolids(sample_id="s1")
    assert len(items) == 1
    assert items[0].request_id == "10306"


def test_get_tolid(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url="http://canopy.test/broker/tolids/s1",
        json=TOLID_LIST_RESPONSE["items"][0],
    )
    client = CanopyClient(make_settings())
    item = client.get_tolid("s1")
    assert item.sample_id == "s1"
    assert item.specimen_id == "ERS123"


def test_report_tolid(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/tolids/s1/report",
        json={"ok": True},
    )
    client = CanopyClient(make_settings())
    payload = ToLIDReportPayload(
        status="pending",
        request_id="10306",
        last_requested_at="2026-06-16T00:00:00Z",
    )
    client.report_tolid("s1", payload)
    req = [r for r in httpx_mock.get_requests() if "tolids/s1/report" in str(r.url)][0]
    body = json.loads(req.content)
    assert body["status"] == "pending"
    assert body["request_id"] == "10306"


# ---------------------------------------------------------------------------
# Finalise claim
# ---------------------------------------------------------------------------


def test_finalise_claim_posts_to_correct_url(httpx_mock):
    """finalise_claim must POST to /broker/attempts/{attempt_id}/finalise."""
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/attempts/atm-xyz/finalise",
        json={"ok": True},
        status_code=200,
    )
    client = CanopyClient(make_settings())
    client.finalise_claim("atm-xyz")

    finalise_reqs = [r for r in httpx_mock.get_requests() if "finalise" in r.url.path]
    assert len(finalise_reqs) == 1
    assert finalise_reqs[0].url.path == "/broker/attempts/atm-xyz/finalise"


def test_finalise_claim_failure_does_not_raise(httpx_mock):
    """finalise_claim logs a warning on failure but never raises."""
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/broker/attempts/atm-xyz/finalise",
        text="Service unavailable",
        status_code=503,
    )
    client = CanopyClient(make_settings())
    client.finalise_claim("atm-xyz")  # must not raise
