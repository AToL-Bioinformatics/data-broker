"""Report submission outcomes back to Canopy.

Reporting is fire-and-forget: if Canopy is unavailable, a warning is logged
but no exception is raised. The submission outcome is already persisted locally
in the state store, so a reporting failure can be retried separately.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from broker.clients.canopy import CanopyClient
from broker.enums import EntitySubmissionStatus
from broker.models.attempt import EntitySubmissionState
from broker.models.canopy import ReportPayload

logger = logging.getLogger(__name__)


class ReportService:
    def __init__(self, canopy_client: CanopyClient) -> None:
        self._canopy = canopy_client

    def report(
        self,
        attempt_id: str,
        entity: EntitySubmissionState,
        raw_receipt: str | None,
    ) -> None:
        """Construct a ReportPayload and send it to Canopy.

        Failure is logged as a warning; it does not raise.
        """
        status = (
            "succeeded"
            if entity.status == EntitySubmissionStatus.SUCCEEDED
            else "failed"
        )
        timestamp = (
            entity.succeeded_at or datetime.now(timezone.utc)
        ).isoformat()

        payload = ReportPayload(
            attempt_id=attempt_id,
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            status=status,
            accession=entity.ena_accession,
            biosample_accession=entity.biosample_accession,
            raw_receipt=raw_receipt,
            submission_timestamp=timestamp,
            error_message=entity.error_message,
        )

        logger.info(
            "Reporting %s outcome to Canopy: %s %s → %s",
            status,
            entity.entity_type,
            entity.entity_id,
            entity.ena_accession or "no accession",
        )
        self._canopy.report_outcome(payload)
