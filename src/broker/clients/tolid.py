"""Client for the Tree of Life ID (ToLID) service.

Endpoint: POST https://id.tol.sanger.ac.uk/api/v3/request/create

Request body (array of one or more):
  [
    {
      "specimen_id": "<ENA sample accession>",
      "requested_taxonomy_id": <int>,
      "confirmation_name": "<scientific name>"   # optional
    }
  ]

Auth: API key via the ``api-key`` request header.

This client is always fire-and-forget: network errors and non-2xx responses
are logged as warnings but never raised.  A failed ToLID request does not
fail the ENA submission — the tolid field on the entity simply stays None.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://id.tol.sanger.ac.uk"


class ToLIDClient:
    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def request_tolid(
        self,
        specimen_id: str,
        taxonomy_id: str | int,
        confirmation_name: str | None = None,
    ) -> str | None:
        """Request a ToLID for one specimen.

        Args:
            specimen_id:       ENA sample accession (e.g. ERS123456).
            taxonomy_id:       NCBI taxonomy ID (numeric).
            confirmation_name: Scientific name — optional but recommended.

        Returns:
            The assigned ToLID string (e.g. ``fAreMarX1``), or ``None`` if
            the service did not return one (warning already logged).
        """
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
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    url,
                    json=body,
                    headers={"api-key": self._api_key},
                )
            response.raise_for_status()
            return self._extract_tolid(response.json(), specimen_id)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "ToLID request failed for specimen %s: HTTP %s — %s",
                specimen_id,
                exc.response.status_code,
                exc.response.text[:300],
            )
            return None
        except Exception as exc:
            logger.warning(
                "ToLID request failed for specimen %s: %s",
                specimen_id,
                exc,
            )
            return None

    @staticmethod
    def _extract_tolid(data: Any, specimen_id: str) -> str | None:
        """Pull the ToLID value out of the API response.

        The Sanger ToLID v3 API returns an array; each element corresponds
        to one request entry.  The assigned ID is in ``tolId``.
        """
        if not isinstance(data, list) or not data:
            logger.warning(
                "Unexpected ToLID response shape for specimen %s: %r",
                specimen_id,
                data,
            )
            return None

        first = data[0]
        # Support the documented field name and common variations
        tolid = first.get("tolId") or first.get("tol_id") or first.get("tolid")
        if not tolid:
            logger.warning(
                "No tolId field in ToLID response for specimen %s: %r",
                specimen_id,
                first,
            )
        return tolid or None
