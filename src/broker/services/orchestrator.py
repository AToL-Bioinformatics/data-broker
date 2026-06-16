"""Submission orchestrator.

Coordinates the full attempt lifecycle:
  - Claims data from Canopy
  - Builds AttemptState
  - Iterates entities in dependency order
  - Delegates each entity to SubmissionService

Two entry points:
  run_bulk:     submit all entities for a tax_id, optionally filtered by type
  run_targeted: submit a single entity by type + id

Dependency order for bulk:  projects → samples → experiments → runs

Prerequisite propagation in bulk mode:
  After a project succeeds, its PRJEB accession is stored on entity.ena_accession
  in AttemptState. The next entity's PrerequisiteValidator finds it there via
  state fallback (allow_state_fallback=True).

In targeted mode (and --only partial mode):
  allow_state_fallback=False — prerequisites must come from CLI args or Canopy payload.
  Missing prerequisites raise PrerequisiteMissingError immediately.
"""

from __future__ import annotations

import logging

from broker.clients.canopy import CanopyClient
from broker.enums import ENTITY_DEPENDENCY_ORDER, AttemptMode, AttemptStatus, EntityType, SubmissionMode
from broker.errors import PrerequisiteMissingError
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.models.canopy import ClaimResponse
from broker.services.report_service import ReportService
from broker.services.submission_service import SubmissionService
from broker.storage.state_store import StateStore

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        canopy_client: CanopyClient,
        submission_service: SubmissionService,
        state_store: StateStore,
        report_service: ReportService,
    ) -> None:
        self._canopy = canopy_client
        self._submission_service = submission_service
        self._state_store = state_store
        self._report_service = report_service

    # ------------------------------------------------------------------
    # Bulk submission
    # ------------------------------------------------------------------

    def run_bulk(
        self,
        tax_id: str,
        only: EntityType | None,
        submission_mode: SubmissionMode,
        cli_overrides: dict[str, str] | None = None,
        hold_until_date: str | None = None,
    ) -> AttemptState:
        """Submit all ready entities for a tax_id, in dependency order.

        Args:
            tax_id:          Taxonomy ID to claim from Canopy.
            only:            If set, submit only this entity type.
            submission_mode: NORMAL, DRY_RUN, or VALIDATE_ONLY.
            cli_overrides:   Accession values from CLI flags.
            hold_until_date: ISO 8601 date for ENA HOLD action.
        """
        if cli_overrides is None:
            cli_overrides = {}

        # In partial mode, only claim the requested type
        entity_types = [only] if only else None
        # In partial mode, prerequisites must not be auto-resolved from state
        allow_state_fallback = only is None

        logger.info("Claiming Canopy data for tax_id=%s, only=%s", tax_id, only)
        claim = self._canopy.claim_by_tax_id(tax_id, entity_types=entity_types)
        if claim.attempt_id is None:
            logger.info("No claimable entities for tax_id=%s — nothing to submit", tax_id)
            # Return a completed empty attempt; no state file is created
            return AttemptState(
                attempt_id=self._state_store.generate_attempt_id(),
                tax_id=tax_id,
                mode=AttemptMode.BULK,
                submission_mode=submission_mode,
                status=AttemptStatus.COMPLETED,
                hold_until_date=hold_until_date,
            )
        if only is not None:
            filtered_entities = [entity for entity in claim.entities if entity.type == only]
            if len(filtered_entities) != len(claim.entities):
                logger.info(
                    "Locally filtering Canopy claim to only=%s (%d -> %d entities)",
                    only,
                    len(claim.entities),
                    len(filtered_entities),
                )
            claim = claim.model_copy(update={"entities": filtered_entities})
        attempt = self._build_attempt_state(
            claim=claim,
            tax_id=tax_id,
            mode=AttemptMode.BULK,
            submission_mode=submission_mode,
            hold_until_date=hold_until_date,
        )
        self._state_store.save(attempt)
        logger.info("Attempt %s created with %d entities", attempt.attempt_id, len(attempt.all_entities_flat()))

        try:
            self._run_entities(attempt, cli_overrides=cli_overrides, allow_state_fallback=allow_state_fallback)
        finally:
            self._report_and_finalise(attempt)

        attempt.status = attempt.compute_status()
        self._state_store.save(attempt)
        return attempt

    # ------------------------------------------------------------------
    # Targeted submission
    # ------------------------------------------------------------------

    def run_targeted(
        self,
        entity_type: EntityType,
        entity_id: str,
        cli_overrides: dict[str, str] | None = None,
        submission_mode: SubmissionMode = SubmissionMode.NORMAL,
        hold_until_date: str | None = None,
    ) -> AttemptState:
        """Submit a single entity by type + id.

        Prerequisites must come from cli_overrides or the Canopy payload.
        State fallback is disabled: missing prerequisites raise immediately.
        """
        if cli_overrides is None:
            cli_overrides = {}

        logger.info("Claiming Canopy data for %s %s", entity_type, entity_id)
        claim = self._canopy.claim_entity(entity_type=entity_type, entity_id=entity_id)
        attempt = self._build_attempt_state(
            claim=claim,
            tax_id=None,
            mode=AttemptMode.TARGETED,
            submission_mode=submission_mode,
            hold_until_date=hold_until_date,
        )
        self._state_store.save(attempt)

        try:
            self._run_entities(attempt, cli_overrides=cli_overrides, allow_state_fallback=False)
        finally:
            self._report_and_finalise(attempt)

        attempt.status = attempt.compute_status()
        self._state_store.save(attempt)
        return attempt

    # ------------------------------------------------------------------
    # Batch submission
    # ------------------------------------------------------------------

    def run_batch(
        self,
        project_ids: list[str] | None = None,
        sample_ids: list[str] | None = None,
        experiment_ids: list[str] | None = None,
        run_ids: list[str] | None = None,
        submission_mode: SubmissionMode = SubmissionMode.NORMAL,
        cli_overrides: dict[str, str] | None = None,
        hold_until_date: str | None = None,
    ) -> AttemptState:
        """Submit specific entities by ID via POST /claims/batch.

        Prerequisites must come from cli_overrides or the Canopy payload.
        State fallback is disabled — same behaviour as targeted mode.
        """
        if cli_overrides is None:
            cli_overrides = {}

        logger.info(
            "Claiming batch: projects=%s samples=%s experiments=%s runs=%s",
            project_ids, sample_ids, experiment_ids, run_ids,
        )
        claim = self._canopy.claim_batch(
            project_ids=project_ids,
            sample_ids=sample_ids,
            experiment_ids=experiment_ids,
            run_ids=run_ids,
        )

        if claim.attempt_id is None:
            logger.info("Batch claim returned no entities — nothing to submit")
            return AttemptState(
                attempt_id=self._state_store.generate_attempt_id(),
                tax_id=claim.tax_id,
                mode=AttemptMode.TARGETED,
                submission_mode=submission_mode,
                status=AttemptStatus.COMPLETED,
                hold_until_date=hold_until_date,
            )

        attempt = self._build_attempt_state(
            claim=claim,
            tax_id=None,
            mode=AttemptMode.TARGETED,
            submission_mode=submission_mode,
            hold_until_date=hold_until_date,
        )
        self._state_store.save(attempt)
        logger.info(
            "Batch attempt %s created with %d entities",
            attempt.attempt_id, len(attempt.all_entities_flat()),
        )

        # No state fallback — prerequisites must be explicit
        try:
            self._run_entities(attempt, cli_overrides=cli_overrides, allow_state_fallback=False)
        finally:
            self._report_and_finalise(attempt)

        attempt.status = attempt.compute_status()
        self._state_store.save(attempt)
        return attempt

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_attempt_state(
        self,
        claim: ClaimResponse,
        tax_id: str | None,
        mode: AttemptMode,
        submission_mode: SubmissionMode,
        hold_until_date: str | None,
    ) -> AttemptState:
        """Translate a ClaimResponse into an AttemptState.

        Entities arrive as a flat list, each carrying its own `type` discriminator.
        Prerequisite accessions come from the entity's `prerequisites` block:
          - resolved values (project_accession etc.) are used directly
          - required_* fields tell us what this entity depends on
        """
        attempt = AttemptState(
            attempt_id=claim.attempt_id,
            tax_id=tax_id or claim.tax_id,
            mode=mode,
            submission_mode=submission_mode,
            status=AttemptStatus.IN_PROGRESS,
            hold_until_date=hold_until_date,
        )
        for ce in claim.entities:
            p = ce.prerequisites
            # tax_id and scientific_name are returned at entity root level by
            # Canopy, not inside payload.  Merge them in so TransformService
            # can find them when building sample XML.  Payload values win if
            # already present (explicit payload takes precedence over root).
            raw_payload = dict(ce.payload)
            for field, value in (("tax_id", ce.tax_id), ("scientific_name", ce.scientific_name)):
                if value and field not in raw_payload:
                    raw_payload[field] = value

            entity = EntitySubmissionState(
                entity_id=ce.id,
                entity_type=ce.type,
                raw_payload=raw_payload,
                # Prefer required_* fields (what this entity specifically needs).
                # Fall back to the plain resolved field in case required_* is absent.
                project_accession=(p.required_project_accession or p.project_accession) if p else None,
                sample_accession=(p.required_sample_accession or p.sample_accession) if p else None,
                experiment_accession=(p.required_experiment_accession or p.experiment_accession) if p else None,
            )
            attempt.entities[ce.type].append(entity)
        return attempt

    def _report_and_finalise(self, attempt: AttemptState) -> None:
        """Batch-report all entity outcomes to Canopy and release the lease.

        Always called in a ``finally`` block so it runs on both success and
        failure paths.  Both inner calls are already fire-and-forget (they never
        raise), so this method is safe to call while an exception is propagating.
        """
        self._report_service.report_attempt(attempt)
        self._canopy.finalise_claim(attempt.attempt_id)

    def _run_entities(
        self,
        attempt: AttemptState,
        cli_overrides: dict[str, str],
        allow_state_fallback: bool,
    ) -> None:
        """Iterate entities in dependency order and submit each."""
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
                try:
                    self._submission_service.submit_entity(
                        entity=entity,
                        attempt_state=attempt,
                        cli_overrides=cli_overrides,
                        allow_state_fallback=allow_state_fallback,
                    )
                except PrerequisiteMissingError:
                    # Re-raise immediately — this is a configuration error,
                    # not a transient failure. State is not yet written for this entity.
                    raise
