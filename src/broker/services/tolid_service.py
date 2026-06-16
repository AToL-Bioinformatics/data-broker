"""ToLID request and ENA update service.

Separate from the submission lifecycle — invoked explicitly via
``broker tolid request`` after samples have been submitted to ENA.

Only specimen-level samples are processed (``kind == "specimen"`` in the
sample payload).  Samples that already have a tolid stored are skipped.

Flow per eligible sample:
  1. POST to ToLID service, or poll an existing pending request
  2. Store tolid or pending request state + save
  3. (optional) Submit MODIFY to ENA to record the tolid attribute
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from broker.clients.ena import ENAClient
from broker.clients.tolid import ToLIDClient, ToLIDLookupResult
from broker.enums import EntitySubmissionStatus, EntityType
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.models.canopy import CanopyEntityPayload
from broker.services.transform_service import TransformService
from broker.storage.state_store import StateStore

logger = logging.getLogger(__name__)

# Only samples with this value in raw_payload["kind"] are eligible
_SPECIMEN_KIND = "specimen"


@dataclass
class ToLIDResult:
    """Outcome of a single ToLID request + optional ENA update."""

    entity_id: str
    ena_accession: str
    tolid: str | None = None
    pending: bool = False
    pending_request_id: str | None = None
    pending_status: str | None = None
    ena_updated: bool = False
    skipped: bool = False          # True if already had a tolid or not a specimen
    skip_reason: str | None = None
    error: str | None = None


class ToLIDService:
    def __init__(
        self,
        tolid_client: ToLIDClient,
        ena_client: ENAClient,
        transform_service: TransformService,
        state_store: StateStore,
    ) -> None:
        self._tolid = tolid_client
        self._ena = ena_client
        self._transform = transform_service
        self._state_store = state_store

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process_attempt(
        self,
        attempt: AttemptState,
        update_ena: bool = True,
    ) -> list[ToLIDResult]:
        """Request ToLIDs for all eligible specimen samples in an attempt.

        Args:
            attempt:    Loaded attempt state.  Modified in place when tolids
                        are assigned; saved to disk after each successful request.
            update_ena: When True (default), submit a MODIFY to ENA after each
                        successful ToLID request to record the tolid attribute.
                        Pass False to fetch ToLIDs without touching ENA.

        Returns:
            One ToLIDResult per sample entity, including skipped ones.
        """
        results: list[ToLIDResult] = []

        for entity in attempt.entities.get(EntityType.SAMPLE, []):
            result = self._process_one(entity, attempt, update_ena)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_one(
        self,
        entity: EntitySubmissionState,
        attempt: AttemptState,
        update_ena: bool,
    ) -> ToLIDResult:
        """Process a single sample entity."""

        # Guard: must have a successful ENA submission with an accession
        if entity.status != EntitySubmissionStatus.SUCCEEDED or not entity.ena_accession:
            return ToLIDResult(
                entity_id=entity.entity_id,
                ena_accession=entity.ena_accession or "",
                skipped=True,
                skip_reason=f"status is {entity.status}, not SUCCEEDED",
            )

        # Guard: specimen-level only
        if entity.raw_payload.get("kind") != _SPECIMEN_KIND:
            return ToLIDResult(
                entity_id=entity.entity_id,
                ena_accession=entity.ena_accession,
                skipped=True,
                skip_reason=f"kind={entity.raw_payload.get('kind')!r}, not 'specimen'",
            )

        # Guard: already processed
        if entity.tolid:
            return ToLIDResult(
                entity_id=entity.entity_id,
                ena_accession=entity.ena_accession,
                tolid=entity.tolid,
                skipped=True,
                skip_reason="tolid already assigned",
            )

        lookup = self._lookup_tolid(entity)

        if lookup.status == "error":
            msg = lookup.error or "ToLID service returned an error"
            logger.warning("Sample %s: %s", entity.entity_id, msg)
            return ToLIDResult(
                entity_id=entity.entity_id,
                ena_accession=entity.ena_accession,
                error=msg,
            )

        if lookup.status == "pending":
            entity.tolid_request_id = lookup.request_id or entity.tolid_request_id
            entity.tolid_status = lookup.pending_status or "Pending"
            self._state_store.save(attempt)
            logger.info(
                "ToLID pending for sample %s (request_id=%s, status=%s)",
                entity.entity_id,
                entity.tolid_request_id,
                entity.tolid_status,
            )
            return ToLIDResult(
                entity_id=entity.entity_id,
                ena_accession=entity.ena_accession,
                pending=True,
                pending_request_id=entity.tolid_request_id,
                pending_status=entity.tolid_status,
            )

        tolid = lookup.tolid
        if not tolid:
            msg = "ToLID service returned no ID"
            logger.warning("Sample %s: %s", entity.entity_id, msg)
            return ToLIDResult(
                entity_id=entity.entity_id,
                ena_accession=entity.ena_accession,
                error=msg,
            )

        # Persist tolid before ENA update so a crash doesn't lose it
        entity.tolid = tolid
        entity.tolid_request_id = None
        entity.tolid_status = None
        logger.info("ToLID assigned: sample %s → %s", entity.entity_id, tolid)
        self._state_store.save(attempt)

        # --- Optionally update ENA via MODIFY ---
        ena_updated = False
        if update_ena:
            ena_updated = self._update_ena(entity, tolid)

        return ToLIDResult(
            entity_id=entity.entity_id,
            ena_accession=entity.ena_accession,
            tolid=tolid,
            ena_updated=ena_updated,
        )

    def _lookup_tolid(self, entity: EntitySubmissionState) -> ToLIDLookupResult:
        """Create or poll a ToLID request depending on saved sample state."""
        if entity.tolid_request_id:
            logger.info(
                "Polling pending ToLID request for sample %s (request_id=%s)",
                entity.entity_id,
                entity.tolid_request_id,
            )
            return self._tolid.poll_tolid_request(entity.tolid_request_id)

        tax_id = entity.raw_payload.get("tax_id", "")
        scientific_name = entity.raw_payload.get("scientific_name")

        logger.info(
            "Requesting ToLID for sample %s (specimen_id=%s, tax_id=%s)",
            entity.entity_id,
            entity.ena_accession,
            tax_id,
        )
        return self._tolid.request_tolid(
            specimen_id=entity.ena_accession or "",
            taxonomy_id=tax_id,
            confirmation_name=scientific_name,
        )

    def _update_ena(self, entity: EntitySubmissionState, tolid: str) -> bool:
        """Submit a MODIFY to ENA adding the tolid sample attribute.

        Returns True if ENA accepted the update, False otherwise.
        Never raises — failures are logged as warnings.
        """
        payload = CanopyEntityPayload(
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            data=entity.raw_payload,
        )
        sample_xml = self._transform.to_sample_modify_xml(
            payload=payload,
            ena_accession=entity.ena_accession,
            tolid=tolid,
        )
        submission_xml = self._transform.build_submission_xml(action="MODIFY")

        logger.info(
            "Submitting MODIFY to ENA for sample %s (tolid=%s)",
            entity.entity_id,
            tolid,
        )
        logger.debug("MODIFY submission XML:\n%s", submission_xml)
        logger.debug("MODIFY sample XML:\n%s", sample_xml)

        result = self._ena.submit_sample(entity.entity_id, submission_xml, sample_xml)

        if result.success:
            logger.info("ENA MODIFY succeeded for sample %s", entity.entity_id)
            return True
        else:
            logger.warning(
                "ENA MODIFY failed for sample %s — %s",
                entity.entity_id,
                result.error_message or "no detail",
            )
            return False
