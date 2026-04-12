"""Tests for StateStore — JSON persistence and atomic write."""

from __future__ import annotations

import json

import pytest

from broker.enums import AttemptMode, EntityType
from broker.errors import AttemptNotFoundError, StateStoreError
from broker.models.attempt import AttemptState, EntitySubmissionState
from broker.storage.state_store import StateStore


def make_state(attempt_id: str = "test-atm") -> AttemptState:
    state = AttemptState(attempt_id=attempt_id, tax_id="9606", mode=AttemptMode.BULK)
    state.entities[EntityType.PROJECT].append(
        EntitySubmissionState(
            entity_id="p1",
            entity_type=EntityType.PROJECT,
            raw_payload={"title": "T"},
        )
    )
    return state


def test_save_and_load_round_trip(tmp_state_store):
    state = make_state("round-trip")
    tmp_state_store.save(state)
    loaded = tmp_state_store.load("round-trip")
    assert loaded.attempt_id == "round-trip"
    assert loaded.tax_id == "9606"
    assert loaded.entities[EntityType.PROJECT][0].entity_id == "p1"


def test_load_nonexistent_raises(tmp_state_store):
    with pytest.raises(AttemptNotFoundError):
        tmp_state_store.load("does-not-exist")


def test_load_corrupted_json_raises(tmp_path):
    store = StateStore(str(tmp_path / "state"))
    bad_file = (tmp_path / "state" / "bad.json")
    bad_file.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(StateStoreError):
        store.load("bad")


def test_save_overwrites_existing(tmp_state_store):
    state = make_state("overwrite")
    tmp_state_store.save(state)
    state.tax_id = "1234"
    tmp_state_store.save(state)
    loaded = tmp_state_store.load("overwrite")
    assert loaded.tax_id == "1234"


def test_tmp_file_not_left_behind(tmp_path):
    store = StateStore(str(tmp_path / "state"))
    state = make_state("clean")
    store.save(state)
    tmp_files = list((tmp_path / "state").glob("*.tmp"))
    assert tmp_files == [], f"Stale .tmp files found: {tmp_files}"


def test_list_attempts(tmp_state_store):
    for aid in ["alpha", "beta", "gamma"]:
        tmp_state_store.save(make_state(aid))
    ids = tmp_state_store.list_attempts()
    assert ids == ["alpha", "beta", "gamma"]


def test_generate_attempt_id_is_unique():
    ids = {StateStore.generate_attempt_id() for _ in range(100)}
    assert len(ids) == 100


def test_hold_until_date_round_trips(tmp_state_store):
    state = make_state("hold-test")
    state.hold_until_date = "2026-01-01"
    tmp_state_store.save(state)
    loaded = tmp_state_store.load("hold-test")
    assert loaded.hold_until_date == "2026-01-01"
