"""ToLID request and retry service backed by Canopy state.

Flow:
  1. Ask Canopy for one sample by specimen accession, or ask for pending ToLID work items
  2. POST each item to the Sanger ToLID request/create endpoint
  3. Report assigned/pending/failed outcomes back to Canopy
  4. Optionally submit an ENA MODIFY to attach the tolid sample attribute

Canopy is the durable source of truth for ToLID state. The broker remains
stateless across CLI invocations for this workflow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from broker.clients.canopy import CanopyClient
from broker.clients.ena import ENAClient
from broker.clients.tolid import ToLIDClient, ToLIDLookupResult
from broker.enums import EntityType, ToLIDStatus
from broker.errors import BrokerError
from broker.models.canopy import (
    CanopyEntityPayload,
    ToLIDReportPayload,
    ToLIDWorkItem,
)
from broker.services.transform_service import TransformService

logger = logging.getLogger(__name__)


@dataclass
class ToLIDResult:
    sample_id: str
    specimen_id: str
    status: ToLIDStatus
    tolid: str | None = None
    request_id: str | None = None
    ena_updated: bool = False
    skipped: bool = False
    note: str | None = None
    error: str | None = None


class ToLIDService:
    def __init__(
        self,
        canopy_client: CanopyClient,
        tolid_client: ToLIDClient,
        ena_client: ENAClient,
        transform_service: TransformService,
        report_to_canopy: bool = True,
    ) -> None:
        self._canopy = canopy_client
        self._tolid = tolid_client
        self._ena = ena_client
        self._transform = transform_service
        self._report_to_canopy = report_to_canopy

    def process_sample_accession(
        self,
        specimen_id: str,
        update_ena: bool = True,
    ) -> ToLIDResult:
        item = self._canopy.get_tolid_by_specimen_accession(specimen_id)
        logger.info(
            "Canopy returned ToLID work item for specimen accession %s (sample_id=%s, status=%s)",
            specimen_id,
            item.sample_id,
            item.status,
        )
        return self._process_one(item, update_ena=update_ena)

    def process_pending(
        self,
        tax_id: str | None = None,
        sample_id: str | None = None,
        limit: int | None = None,
        update_ena: bool = True,
        now: datetime | None = None,
    ) -> list[ToLIDResult]:
        items = self._canopy.list_pending_tolids(
            tax_id=tax_id,
            sample_id=sample_id,
            limit=limit,
        )
        logger.info(
            "Canopy returned %d pending ToLID row(s) (tax_id=%s, sample_id=%s, limit=%s)",
            len(items),
            tax_id,
            sample_id,
            limit,
        )
        poll_now = now or datetime.now(timezone.utc)
        return [self._process_one(item, update_ena=update_ena, now=poll_now) for item in items]

    def _process_one(
        self,
        item: ToLIDWorkItem,
        update_ena: bool,
        now: datetime | None = None,
    ) -> ToLIDResult:
        requested_at = now or datetime.now(timezone.utc)
        lookup = self._request_tolid(item)

        if lookup.status == "error":
            error = lookup.error or "ToLID service returned an error"
            logger.warning("ToLID request failed for sample %s: %s", item.sample_id, error)
            return ToLIDResult(
                sample_id=item.sample_id,
                specimen_id=item.specimen_id,
                status=item.status,
                request_id=lookup.request_id or item.request_id,
                error=error,
            )

        if lookup.status == "pending":
            request_id = lookup.request_id or item.request_id
            self._report_tolid(
                item.sample_id,
                ToLIDReportPayload(
                    status=ToLIDStatus.PENDING,
                    request_id=request_id,
                    last_requested_at=requested_at,
                ),
            )
            logger.info(
                "ToLID pending for sample %s (request_id=%s)",
                item.sample_id,
                request_id,
            )
            return ToLIDResult(
                sample_id=item.sample_id,
                specimen_id=item.specimen_id,
                status=ToLIDStatus.PENDING,
                request_id=request_id,
                note=lookup.pending_status or "Pending",
            )

        tolid = lookup.tolid
        if not tolid:
            error = "ToLID service returned no ID"
            logger.warning("ToLID request failed for sample %s: %s", item.sample_id, error)
            return ToLIDResult(
                sample_id=item.sample_id,
                specimen_id=item.specimen_id,
                status=item.status,
                request_id=item.request_id,
                error=error,
            )

        ena_updated = False
        ena_error: str | None = None
        if update_ena:
            try:
                ena_updated = self._update_ena(item, tolid)
                if not ena_updated:
                    ena_error = "ENA MODIFY failed"
            except BrokerError as exc:
                ena_error = str(exc)
                logger.warning(
                    "Unable to update ENA for sample %s after ToLID assignment: %s",
                    item.sample_id,
                    exc,
                )

        self._report_tolid(
            item.sample_id,
            ToLIDReportPayload(
                status=ToLIDStatus.ASSIGNED,
                tolid=tolid,
                request_id=lookup.request_id or item.request_id,
                last_requested_at=requested_at,
            ),
        )
        logger.info("ToLID assigned: sample %s → %s", item.sample_id, tolid)
        return ToLIDResult(
            sample_id=item.sample_id,
            specimen_id=item.specimen_id,
            status=ToLIDStatus.ASSIGNED,
            tolid=tolid,
            request_id=lookup.request_id or item.request_id,
            ena_updated=ena_updated,
            note=ena_error,
        )

    def _report_tolid(self, sample_id: str, payload: ToLIDReportPayload) -> None:
        # Temporary policy: dev ToLID responses are not written back to Canopy
        # because they may contain dummy or transient identifiers. To allow
        # reporting in dev again later, pass report_to_canopy=True when wiring
        # this service in the CLI.
        if not self._report_to_canopy:
            logger.info(
                "Skipping Canopy ToLID report for sample %s because broker is running in dev mode",
                sample_id,
            )
            return
        self._canopy.report_tolid(sample_id, payload)

    def _request_tolid(self, item: ToLIDWorkItem) -> ToLIDLookupResult:
        logger.info(
            "Requesting ToLID for sample %s (specimen_id=%s, tax_id=%s)",
            item.sample_id,
            item.specimen_id,
            item.tax_id,
        )
        return self._tolid.request_tolid(
            specimen_id=item.specimen_id,
            taxonomy_id=item.tax_id,
            confirmation_name=item.scientific_name,
        )

    def _update_ena(self, item: ToLIDWorkItem, tolid: str) -> bool:
        payload = self._load_sample_payload(item)
        sample_xml = self._transform.to_sample_modify_xml(
            payload=payload,
            ena_accession=item.specimen_id,
            tolid=tolid,
        )
        submission_xml = self._transform.build_submission_xml(action="MODIFY")

        logger.info(
            "Submitting MODIFY to ENA for sample %s (tolid=%s)",
            item.sample_id,
            tolid,
        )
        logger.debug("MODIFY submission XML:\n%s", submission_xml)
        logger.debug("MODIFY sample XML:\n%s", sample_xml)

        result = self._ena.submit_sample(item.sample_id, submission_xml, sample_xml)
        if result.success:
            logger.info("ENA MODIFY succeeded for sample %s", item.sample_id)
            return True

        logger.warning(
            "ENA MODIFY failed for sample %s — %s",
            item.sample_id,
            result.error_message or "no detail",
        )
        return False

    def _load_sample_payload(self, item: ToLIDWorkItem) -> CanopyEntityPayload:
        data = item.sample_payload
        if data is None:
            full_item = self._canopy.get_tolid(item.sample_id)
            data = full_item.sample_payload

        if data is None:
            raise BrokerError(
                f"Canopy ToLID row for sample '{item.sample_id}' does not include sample payload needed for ENA MODIFY"
            )

        normalised_data = dict(data)
        if "tax_id" not in normalised_data and "taxon_id" not in normalised_data:
            normalised_data["tax_id"] = item.tax_id
        if "scientific_name" not in normalised_data and item.scientific_name:
            normalised_data["scientific_name"] = item.scientific_name
        if "title" not in normalised_data or not normalised_data["title"]:
            normalised_data["title"] = self._fallback_sample_title(item)
            logger.info(
                "Synthesised sample title for ENA MODIFY on sample %s: %s",
                item.sample_id,
                normalised_data["title"],
            )

        return CanopyEntityPayload(
            entity_id=item.sample_id,
            entity_type=EntityType.SAMPLE,
            data=normalised_data,
        )

    @staticmethod
    def _fallback_sample_title(item: ToLIDWorkItem) -> str:
        scientific_name = item.scientific_name or "unknown organism"
        label = "Specimen" if item.kind == "specimen" else "Sample"
        return f"{label} {item.sample_payload.specimen_id} for {scientific_name}"
