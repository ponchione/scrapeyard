from scrapeyard.common.time import utc_now


def test_utc_now_returns_timezone_aware_datetime() -> None:
    value = utc_now()

    assert value.tzinfo is not None
    assert value.utcoffset().total_seconds() == 0
