"""Parse ENA XML receipts into ENASubmissionResult.

ENA's drop-box endpoint returns XML in this shape:

  <RECEIPT receiptDate="..." submissionFile="..." success="true|false">
    <PROJECT accession="PRJEB12345" alias="broker-project-p1" status="PRIVATE"/>
    <SUBMISSION accession="ERA..." alias="..."/>
    <MESSAGES>
      <INFO>...</INFO>
      <ERROR>...</ERROR>
    </MESSAGES>
    <ACTIONS>ADD</ACTIONS>
  </RECEIPT>

  For samples:
    <SAMPLE accession="ERS111111" alias="..." status="PRIVATE">
      <EXT_ID accession="SAMEA111111" type="biosample"/>
    </SAMPLE>

The parser extracts:
- success flag
- primary accession (PRJEB, ERS, ERX, ERR)
- biosample_accession (SAMEA — samples only)
- error messages on failure
"""

from __future__ import annotations

import json
import logging

from lxml import etree

from broker.enums import EntityType
from broker.models.ena import ENAAccessions, ENASubmissionResult

logger = logging.getLogger(__name__)

# Map entity type → XML element tag name in the receipt
_RECEIPT_TAG: dict[EntityType, str] = {
    EntityType.PROJECT: "PROJECT",
    EntityType.SAMPLE: "SAMPLE",
    EntityType.EXPERIMENT: "EXPERIMENT",
    EntityType.RUN: "RUN",
}


class ReceiptParser:
    def parse(
        self,
        raw: str,
        entity_id: str,
        entity_type: EntityType,
    ) -> ENASubmissionResult:
        """Auto-detect XML vs JSON and parse into ENASubmissionResult."""
        stripped = raw.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return self._parse_json(raw, entity_id)
        return self._parse_xml(raw, entity_id, entity_type)

    # ------------------------------------------------------------------
    # XML parsing
    # ------------------------------------------------------------------

    def _parse_xml(
        self,
        raw: str,
        entity_id: str,
        entity_type: EntityType,
    ) -> ENASubmissionResult:
        try:
            root = etree.fromstring(raw.encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            logger.warning("ENA receipt is not valid XML: %s", exc)
            return ENASubmissionResult(
                entity_id=entity_id,
                success=False,
                raw_receipt=raw,
                error_message=f"Receipt XML could not be parsed: {exc}",
            )

        success_attr = root.get("success", "false").lower()
        success = success_attr == "true"

        errors = self._collect_errors(root)

        if not success:
            return ENASubmissionResult(
                entity_id=entity_id,
                success=False,
                raw_receipt=raw,
                error_message="; ".join(errors) if errors else "ENA reported failure (no error detail)",
            )

        tag = _RECEIPT_TAG.get(entity_type)
        if not tag:
            return ENASubmissionResult(
                entity_id=entity_id,
                success=False,
                raw_receipt=raw,
                error_message=f"Unknown entity type for receipt parsing: {entity_type}",
            )

        entity_el = root.find(tag)
        if entity_el is None:
            # Some ENA environments use plural tags or different nesting; log and fail clearly.
            return ENASubmissionResult(
                entity_id=entity_id,
                success=False,
                raw_receipt=raw,
                error_message=f"Receipt success=true but no <{tag}> element found",
            )

        primary_accession = entity_el.get("accession", "")
        alias = entity_el.get("alias")
        biosample_accession: str | None = None

        if entity_type == EntityType.SAMPLE:
            ext_id = entity_el.find("EXT_ID[@type='biosample']")
            if ext_id is not None:
                biosample_accession = ext_id.get("accession")

        if not primary_accession:
            return ENASubmissionResult(
                entity_id=entity_id,
                success=False,
                raw_receipt=raw,
                error_message=f"Receipt success=true but <{tag}> has no accession attribute",
            )

        return ENASubmissionResult(
            entity_id=entity_id,
            success=True,
            accessions=ENAAccessions(
                primary_accession=primary_accession,
                biosample_accession=biosample_accession,
                alias=alias,
            ),
            raw_receipt=raw,
        )

    # ------------------------------------------------------------------
    # JSON parsing (for future ENA v2 JSON endpoint support)
    # ------------------------------------------------------------------

    def _parse_json(self, raw: str, entity_id: str) -> ENASubmissionResult:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ENASubmissionResult(
                entity_id=entity_id,
                success=False,
                raw_receipt=raw,
                error_message=f"Receipt JSON could not be parsed: {exc}",
            )

        # ASSUMPTION: ENA v2 JSON receipt shape.
        # Adjust field names when integrating against the real v2 endpoint.
        accession = data.get("accession") or data.get("primaryAccession")
        success = bool(accession)
        biosample = data.get("bioSampleAccession") or data.get("biosampleAccession")

        if not success:
            errors = data.get("errors", []) or data.get("messages", [])
            return ENASubmissionResult(
                entity_id=entity_id,
                success=False,
                raw_receipt=raw,
                error_message="; ".join(str(e) for e in errors) if errors else "ENA JSON receipt missing accession",
            )

        return ENASubmissionResult(
            entity_id=entity_id,
            success=True,
            accessions=ENAAccessions(
                primary_accession=accession,
                biosample_accession=biosample or None,
            ),
            raw_receipt=raw,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_errors(root: etree._Element) -> list[str]:
        messages = root.find("MESSAGES")
        if messages is None:
            return []
        return [el.text or "" for el in messages.findall("ERROR") if el.text]
