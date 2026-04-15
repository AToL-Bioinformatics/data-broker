"""Canopy API client.

Base path: /api/v1/broker  (CANOPY_BASE_URL env var; paths below are relative)

Endpoints:
  POST /auth/login               get access_token + refresh_token (form-encoded)
  POST /auth/refresh             exchange refresh_token for new tokens
  POST /broker/claims/ready      bulk claim by tax_id
  POST /broker/claims/entity     targeted claim by entity type + id
  POST /broker/claims/batch      claim multiple specific entities
  POST /broker/validation        validate prerequisites
  POST /broker/reports/{id}      report submission outcomes (attempt_id in path)

Auth flow:
  1. On first authenticated request: POST /auth/login with form-encoded
     username + password.  Response: {"access_token": "...", "refresh_token": "..."}
  2. Set Authorization: Bearer {access_token} on subsequent requests.
  3. On 401: attempt POST /auth/refresh with the stored refresh_token.
     On refresh success: update tokens and retry the original request once.
     On refresh failure (4xx): fall back to full re-login and retry once.

Retry policy: exponential backoff on transient transport errors only (not 4xx).
The 401 → refresh/re-login → single retry is separate from this transport retry.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from broker.config import BrokerSettings
from broker.enums import EntityType
from broker.errors import CanopyError
from broker.models.canopy import (
    ClaimResponse,
    ReportBatchPayload,
    ValidationResponse,
)

logger = logging.getLogger(__name__)


def _make_retry_decorator(settings: BrokerSettings):
    return retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(settings.http_max_retries),
        wait=wait_exponential(
            multiplier=1,
            min=settings.http_retry_min_wait,
            max=settings.http_retry_max_wait,
        ),
        reraise=True,
    )


class CanopyClient:
    def __init__(self, settings: BrokerSettings) -> None:
        self._settings = settings
        self._base_url = str(settings.canopy_base_url).rstrip("/")
        # Content-Type is intentionally omitted from shared headers:
        # - JSON endpoints set it implicitly via httpx's json= kwarg
        # - Login uses application/x-www-form-urlencoded via data= kwarg
        self._client = httpx.Client(
            headers={"Accept": "application/json"},
            timeout=settings.http_timeout_seconds,
        )
        self._retry = _make_retry_decorator(settings)
        self._access_token: str | None = None
        self._refresh_token: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CanopyClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API — claim
    # ------------------------------------------------------------------

    def claim_by_tax_id(
        self,
        tax_id: str,
        entity_types: list[EntityType] | None = None,
    ) -> ClaimResponse:
        """POST /broker/claims/ready — bulk claim all ready entities for a tax_id.

        entity_types: optional filter for partial scope (--only flag).
        NOTE: the contract spec does not include entity_types in this endpoint
        today; the server ignores unknown fields until it is added. Tracking
        issue: add optional entity_types filter to /claims/ready.
        """
        body: dict[str, Any] = {"tax_id": tax_id}
        if entity_types is not None:
            body["entity_types"] = [str(et) for et in entity_types]
        resp = self._post("/broker/claims/ready", json=body)
        return ClaimResponse.model_validate(resp.json())

    def claim_entity(self, entity_type: EntityType, entity_id: str) -> ClaimResponse:
        """POST /broker/claims/entity — targeted claim for a single entity."""
        resp = self._post(
            "/broker/claims/entity",
            json={"entity_type": str(entity_type), "entity_id": entity_id},
        )
        return ClaimResponse.model_validate(resp.json())

    def claim_batch(
        self,
        project_ids: list[str] | None = None,
        sample_ids: list[str] | None = None,
        experiment_ids: list[str] | None = None,
        run_ids: list[str] | None = None,
    ) -> ClaimResponse:
        """POST /broker/claims/batch — claim multiple specific entities by ID."""
        body: dict[str, list[str]] = {}
        if project_ids:
            body["project_ids"] = project_ids
        if sample_ids:
            body["sample_ids"] = sample_ids
        if experiment_ids:
            body["experiment_ids"] = experiment_ids
        if run_ids:
            body["run_ids"] = run_ids
        resp = self._post("/broker/claims/batch", json=body)
        return ClaimResponse.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Public API — validation
    # ------------------------------------------------------------------

    def validate_entity(
        self,
        entity_type: EntityType,
        entity_id: str,
        overrides: dict[str, str | None] | None = None,
    ) -> ValidationResponse:
        """POST /broker/validation — validate prerequisite accessions for an entity."""
        body: dict[str, Any] = {
            "entity_type": str(entity_type),
            "entity_id": entity_id,
        }
        if overrides is not None:
            body["overrides"] = overrides
        resp = self._post("/broker/validation", json=body)
        return ValidationResponse.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Public API — reporting
    # ------------------------------------------------------------------

    def report_outcome(self, attempt_id: str, payload: ReportBatchPayload) -> None:
        """POST /broker/reports/{attempt_id} — fire-and-forget outcome reporting.

        attempt_id is in the URL path; payload body has tax_id + results list.
        Failure is logged as a warning and not re-raised — the submission outcome
        is already persisted locally, so a reporting failure can be retried later.
        """
        try:
            self._post(
                f"/broker/reports/{attempt_id}",
                json=payload.model_dump(),
            )
        except (CanopyError, httpx.TransportError) as exc:
            logger.warning(
                "Failed to report %d outcome(s) to Canopy for attempt %s: %s",
                len(payload.results),
                attempt_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Auth: login and token refresh
    # ------------------------------------------------------------------

    def _ensure_authenticated(self) -> None:
        if self._access_token is None:
            self._login()

    def _login(self) -> None:
        """POST /auth/login with form-encoded username + password."""
        url = f"{self._base_url}/auth/login"
        logger.debug("Authenticating with Canopy at %s", url)
        resp = self._client.post(
            url,
            data={
                "username": self._settings.canopy_username,
                "password": self._settings.canopy_password,
            },
        )
        if resp.status_code >= 400:
            raise CanopyError(status_code=resp.status_code, body=resp.text, url=url)
        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        logger.debug("Canopy login successful")

    def _do_refresh(self) -> None:
        """POST /auth/refresh with the stored refresh_token."""
        url = f"{self._base_url}/auth/refresh"
        resp = self._client.post(url, json={"refresh_token": self._refresh_token})
        if resp.status_code >= 400:
            raise CanopyError(status_code=resp.status_code, body=resp.text, url=url)
        data = resp.json()
        self._access_token = data["access_token"]
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]
        logger.debug("Canopy token refreshed successfully")

    def _refresh_or_relogin(self) -> None:
        if self._refresh_token:
            try:
                self._do_refresh()
                return
            except CanopyError as exc:
                logger.warning(
                    "Token refresh failed (%s) — falling back to re-login", exc.status_code
                )
        self._login()

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._authenticated_request("POST", f"{self._base_url}{path}", **kwargs)

    def _authenticated_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_authenticated()
        resp = self._raw_request(method, url, **kwargs)
        if resp.status_code == 401:
            logger.info("Canopy returned 401 — refreshing token and retrying")
            self._refresh_or_relogin()
            resp = self._raw_request(method, url, **kwargs)
        self._raise_for_status(resp, url)
        return resp

    def _raw_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        req_kwargs = dict(kwargs)

        @self._retry
        def _do() -> httpx.Response:
            headers = {
                **req_kwargs.pop("headers", {}),
                "Authorization": f"Bearer {self._access_token}",
            }
            return self._client.request(method, url, headers=headers, **req_kwargs)

        return _do()

    @staticmethod
    def _raise_for_status(resp: httpx.Response, url: str) -> None:
        if resp.status_code >= 400:
            raise CanopyError(
                status_code=resp.status_code,
                body=resp.text,
                url=url,
            )
