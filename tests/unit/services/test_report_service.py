from __future__ import annotations

from unittest.mock import MagicMock

from broker.enums import AttemptMode, EntityType
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.services.report_service import ReportService


def make_attempt() -> AttemptState:
    attempt = AttemptState(
        attempt_id="attempt-1",
        tax_id="9606",
        mode=AttemptMode.BULK,
    )
    entity = EntitySubmissionState(
        entity_id="p1",
        entity_type=EntityType.PROJECT,
        raw_payload={"title": "T", "description": "D"},
    )
    entity.mark_succeeded("PRJEB1")
    attempt.entities[EntityType.PROJECT].append(entity)
    return attempt


def test_report_attempt_posts_outcomes_when_enabled():
    canopy = MagicMock()
    service = ReportService(canopy, enabled=True)

    service.report_attempt(make_attempt())

    canopy.report_outcome.assert_called_once()


def test_report_attempt_skips_canopy_when_disabled():
    canopy = MagicMock()
    service = ReportService(canopy, enabled=False)

    service.report_attempt(make_attempt())

    canopy.report_outcome.assert_not_called()
