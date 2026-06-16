"""Client for the Tree of Life ID (ToLID) service.

Primary entrypoint:
  POST https://id-staging.tol.sanger.ac.uk/api/v3/request/create

Request body (array of one or more):
  [
    {
      "specimen_id": "<ENA sample accession>",
      "requested_taxonomy_id": <int>,
      "confirmation_name": "<scientific name>"   # optional
    }
  ]

Observed response shapes:
  1. Immediate success:
     {
       "data": [
         {
           "id": "mMacGis1",
           "type": "specimen",
           ...
         }
       ]
     }

  2. Species unresolved / async processing:
     {
       "data": [
         {
           "id": "10306",
           "type": "request",
           "attributes": {
             "status": "Pending",
             ...
           }
         }
       ]
     }

Auth: API key via the ``api-key`` request header.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://id-staging.tol.sanger.ac.uk"
_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ToLIDLookupResult:
    status: str
    tolid: str | None = None
    request_id: str | None = None
    pending_status: str | None = None
    error: str | None = None


class ToLIDClient:
    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def request_tolid(
        self,
        specimen_id: str,
        taxonomy_id: str | int,
        confirmation_name: str | None = None,
    ) -> ToLIDLookupResult:
        """Create a ToLID request for one specimen."""
        body: list[dict[str, Any]] = [
            {
                "specimen_id": specimen_id,
                "requested_taxonomy_id": int(taxonomy_id),
            }
        ]
        if confirmation_name:
            body[0]["confirmation_name"] = confirmation_name

        url = f"{self._base_url}/api/v3/request/create"
        try:
            with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
                response = client.post(
                    url,
                    json=body,
                    headers={"api-key": self._api_key},
                )
            response.raise_for_status()
            return self._extract_result(response.json(), specimen_id)
        except httpx.HTTPStatusError as exc:
            body_preview = exc.response.text[:300]
            logger.warning(
                "ToLID request failed for specimen %s: HTTP %s — %s",
                specimen_id,
                exc.response.status_code,
                body_preview,
            )
            return ToLIDLookupResult(
                status="error",
                error=f"HTTP {exc.response.status_code}: {body_preview}",
            )
        except Exception as exc:
            logger.warning(
                "ToLID request failed for specimen %s: %s",
                specimen_id,
                exc,
            )
            return ToLIDLookupResult(status="error", error=str(exc))

    def poll_tolid_request(self, request_id: str) -> ToLIDLookupResult:
        """Poll an existing ToLID request until it resolves to a specimen or remains pending."""
        candidate_urls = [
            f"{self._base_url}/api/v3/request/{request_id}",
            f"{self._base_url}/api/v3/requests/{request_id}",
        ]

        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            for url in candidate_urls:
                try:
                    response = client.get(url, headers={"api-key": self._api_key})
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                    return self._extract_result(
                        response.json(),
                        specimen_id=f"request:{request_id}",
                    )
                except httpx.HTTPStatusError as exc:
                    body_preview = exc.response.text[:300]
                    logger.warning(
                        "ToLID poll failed for request %s: HTTP %s — %s",
                        request_id,
                        exc.response.status_code,
                        body_preview,
                    )
                    return ToLIDLookupResult(
                        status="error",
                        request_id=request_id,
                        error=f"HTTP {exc.response.status_code}: {body_preview}",
                    )
                except Exception as exc:
                    logger.warning(
                        "ToLID poll failed for request %s: %s",
                        request_id,
                        exc,
                    )
                    return ToLIDLookupResult(
                        status="error",
                        request_id=request_id,
                        error=str(exc),
                    )

        logger.warning("ToLID poll failed for request %s: request not found", request_id)
        return ToLIDLookupResult(
            status="error",
            request_id=request_id,
            error="request not found",
        )

    @staticmethod
    def _extract_result(data: Any, specimen_id: str) -> ToLIDLookupResult:
        """Parse both immediate-success and async-pending response shapes."""
        first = ToLIDClient._first_item(data)
        if first is None:
            logger.warning(
                "Unexpected ToLID response shape for specimen %s: %r",
                specimen_id,
                data,
            )
            return ToLIDLookupResult(status="error", error="unexpected response shape")

        if isinstance(first, dict):
            # Backward-compatible support for older flat-list responses.
            flat_tolid = first.get("tolId") or first.get("tol_id") or first.get("tolid")
            if flat_tolid:
                return ToLIDLookupResult(status="assigned", tolid=str(flat_tolid))

        resource_type = first.get("type")
        resource_id = first.get("id")
        attributes = first.get("attributes", {}) if isinstance(first, dict) else {}

        if resource_type == "specimen" and resource_id:
            return ToLIDLookupResult(
                status="assigned",
                tolid=str(resource_id),
            )

        pending_status = attributes.get("status")
        if resource_type == "request" and pending_status:
            return ToLIDLookupResult(
                status="pending",
                request_id=str(resource_id) if resource_id is not None else None,
                pending_status=str(pending_status),
            )

        logger.warning(
            "Unrecognised ToLID response payload for specimen %s: %r",
            specimen_id,
            first,
        )
        return ToLIDLookupResult(status="error", error="unrecognised response payload")

    @staticmethod
    def _first_item(data: Any) -> dict[str, Any] | None:
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else None
        if isinstance(data, dict):
            items = data.get("data")
            if isinstance(items, list) and items and isinstance(items[0], dict):
                return items[0]
        return None
