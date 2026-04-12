"""Integration tests for CanopyClient — mocked HTTP via pytest-httpx."""

from __future__ import annotations

import json

import pytest

from broker.clients.canopy import CanopyClient
from broker.config import BrokerSettings
from broker.enums import EntityType
from broker.errors import CanopyError
from broker.models.canopy import ClaimResponse, ReportPayload


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

CLAIM_RESPONSE = {
    "attempt_id": "atm-123",
    "entities": [
        {
            "entity_id": "p1",
            "entity_type": "project",
            "data": {"title": "Test", "description": "Desc"},
            "project_accession": None,
            "sample_accession": None,
            "experiment_accession": None,
        }
    ],
}

VALIDATION_RESPONSE = {
    "entity_id": "p1",
    "entity_type": "project",
    "valid": True,
    "errors": [],
    "prerequisites": {},
}

REPORT_RESPONSE = {"status": "ok"}


def add_login_mock(httpx_mock) -> None:
    """Add a /login mock. Called before any test that triggers an authenticated request."""
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/auth/login",
        json=LOGIN_RESPONSE,
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Login behaviour
# ---------------------------------------------------------------------------


def test_login_called_on_first_request(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_by_tax_id("9606")
    # First request should be to /auth/login
    assert httpx_mock.get_requests()[0].url.path == "/auth/login"


def test_login_uses_username_password(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_by_tax_id("9606")
    login_req = httpx_mock.get_requests()[0]
    # Login uses x-www-form-urlencoded, not JSON
    assert login_req.headers["content-type"] == "application/x-www-form-urlencoded"
    from urllib.parse import parse_qs
    body = parse_qs(login_req.content.decode())
    assert body["username"] == ["user@example.com"]
    assert body["password"] == ["hunter2"]


def test_login_not_repeated_on_subsequent_requests(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", json=CLAIM_RESPONSE
    )
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", json=CLAIM_RESPONSE
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
    """On 401 from a business endpoint, client should call /refresh and retry."""
    add_login_mock(httpx_mock)
    # First /claim returns 401 (token expired)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", status_code=401
    )
    # /refresh returns new tokens
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/auth/refresh", json=REFRESH_RESPONSE
    )
    # Retry /claim succeeds
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    result = client.claim_by_tax_id("9606")
    assert result.attempt_id == "atm-123"
    # Verify /refresh was called
    refresh_calls = [r for r in httpx_mock.get_requests() if r.url.path == "/auth/refresh"]
    assert len(refresh_calls) == 1
    body = json.loads(refresh_calls[0].content)
    assert body["refresh_token"] == "tok-refresh-xyz"


def test_401_with_refresh_failure_falls_back_to_relogin(httpx_mock):
    """/refresh failure causes a full re-login, then retries the original request."""
    add_login_mock(httpx_mock)
    # First /claim → 401
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", status_code=401
    )
    # /refresh → 401 (refresh token also expired)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/auth/refresh", status_code=401
    )
    # Re-login succeeds
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/auth/login", json=LOGIN_RESPONSE
    )
    # Retry /claim succeeds
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    result = client.claim_by_tax_id("9606")
    assert result.attempt_id == "atm-123"
    login_calls = [r for r in httpx_mock.get_requests() if r.url.path == "/auth/login"]
    assert len(login_calls) == 2  # initial login + re-login


def test_access_token_sent_as_bearer(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    client.claim_by_tax_id("9606")
    claim_req = httpx_mock.get_requests()[1]  # [0] = login, [1] = /claim
    assert claim_req.headers["Authorization"] == "Bearer tok-access-abc"


# ---------------------------------------------------------------------------
# Business endpoint tests (same as before, now with login mock)
# ---------------------------------------------------------------------------


def test_claim_by_tax_id_success(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    result = client.claim_by_tax_id("9606")
    assert isinstance(result, ClaimResponse)
    assert result.attempt_id == "atm-123"
    assert result.entities[0].entity_id == "p1"


def test_claim_by_tax_id_with_entity_type_filter(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", json=CLAIM_RESPONSE
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
        method="POST", url="http://canopy.test/claim/entity", json=CLAIM_RESPONSE
    )
    client = CanopyClient(make_settings())
    result = client.claim_entity(EntityType.PROJECT, "p1")
    assert result.attempt_id == "atm-123"


def test_canopy_4xx_raises_canopy_error(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", text="Not found", status_code=404
    )
    client = CanopyClient(make_settings())
    with pytest.raises(CanopyError) as exc_info:
        client.claim_by_tax_id("9606")
    assert exc_info.value.status_code == 404


def test_canopy_5xx_raises_canopy_error(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/claim", text="Internal error", status_code=500
    )
    client = CanopyClient(make_settings())
    with pytest.raises(CanopyError) as exc_info:
        client.claim_by_tax_id("9606")
    assert exc_info.value.status_code == 500


def test_validate_entity(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url="http://canopy.test/validate/project/p1",
        json=VALIDATION_RESPONSE,
    )
    client = CanopyClient(make_settings())
    result = client.validate_entity(EntityType.PROJECT, "p1")
    assert result.valid is True
    assert result.entity_id == "p1"


def test_report_outcome_success(httpx_mock):
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST", url="http://canopy.test/report", json=REPORT_RESPONSE
    )
    client = CanopyClient(make_settings())
    payload = ReportPayload(
        attempt_id="atm-1",
        entity_id="p1",
        entity_type=EntityType.PROJECT,
        status="succeeded",
        accession="PRJEB1",
    )
    client.report_outcome(payload)


def test_report_outcome_failure_does_not_raise(httpx_mock):
    """report_outcome logs a warning on failure but never raises."""
    add_login_mock(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url="http://canopy.test/report",
        text="Service unavailable",
        status_code=503,
    )
    client = CanopyClient(make_settings())
    payload = ReportPayload(
        attempt_id="atm-1",
        entity_id="p1",
        entity_type=EntityType.PROJECT,
        status="succeeded",
    )
    client.report_outcome(payload)  # must not raise
