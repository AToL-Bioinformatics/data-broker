"""Resume service — continue a previously interrupted submission attempt.

Loads the persisted AttemptState, skips entities already in a terminal state
(SUCCEEDED or SKIPPED), and re-submits the rest in dependency order.

Raw payloads are stored on each EntitySubmissionState, so Canopy is not
re-called on resume. hold_until_date is preserved from the original attempt.

The SUBMITTED state (a crash-safety checkpoint) is treated as non-terminal —
meaning if the process died between writing SUBMITTED and receiving the ENA
response, resume will re-POST. ENA alias idempotency guarantees the same
alias returns the same accession rather than creating a duplicate.

After all entities have been processed (or re-processed), resume batch-reports
ALL entity outcomes to Canopy — including entities that were already terminal
before resume ran — and then finalises the claim.  This guarantees Canopy
always receives a complete picture even when the original run was interrupted
before its own report+finalise could fire.
"""

from __future__ import annotations

import logging

from broker.clients.canopy import CanopyClient
from broker.enums import ENTITY_DEPENDENCY_ORDER, AttemptStatus
from broker.errors import AttemptNotFoundError, PrerequisiteMissingError
from broker.models.attempt import AttemptState
from broker.services.report_service import ReportService
from broker.services.submission_service import SubmissionService
from broker.storage.state_store import StateStore

logger = logging.getLogger(__name__)


class ResumeService:
    def __init__(
        self,
        state_store: StateStore,
        submission_service: SubmissionService,
        canopy_client: CanopyClient,
        report_service: ReportService,
    ) -> None:
        self._state_store = state_store
        self._submission_service = submission_service
        self._canopy = canopy_client
        self._report_service = report_service

    def resume(
        self,
        attempt_id: str,
        cli_overrides: dict[str, str] | None = None,
    ) -> AttemptState:
        """Load persisted attempt and continue from where it left off.

        After all non-terminal entities are processed (or attempted), ALL entity
        outcomes are batch-reported to Canopy and the claim is finalised.  This
        covers entities that were already terminal before this resume call —
        ensuring that a Canopy report is never permanently lost due to a crash.

        Args:
            attempt_id:    The attempt to resume.
            cli_overrides: Optional additional accession overrides (e.g. if
                           the user now has accessions they didn't before).

        Returns:
            The final AttemptState after all non-terminal entities are processed.

        Raises:
            AttemptNotFoundError: if the attempt ID is not in the state store.
        """
        if cli_overrides is None:
            cli_overrides = {}

        attempt = self._state_store.load(attempt_id)
        logger.info(
            "Resuming attempt %s (mode=%s, status=%s)",
            attempt_id,
            attempt.mode,
            attempt.status,
        )

        if attempt.status == AttemptStatus.COMPLETED:
            logger.info("Attempt %s is already COMPLETED — nothing to do.", attempt_id)
            return attempt

        # Determine fallback mode: bulk attempts allow state fallback,
        # targeted attempts do not (same rule as original submission).
        from broker.enums import AttemptMode
        allow_state_fallback = attempt.mode == AttemptMode.BULK

        try:
            for entity_type in ENTITY_DEPENDENCY_ORDER:
                for entity in attempt.entities.get(entity_type, []):
                    if entity.is_terminal:
                        logger.info(
                            "Skipping %s %s — already %s",
                            entity.entity_type,
                            entity.entity_id,
                            entity.status,
                        )
                        continue

                    logger.info(
                        "Resuming %s %s (status=%s)",
                        entity.entity_type,
                        entity.entity_id,
                        entity.status,
                    )
                    try:
                        self._submission_service.submit_entity(
                            entity=entity,
                            attempt_state=attempt,
                            cli_overrides=cli_overrides,
                            allow_state_fallback=allow_state_fallback,
                        )
                    except PrerequisiteMissingError:
                        raise  # propagate: user must supply missing accessions
        finally:
            # Always report all outcomes (including pre-existing terminal
            # entities) and release the lease, even if an exception is live.
            self._report_and_finalise(attempt)

        attempt.status = attempt.compute_status()
        self._state_store.save(attempt)
        return attempt

    def _report_and_finalise(self, attempt: AttemptState) -> None:
        """Batch-report all entity outcomes and release the Canopy lease.

        Mirrors the same helper in Orchestrator.  Both inner calls are
        fire-and-forget so this method never raises.
        """
        self._report_service.report_attempt(attempt)
        self._canopy.finalise_claim(attempt.attempt_id)
