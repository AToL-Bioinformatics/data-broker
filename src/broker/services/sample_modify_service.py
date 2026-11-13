"""Backfill attributes onto samples already registered at ENA, via MODIFY.

Flow:
  1. Ask Canopy for accepted samples whose ENA record is missing attributes
     Canopy can now supply (currently: "sample derived from")
  2. Build the full sample XML with the existing accession and submit MODIFY
  3. Report the fields ENA accepted back to Canopy, which drops the sample
     from the pending list

MODIFY replaces the whole ENA record, so Canopy sends the complete payload,
not just the new fields.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from broker.clients.canopy import CanopyClient
from broker.clients.ena import ENAClient
from broker.enums import EntityType
from broker.models.canopy import CanopyEntityPayload, SampleModifyItem
from broker.services.transform_service import TransformService

logger = logging.getLogger(__name__)

# Fields this backfill exists to add; only these are reported back to Canopy.
MODIFY_FIELDS = ("sample derived from",)


@dataclass
class SampleModifyResult:
    sample_id: str
    accession: str
    updates: dict[str, str]
    ena_updated: bool = False
    reported: bool = False
    sample_xml: str | None = None  # set on dry runs
    error: str | None = None


class SampleModifyService:
    def __init__(
        self,
        canopy_client: CanopyClient,
        ena_client: ENAClient,
        transform_service: TransformService,
        report_to_canopy: bool = True,
    ) -> None:
        self._canopy = canopy_client
        self._ena = ena_client
        self._transform = transform_service
        self._report_to_canopy = report_to_canopy

    def process_pending(
        self,
        tax_id: str | None = None,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> list[SampleModifyResult]:
        items = self._canopy.list_sample_modify_pending(tax_id=tax_id, limit=limit)
        logger.info(
            "Canopy returned %d sample(s) needing ENA MODIFY (tax_id=%s, limit=%s)",
            len(items),
            tax_id,
            limit,
        )
        return [self._process_one(item, dry_run=dry_run) for item in items]

    def _process_one(self, item: SampleModifyItem, dry_run: bool) -> SampleModifyResult:
        entity = item.entity
        updates = {f: str(entity.payload[f]) for f in MODIFY_FIELDS if entity.payload.get(f)}
        result = SampleModifyResult(sample_id=entity.id, accession=item.accession, updates=updates)

        try:
            sample_xml = self._transform.to_sample_modify_xml(
                payload=self._to_payload(item), ena_accession=item.accession
            )
        except Exception as exc:  # missing required fields etc. — skip this sample, keep going
            result.error = f"Could not build MODIFY XML: {exc}"
            logger.warning("Sample %s: %s", entity.id, result.error)
            return result

        if dry_run:
            result.sample_xml = sample_xml
            return result

        submission_xml = self._transform.build_submission_xml(action="MODIFY")
        logger.info("Submitting MODIFY to ENA for sample %s (%s)", entity.id, item.accession)
        ena = self._ena.submit_sample(entity.id, submission_xml, sample_xml)
        if not ena.success:
            result.error = f"ENA MODIFY failed: {ena.error_message or 'no detail'}"
            logger.warning("Sample %s: %s", entity.id, result.error)
            return result
        result.ena_updated = True

        if not self._report_to_canopy:
            logger.info("Not reporting MODIFY of sample %s to Canopy (dev mode)", entity.id)
            return result
        try:
            self._canopy.report_sample_modified(entity.id, updates)
            result.reported = True
        except Exception as exc:  # ENA already updated; re-running is harmless (same MODIFY)
            result.error = f"ENA updated but Canopy report failed: {exc}"
            logger.warning("Sample %s: %s", entity.id, result.error)
        return result

    @staticmethod
    def _to_payload(item: SampleModifyItem) -> CanopyEntityPayload:
        # Same merge the orchestrator does for claims: tax_id/scientific_name live on the entity.
        entity = item.entity
        data = dict(entity.payload)
        for field, value in (
            ("tax_id", entity.tax_id),
            ("scientific_name", entity.scientific_name),
        ):
            if value and field not in data:
                data[field] = value
        return CanopyEntityPayload(entity_id=entity.id, entity_type=EntityType.SAMPLE, data=data)
