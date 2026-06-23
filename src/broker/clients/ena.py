"""ENA Webin drop-box submission client.

Submits ENA XML to the drop-box endpoint via multipart POST.
The drop-box accepts two parts:
  SUBMISSION: the submission XML envelope (action, optional HOLD date)
  {entity_set}: the entity XML (PROJECT_SET, SAMPLE_SET, etc.)

Auth: HTTP Basic (WEBIN_USERNAME / WEBIN_PASSWORD).

Critical invariant: ENAClient NEVER raises on ENA API errors.
All outcomes are returned as ENASubmissionResult(success=False).
This guarantees the persistence step in SubmissionService is never bypassed.

Retry policy: exponential backoff on transient transport errors only.
HTTP 4xx/5xx from ENA are NOT retried — they are returned as failures.
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
from broker.models.ena import ENASubmissionResult
from broker.services.receipt_parser import ReceiptParser

logger = logging.getLogger(__name__)

# Maps entity type → multipart field name for the entity XML part.
# ENA drop-box uses these specific field names.
_ENTITY_PART_NAME: dict[EntityType, str] = {
    EntityType.PROJECT: "PROJECT",
    EntityType.SAMPLE: "SAMPLE",
    EntityType.EXPERIMENT: "EXPERIMENT",
    EntityType.RUN: "RUN",
}


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


class ENAClient:
    def __init__(self, settings: BrokerSettings, base_url: str | None = None) -> None:
        self._settings = settings
        self._url = str(base_url or settings.ena_dev_base_url).rstrip("/")
        self._auth = (settings.webin_username, settings.webin_password)
        self._receipt_parser = ReceiptParser()
        self._client = httpx.Client(
            auth=self._auth,
            timeout=settings.http_timeout_seconds,
        )
        self._retry = _make_retry_decorator(settings)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ENAClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public: per-entity submission
    # ------------------------------------------------------------------

    def submit_project(
        self, entity_id: str, submission_xml: str, entity_xml: str
    ) -> ENASubmissionResult:
        return self._post_xml(entity_id, EntityType.PROJECT, submission_xml, entity_xml)

    def submit_sample(
        self, entity_id: str, submission_xml: str, entity_xml: str
    ) -> ENASubmissionResult:
        return self._post_xml(entity_id, EntityType.SAMPLE, submission_xml, entity_xml)

    def submit_experiment(
        self, entity_id: str, submission_xml: str, entity_xml: str
    ) -> ENASubmissionResult:
        return self._post_xml(entity_id, EntityType.EXPERIMENT, submission_xml, entity_xml)

    def submit_run(
        self, entity_id: str, submission_xml: str, entity_xml: str
    ) -> ENASubmissionResult:
        return self._post_xml(entity_id, EntityType.RUN, submission_xml, entity_xml)

    # ------------------------------------------------------------------
    # Internal: shared XML POST + receipt parsing
    # ------------------------------------------------------------------

    def _post_xml(
        self,
        entity_id: str,
        entity_type: EntityType,
        submission_xml: str,
        entity_xml: str,
    ) -> ENASubmissionResult:
        """POST submission + entity XML to ENA drop-box.

        Captures the raw response body BEFORE any parsing.
        Never raises — all error paths return ENASubmissionResult(success=False).
        """
        part_name = _ENTITY_PART_NAME[entity_type]
        files = {
            "SUBMISSION": ("submission.xml", submission_xml, "text/xml"),
            part_name: (f"{part_name.lower()}.xml", entity_xml, "text/xml"),
        }
        raw_receipt = ""
        http_status: int | None = None

        try:
            @self._retry
            def _do() -> httpx.Response:
                return self._client.post(self._url, files=files)

            resp = _do()
            raw_receipt = resp.text
            http_status = resp.status_code
        except httpx.TransportError as exc:
            # Transport-level failure even after retries
            logger.error("ENA transport error for %s %s: %s", entity_type, entity_id, exc)
            return ENASubmissionResult(
                entity_id=entity_id,
                success=False,
                raw_receipt=raw_receipt,
                error_message=f"Network error communicating with ENA: {exc}",
                http_status=None,
            )
        except Exception as exc:
            logger.error("Unexpected error submitting %s %s: %s", entity_type, entity_id, exc)
            return ENASubmissionResult(
                entity_id=entity_id,
                success=False,
                raw_receipt=raw_receipt,
                error_message=f"Unexpected error: {exc}",
                http_status=http_status,
            )

        # Parse the receipt regardless of HTTP status code —
        # ENA sometimes returns 200 with success=false in the XML.
        result = self._receipt_parser.parse(raw_receipt, entity_id, entity_type)
        result.http_status = http_status
        return result
