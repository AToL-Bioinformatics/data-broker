"""Report submission outcomes back to Canopy.

Reporting is fire-and-forget: if the server is unavailable, a warning is logged
but no exception is raised. The submission outcome is already persisted locally
in the state store, so a reporting failure can be retried separately.

Contract status values (the ONLY values the server accepts):
  "accepted"   — entity was accepted by ENA
  "rejected"   — entity was rejected by ENA
  "submitting" — entity has been posted to ENA but outcome is not yet confirmed
                 (maps to our internal SUBMITTED checkpoint status)

Only entities in SUCCEEDED, FAILED, or SUBMITTED states are included in the
report.  PENDING entities have never been attempted; SKIPPED entities were
dry-run only — neither should be reported to Canopy.
"""

from __future__ import annotations

import logging

from broker.clients.canopy import CanopyClient
from broker.enums import EntitySubmissionStatus
from broker.models.attempt import AttemptState
from broker.models.canopy import ReportBatchPayload, ReportResult

logger = logging.getLogger(__name__)

# Internal status → contract status
_STATUS_MAP: dict[EntitySubmissionStatus, str] = {
    EntitySubmissionStatus.SUCCEEDED: "accepted",
    EntitySubmissionStatus.FAILED: "rejected",
    EntitySubmissionStatus.SUBMITTED: "submitting",
}

# Only entities in these states have a meaningful outcome to report
_REPORTABLE_STATUSES = frozenset(_STATUS_MAP.keys())


class ReportService:
    def __init__(self, canopy_client: CanopyClient) -> None:
        self._canopy = canopy_client

    def report_attempt(self, attempt: AttemptState) -> None:
        """Batch-report ALL reportable entity outcomes for an attempt.

        Collects every entity that is in SUCCEEDED, FAILED, or SUBMITTED state
        and sends them in a single POST /broker/reports/{attempt_id}.

        Entities that are PENDING (never attempted) or SKIPPED (dry-run) are
        excluded — they have no meaningful outcome to report.

        This method is intended to be called from a ``finally`` block so that
        Canopy always learns the outcome regardless of whether an exception is
        propagating.  It never raises.
        """
        if attempt.attempt_id is None:
            return

        results: list[ReportResult] = []

        for entity in attempt.all_entities_flat():
            if entity.status not in _REPORTABLE_STATUSES:
                continue

            contract_status = _STATUS_MAP[entity.status]

            results.append(
                ReportResult(
                    entity_type=entity.entity_type,
                    entity_id=entity.entity_id,
                    status=contract_status,
                    accession=entity.ena_accession,
                    secondary_accession=entity.biosample_accession,
                    message=(
                        "Submission accepted"
                        if entity.status == EntitySubmissionStatus.SUCCEEDED
                        else entity.error_message
                    ),
                    errors=(
                        [entity.error_message]
                        if entity.error_message
                        and entity.status == EntitySubmissionStatus.FAILED
                        else []
                    ),
                )
            )

        if not results:
            logger.debug("No reportable entities for attempt %s — skipping report", attempt.attempt_id)
            return

        logger.info(
            "Reporting %d outcome(s) to Canopy for attempt %s",
            len(results),
            attempt.attempt_id,
        )
        batch = ReportBatchPayload(tax_id=attempt.tax_id, results=results)
        self._canopy.report_outcome(attempt_id=attempt.attempt_id, payload=batch)
