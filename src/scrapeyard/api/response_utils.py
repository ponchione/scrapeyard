"""HTTP response helpers for the API layer."""

from __future__ import annotations

from typing import Any, NoReturn, TypeVar

from fastapi import HTTPException, Response
from fastapi.responses import JSONResponse

_Row = TypeVar("_Row")


def json_response(status_code: int, content: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)


def raise_json_error(status_code: int, message: str) -> NoReturn:
    raise HTTPException(status_code=status_code, detail=message)


def apply_paginated_list_response(
    response: Response,
    *,
    rows: list[_Row],
    limit: int,
    offset: int,
) -> list[_Row]:
    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    item_count = len(visible_rows)
    response.headers["X-Scrapeyard-Limit"] = str(limit)
    response.headers["X-Scrapeyard-Offset"] = str(offset)
    response.headers["X-Scrapeyard-Item-Count"] = str(item_count)
    response.headers["X-Scrapeyard-Has-More"] = "true" if has_more else "false"
    if has_more:
        response.headers["X-Scrapeyard-Next-Offset"] = str(offset + item_count)
    return visible_rows


def no_content_response() -> Response:
    return Response(status_code=204)
