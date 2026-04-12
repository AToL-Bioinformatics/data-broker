"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from broker.enums import AttemptMode, EntityType
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.storage.receipt_store import ReceiptStore
from broker.storage.state_store import StateStore


@pytest.fixture
def tmp_state_store(tmp_path):
    return StateStore(str(tmp_path / "state"))


@pytest.fixture
def tmp_receipt_store(tmp_path):
    return ReceiptStore(str(tmp_path / "receipts"))


@pytest.fixture
def sample_attempt():
    """A minimal AttemptState with one project entity."""
    state = AttemptState(
        attempt_id="test-attempt-1",
        tax_id="9606",
        mode=AttemptMode.BULK,
    )
    entity = EntitySubmissionState(
        entity_id="proj-1",
        entity_type=EntityType.PROJECT,
        raw_payload={"title": "Test Project", "description": "A test project"},
    )
    state.entities[EntityType.PROJECT].append(entity)
    return state
