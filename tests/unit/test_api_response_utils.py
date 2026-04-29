from __future__ import annotations

from fastapi import Response

from scrapeyard.api.response_utils import apply_paginated_list_response


def test_apply_paginated_list_response_trims_overfetch_and_sets_headers() -> None:
    response = Response()

    visible_rows = apply_paginated_list_response(
        response,
        rows=["a", "b", "c"],
        limit=2,
        offset=5,
    )

    assert visible_rows == ["a", "b"]
    assert response.headers["X-Scrapeyard-Limit"] == "2"
    assert response.headers["X-Scrapeyard-Offset"] == "5"
    assert response.headers["X-Scrapeyard-Item-Count"] == "2"
    assert response.headers["X-Scrapeyard-Has-More"] == "true"
    assert response.headers["X-Scrapeyard-Next-Offset"] == "7"


def test_apply_paginated_list_response_omits_next_offset_when_page_is_complete() -> None:
    response = Response()

    visible_rows = apply_paginated_list_response(
        response,
        rows=["a"],
        limit=2,
        offset=1,
    )

    assert visible_rows == ["a"]
    assert response.headers["X-Scrapeyard-Limit"] == "2"
    assert response.headers["X-Scrapeyard-Offset"] == "1"
    assert response.headers["X-Scrapeyard-Item-Count"] == "1"
    assert response.headers["X-Scrapeyard-Has-More"] == "false"
    assert "X-Scrapeyard-Next-Offset" not in response.headers
