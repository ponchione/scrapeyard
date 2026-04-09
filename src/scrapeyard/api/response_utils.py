"""HTTP response helpers for the API layer."""

from __future__ import annotations

from typing import Any

from fastapi import Response
from fastapi.responses import JSONResponse


def json_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def json_response(status_code: int, content: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)


def no_content_response() -> Response:
    return Response(status_code=204)
