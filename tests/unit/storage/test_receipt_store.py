"""Tests for ReceiptStore — raw receipt persistence."""

from __future__ import annotations

from broker.enums import EntityType


RAW_RECEIPT = "<RECEIPT success='true'><PROJECT accession='PRJEB1'/></RECEIPT>"


def test_save_and_load_round_trip(tmp_receipt_store):
    tmp_receipt_store.save("atm-1", EntityType.PROJECT, "p1", RAW_RECEIPT)
    loaded = tmp_receipt_store.load("atm-1", EntityType.PROJECT, "p1")
    assert loaded == RAW_RECEIPT


def test_load_missing_returns_none(tmp_receipt_store):
    result = tmp_receipt_store.load("atm-1", EntityType.PROJECT, "missing")
    assert result is None


def test_save_preserves_verbatim(tmp_receipt_store):
    messy = "  \n<RECEIPT>\n  <tab/>\n</RECEIPT>\n  "
    tmp_receipt_store.save("atm-1", EntityType.SAMPLE, "s1", messy)
    loaded = tmp_receipt_store.load("atm-1", EntityType.SAMPLE, "s1")
    assert loaded == messy


def test_separate_attempts_do_not_collide(tmp_receipt_store):
    tmp_receipt_store.save("atm-1", EntityType.PROJECT, "p1", "receipt-A")
    tmp_receipt_store.save("atm-2", EntityType.PROJECT, "p1", "receipt-B")
    assert tmp_receipt_store.load("atm-1", EntityType.PROJECT, "p1") == "receipt-A"
    assert tmp_receipt_store.load("atm-2", EntityType.PROJECT, "p1") == "receipt-B"
