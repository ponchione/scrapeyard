"""Path helpers for user-controlled storage components."""

from __future__ import annotations

from pathlib import Path

_UNSAFE_PATH_CHARS = ("/", "\\", "\x00")
MAX_PATH_PART_BYTES = 255


def safe_path_part(value: str, *, label: str = "path component") -> str:
    """Return *value* if it is safe to use as one filesystem path segment."""
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value.strip():
        raise ValueError(f"Unsafe {label}: value must not be blank")
    if (
        value in {".", ".."}
        or any(char in value for char in _UNSAFE_PATH_CHARS)
        or not value.isprintable()
    ):
        raise ValueError(f"Unsafe {label}: {value!r}")
    if len(value.encode("utf-8")) > MAX_PATH_PART_BYTES:
        raise ValueError(f"Unsafe {label}: value must be at most {MAX_PATH_PART_BYTES} bytes")
    return value


def safe_join(root: str | Path, *parts: str) -> Path:
    """Join *parts* beneath *root* after validating each path component."""
    path = Path(root)
    for part in parts:
        path /= safe_path_part(part)
    return path
