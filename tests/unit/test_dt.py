"""Tests for scrapeyard.common.dt — datetime parse/format helpers."""

from datetime import datetime

from scrapeyard.common.dt import fmt_dt, parse_dt


def test_parse_dt_none_returns_none():
    assert parse_dt(None) is None


def test_parse_dt_iso_string():
    result = parse_dt("2024-01-15T10:30:00")
    assert isinstance(result, datetime)
    assert result.year == 2024
    assert result.month == 1
    assert result.day == 15
    assert result.hour == 10
    assert result.minute == 30
    assert result.second == 0


def test_fmt_dt_none_returns_none():
    assert fmt_dt(None) is None


def test_fmt_dt_datetime_returns_iso_string():
    dt = datetime(2024, 1, 15, 10, 30, 0)
    result = fmt_dt(dt)
    assert result == "2024-01-15T10:30:00"


def test_round_trip():
    original = "2024-01-15T10:30:00"
    assert fmt_dt(parse_dt(original)) == original


def test_round_trip_with_timezone_offset():
    original = "2024-06-01T08:00:00+00:00"
    assert fmt_dt(parse_dt(original)) == original
