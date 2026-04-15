"""Report submission outcomes back to ATOL.

Reporting is fire-and-forget: if the server is unavailable, a warning is logged
but no exception is raised. The submission outcome is already persisted locally
in the state store, so a reporting failure can be retried separately.

Contract status values (the ONLY values the server accepts):
  "accepted"   — entity was accepted by ENA
  "rejected"   — entity was rejected by ENA
  "submitting" — entity has been posted to ENA but outcome is not yet confirmed
                 (maps to our internal SUBMITTED checkpoint status)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from broker.clients.canopy import CanopyClient
from broker.enums import EntitySubmissionStatus
from broker.models.attempt import EntitySubmissionState
from broker.models.canopy import ReportBatchPayload, ReportResult

logger = logging.getLogger(__name__)

# Internal status → contract status
_STATUS_MAP: dict[EntitySubmissionStatus, str] = {
    EntitySubmissionStatus.SUCCEEDED: "accepted",
    EntitySubmissionStatus.FAILED: "rejected",
    EntitySubmissionStatus.SUBMITTED: "submitting",
}


class ReportService:
    def __init__(self, canopy_client: CanopyClient) -> None:
        self._canopy = canopy_client

    def report(
        self,
        attempt_id: str,
        entity: EntitySubmissionState,
        raw_receipt: str | None,
        receipt_path: Path | None = None,
        tax_id: str | None = None,
    ) -> None:
        """Build a ReportBatchPayload (single result) and send it to Canopy.

        Args:
            attempt_id:    Attempt UUID (goes in the URL path).
            entity:        The entity whose outcome we're reporting.
            raw_receipt:   Raw ENA response body for traceability.
            receipt_path:  Filesystem path where the receipt was stored.
            tax_id:        Organism tax_id for the report body.
        """
        contract_status = _STATUS_MAP.get(entity.status, "rejected")

        result = ReportResult(
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
            status=contract_status,
            accession=entity.ena_accession,
            secondary_accession=entity.biosample_accession,
            receipt_path=str(receipt_path) if receipt_path else None,
            message=(
                "Submission accepted"
                if entity.status == EntitySubmissionStatus.SUCCEEDED
                else entity.error_message
            ),
            errors=[entity.error_message] if entity.error_message and entity.status == EntitySubmissionStatus.FAILED else [],
            response_payload={"receipt": raw_receipt} if raw_receipt else None,
        )

        batch = ReportBatchPayload(tax_id=tax_id, results=[result])

        logger.info(
            "Reporting %s outcome to Canopy: %s %s → %s",
            contract_status,
            entity.entity_type,
            entity.entity_id,
            entity.ena_accession or "no accession",
        )
        self._canopy.report_outcome(attempt_id=attempt_id, payload=batch)
