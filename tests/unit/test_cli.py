from __future__ import annotations

import pytest
import typer

from broker.cli import _normalize_hold_until


def test_normalize_hold_until_accepts_valid_date():
    assert _normalize_hold_until("2026-01-01") == "2026-01-01"


def test_normalize_hold_until_rejects_missing_date():
    with pytest.raises(typer.Exit):
        _normalize_hold_until(None)
