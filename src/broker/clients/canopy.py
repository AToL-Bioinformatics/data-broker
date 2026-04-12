"""Canopy API client.

ASSUMPTION: Canopy API paths and request/response shapes are inferred from
the spec. All paths are isolated here so they can be updated without touching
any other layer.

Endpoints (base URL from CANOPY_BASE_URL env var):
  POST /login              get access_token + refresh_token
  POST /refresh            exchange refresh_token for new access_token
                           ASSUMPTION: endpoint is /refresh, body {"refresh_token": "..."}
                           Adjust if the actual path or body shape differs.
  POST /claim              bulk claim by tax_id
  POST /claim/entity       targeted claim by entity type + id
  GET  /validate/{type}/{id}
  POST /report

Auth flow:
  1. On first authenticated request: POST /login with CANOPY_USERNAME + CANOPY_PASSWORD.
     Response: {"access_token": "...", "refresh_token": "..."}
  2. Set Authorization: Bearer {access_token} on subsequent requests.
  3. On 401: attempt POST /refresh with the stored refresh_token.
     On refresh success: update tokens and retry the original request once.
     On refresh failure (4xx): fall back to a full re-login and retry once.

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
from broker.models.canopy import ClaimResponse, ReportPayload, ValidationResponse

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
        # No auth headers in the shared client — tokens are injected per-request
        # so that token rotation after a refresh takes effect immediately.
        # Content-Type is intentionally omitted from default headers:
        # - JSON endpoints set it implicitly via httpx's json= kwarg
        # - The login endpoint uses application/x-www-form-urlencoded (data= kwarg)
        # Setting Content-Type here would override the per-request value.
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
    # Public API
    # ------------------------------------------------------------------

    def claim_by_tax_id(
        self,
        tax_id: str,
        entity_types: list[EntityType] | None = None,
    ) -> ClaimResponse:
        """POST /claim — bulk claim all ready entities for a tax_id.

        entity_types: if provided, request only those types; None means all.
        """
        body: dict[str, Any] = {"tax_id": tax_id}
        if entity_types is not None:
            body["entity_types"] = [str(et) for et in entity_types]
        resp = self._post("/broker/organisms/taxid{tax_id}/claim", json=body)
        return ClaimResponse.model_validate(resp.json())

    def claim_entity(self, entity_type: EntityType, entity_id: str) -> ClaimResponse:
        """POST /claim/entity — targeted claim for a single entity."""
        resp = self._post(
            "/claim/entity",
            json={"entity_type": str(entity_type), "entity_id": entity_id},
        )
        return ClaimResponse.model_validate(resp.json())

    def validate_entity(
        self, entity_type: EntityType, entity_id: str
    ) -> ValidationResponse:
        """GET /validate/{entity_type}/{entity_id}."""
        resp = self._get(f"/broker/validate/{entity_type}/{entity_id}")
        return ValidationResponse.model_validate(resp.json())

    def report_outcome(self, payload: ReportPayload) -> None:
        """POST /report — fire-and-forget; logs warning on failure.

        Submission outcome is already persisted locally, so Canopy
        reporting failure is non-fatal.
        """
        try:
            self._post("/report", json=payload.model_dump())
        except (CanopyError, httpx.TransportError) as exc:
            logger.warning(
                "Failed to report outcome to Canopy for entity %s (%s): %s",
                payload.entity_id,
                payload.entity_type,
                exc,
            )

    # ------------------------------------------------------------------
    # Auth: login and token refresh
    # ------------------------------------------------------------------

    def _ensure_authenticated(self) -> None:
        """Login if we do not yet have an access token."""
        if self._access_token is None:
            self._login()

    def _login(self) -> None:
        """POST /login with username + password; stores access_token + refresh_token."""
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
            raise CanopyError(
                status_code=resp.status_code,
                body=resp.text,
                url=url,
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        logger.debug("Canopy login successful")

    def _do_refresh(self) -> None:
        """POST /refresh with the stored refresh_token; updates stored tokens.

        ASSUMPTION: refresh endpoint is POST /refresh with body
        {"refresh_token": "..."} returning {"access_token": "...", "refresh_token": "..."}.
        Adjust the URL and body if the actual endpoint differs.

        Raises CanopyError on 4xx/5xx so the caller can fall back to re-login.
        """
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
        """Try to refresh the token; fall back to full re-login if refresh fails."""
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

    def _get(self, path: str) -> httpx.Response:
        return self._authenticated_request("GET", f"{self._base_url}{path}")

    def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._authenticated_request("POST", f"{self._base_url}{path}", **kwargs)

    def _authenticated_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Make an authenticated request, refreshing the token on 401 and retrying once."""
        self._ensure_authenticated()
        resp = self._raw_request(method, url, **kwargs)
        if resp.status_code == 401:
            logger.info("Canopy returned 401 — refreshing token and retrying")
            self._refresh_or_relogin()
            resp = self._raw_request(method, url, **kwargs)
        self._raise_for_status(resp, url)
        return resp

    def _raw_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Make a single request with the current access token.

        Retries on transient transport errors via tenacity.
        Returns the response regardless of HTTP status — callers check status.
        """
        # Copy kwargs so the retry closure captures a stable snapshot
        req_kwargs = dict(kwargs)

        @self._retry
        def _do() -> httpx.Response:
            headers = {**req_kwargs.pop("headers", {}), "Authorization": f"Bearer {self._access_token}"}
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
