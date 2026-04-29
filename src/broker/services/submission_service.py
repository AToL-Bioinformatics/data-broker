"""Single-entity submission lifecycle.

Checkpoint ordering guarantee (crash-safe):
  1. Validate prerequisites → raise PrerequisiteMissingError if missing
  2. Mark entity SUBMITTED → save state  ← crash here = re-POST on resume (safe: ENA aliases are idempotent)
  3. Build ENA XML via TransformService
  4. POST to ENAClient  ← never raises; returns ENASubmissionResult
  5. Save raw receipt to ReceiptStore
  6. Mark SUCCEEDED or FAILED → save state  ← local outcome is authoritative

Reporting to Canopy is NOT done here.  It is the orchestrator's responsibility
to batch-report all entity outcomes (and call /finalise) in a ``finally`` block
after the full run completes, so that Canopy always receives a complete picture
regardless of whether any entity failed or the process was interrupted.

Dry-run mode skips steps 3-6 and marks the entity SKIPPED after step 2.
"""

from __future__ import annotations

import logging

from broker.clients.ena import ENAClient
from broker.clients.tolid import ToLIDClient
from broker.enums import EntitySubmissionStatus, EntityType, SubmissionMode
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.services.prerequisite_validation import PrerequisiteValidator
from broker.services.receipt_parser import ReceiptParser
from broker.services.transform_service import TransformService
from broker.storage.receipt_store import ReceiptStore
from broker.storage.state_store import StateStore

logger = logging.getLogger(__name__)

# Entity types for which a HOLD date applies
_HOLD_APPLIES_TO = {EntityType.PROJECT, EntityType.SAMPLE}


class SubmissionService:
    def __init__(
        self,
        ena_client: ENAClient,
        transform_service: TransformService,
        prerequisite_validator: PrerequisiteValidator,
        receipt_parser: ReceiptParser,
        state_store: StateStore,
        receipt_store: ReceiptStore,
        tolid_client: ToLIDClient | None = None,
    ) -> None:
        self._ena = ena_client
        self._transform = transform_service
        self._prereq_validator = prerequisite_validator
        self._receipt_parser = receipt_parser
        self._state_store = state_store
        self._receipt_store = receipt_store
        self._tolid_client = tolid_client

    def submit_entity(
        self,
        entity: EntitySubmissionState,
        attempt_state: AttemptState,
        cli_overrides: dict[str, str] | None = None,
        allow_state_fallback: bool = True,
    ) -> EntitySubmissionState:
        """Execute the full submission lifecycle for one entity.

        Args:
            entity:              The entity to submit. Modified in place.
            attempt_state:       Full attempt state. Modified and saved after each step.
            cli_overrides:       Accession values from CLI flags (highest priority).
            allow_state_fallback: Whether to fall back to state-store for prerequisite
                                  resolution. False in targeted/partial mode.

        Returns:
            The updated entity state.
        """
        if cli_overrides is None:
            cli_overrides = {}

        submission_mode = attempt_state.submission_mode

        # Step 1: Resolve prerequisites
        # Raises PrerequisiteMissingError immediately — nothing is persisted yet.
        self._prereq_validator.resolve_prerequisites(
            entity=entity,
            attempt_state=attempt_state,
            cli_overrides=cli_overrides,
            allow_state_fallback=allow_state_fallback,
        )

        # Step 2: Checkpoint — mark SUBMITTED and save.
        # If the process crashes after this point and before step 6,
        # resume will see SUBMITTED (non-terminal) and re-POST.
        # ENA Webin alias idempotency ensures the same alias returns the same accession.
        entity.mark_submitted()
        self._state_store.save(attempt_state)

        if submission_mode == SubmissionMode.DRY_RUN:
            logger.info("[DRY RUN] Would submit %s %s", entity.entity_type, entity.entity_id)
            entity.mark_skipped()
            self._state_store.save(attempt_state)
            return entity

        # Step 3: Build ENA XML
        entity_xml = self._build_entity_xml(entity, attempt_state)

        # HOLD applies to projects and samples only
        hold_until_date = (
            attempt_state.hold_until_date
            if entity.entity_type in _HOLD_APPLIES_TO
            else None
        )
        submission_xml = self._transform.build_submission_xml(
            hold_until_date=hold_until_date
        )

        # Step 4: POST to ENA — never raises
        logger.info("Submitting %s %s to ENA", entity.entity_type, entity.entity_id)
        result = self._dispatch_to_ena(entity, submission_xml, entity_xml)

        # Step 5: Persist raw receipt verbatim
        receipt_path = self._receipt_store.save(
            attempt_id=attempt_state.attempt_id,
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
            raw_receipt=result.raw_receipt,
        )

        # Step 6: Update entity state based on result and save
        if result.success and result.accessions:
            entity.mark_succeeded(
                ena_accession=result.accessions.primary_accession,
                biosample_accession=result.accessions.biosample_accession,
            )
            logger.info(
                "Succeeded: %s %s → %s",
                entity.entity_type,
                entity.entity_id,
                result.accessions.primary_accession,
            )
        else:
            error = result.error_message or "ENA submission failed (no detail)"
            entity.mark_failed(error)
            logger.warning(
                "Failed: %s %s — %s",
                entity.entity_type,
                entity.entity_id,
                error,
            )

        self._state_store.save(attempt_state)

        # Step 7 (samples only): Request a Tree of Life ID using the ENA accession.
        # Fire-and-forget — a ToLID failure never fails the submission.
        if (
            entity.status == EntitySubmissionStatus.SUCCEEDED
            and entity.entity_type == EntityType.SAMPLE
            and self._tolid_client is not None
            and entity.ena_accession
        ):
            self._request_tolid(entity, attempt_state)

        return entity

    def _request_tolid(
        self,
        entity: EntitySubmissionState,
        attempt_state: AttemptState,
    ) -> None:
        """Request a ToLID for a successfully submitted sample.

        Uses the ENA sample accession as the specimen_id.  Stores the result
        on the entity and saves state.  Failures are logged and silently ignored.
        """
        tax_id = entity.raw_payload.get("tax_id", "")
        scientific_name = entity.raw_payload.get("scientific_name")

        logger.info(
            "Requesting ToLID for sample %s (specimen_id=%s)",
            entity.entity_id,
            entity.ena_accession,
        )
        tolid = self._tolid_client.request_tolid(
            specimen_id=entity.ena_accession,
            taxonomy_id=tax_id,
            confirmation_name=scientific_name,
        )
        if tolid:
            entity.tolid = tolid
            logger.info("ToLID assigned for sample %s: %s", entity.entity_id, tolid)
            self._state_store.save(attempt_state)
        else:
            logger.warning(
                "No ToLID returned for sample %s (specimen_id=%s) — continuing without it",
                entity.entity_id,
                entity.ena_accession,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_entity_xml(
        self, entity: EntitySubmissionState, attempt_state: AttemptState
    ) -> str:
        """Route to the correct TransformService method based on entity type."""
        # Re-hydrate a CanopyEntityPayload-like object from stored raw_payload + state
        from broker.models.canopy import CanopyEntityPayload

        payload = CanopyEntityPayload(
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            data=entity.raw_payload,
            project_accession=entity.project_accession,
            sample_accession=entity.sample_accession,
            experiment_accession=entity.experiment_accession,
        )

        if entity.entity_type == EntityType.PROJECT:
            return self._transform.to_project_xml(payload)
        elif entity.entity_type == EntityType.SAMPLE:
            return self._transform.to_sample_xml(payload)
        elif entity.entity_type == EntityType.EXPERIMENT:
            assert entity.project_accession, "project_accession must be resolved before transform"
            assert entity.sample_accession, "sample_accession must be resolved before transform"
            return self._transform.to_experiment_xml(
                payload,
                project_accession=entity.project_accession,
                sample_accession=entity.sample_accession,
            )
        elif entity.entity_type == EntityType.RUN:
            assert entity.experiment_accession, "experiment_accession must be resolved before transform"
            return self._transform.to_run_xml(
                payload,
                experiment_accession=entity.experiment_accession,
            )
        else:
            raise ValueError(f"Unknown entity type: {entity.entity_type}")

    def _dispatch_to_ena(
        self,
        entity: EntitySubmissionState,
        submission_xml: str,
        entity_xml: str,
    ):
        """Route to the correct ENAClient method."""
        dispatch = {
            EntityType.PROJECT: self._ena.submit_project,
            EntityType.SAMPLE: self._ena.submit_sample,
            EntityType.EXPERIMENT: self._ena.submit_experiment,
            EntityType.RUN: self._ena.submit_run,
        }
        method = dispatch[entity.entity_type]
        return method(entity.entity_id, submission_xml, entity_xml)
