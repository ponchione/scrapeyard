"""Tests for scrapeyard.common.ids — run ID generation."""

import re

from scrapeyard.common.ids import generate_run_id

_RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


def test_format_matches_expected_pattern():
    run_id = generate_run_id()
    assert _RUN_ID_RE.match(run_id), f"run_id '{run_id}' doesn't match YYYYMMDD-HHMMSS-8hex"


def test_hex_suffix_is_8_chars():
    run_id = generate_run_id()
    hex_part = run_id.split("-", 2)[2]
    assert len(hex_part) == 8
    # Verify it's valid hex
    int(hex_part, 16)


def test_each_call_produces_unique_id():
    ids = {generate_run_id() for _ in range(50)}
    assert len(ids) == 50
