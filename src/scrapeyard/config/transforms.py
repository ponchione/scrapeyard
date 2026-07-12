"""Transform vocabulary parser and applicator for selector transforms."""

from __future__ import annotations

import csv
import re
from collections.abc import Callable

import regex

from scrapeyard.common.settings import get_settings

# Pattern matching spec-style func("arg1", "arg2") syntax.
_FUNC_RE = re.compile(r"^(\w+)\((.*)\)$")


class TransformTimeoutError(ValueError):
    """Raised when a user-configured regex exceeds its execution deadline."""


def _compile_regex(pattern: str) -> regex.Pattern[str]:
    settings = get_settings()
    pattern_bytes = len(pattern.encode("utf-8"))
    if pattern_bytes > settings.transform_regex_max_pattern_bytes:
        raise ValueError(
            "Regex transform pattern exceeds "
            f"{settings.transform_regex_max_pattern_bytes} UTF-8 bytes"
        )
    try:
        return regex.compile(pattern)
    except regex.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc


def _regex_timeout_seconds() -> float:
    return get_settings().transform_regex_timeout_seconds


def _parse_args(raw_args: str) -> list[str]:
    """Parse comma-separated, optionally quoted arguments."""
    if not raw_args:
        return []
    try:
        return [
            part.strip()
            for part in next(csv.reader([raw_args], skipinitialspace=True, strict=True))
        ]
    except csv.Error as exc:
        raise ValueError(f"Invalid transform arguments: {exc}") from exc


def _require_arg(name: str, raw: str, args: list[str], label: str = "a value") -> str:
    if not args:
        raise ValueError(f"{name} requires {label}, got '{raw}'")
    return args[0]


def parse_transform(raw: str) -> Callable[[str], str]:
    """Parse a single transform string into a callable.

    Supports both colon syntax (``prepend:value``) and spec function-call
    syntax (``prepend("value")``).

    Raises ValueError for unknown transforms or bad syntax.
    """
    # Try spec func("arg") syntax first.
    m = _FUNC_RE.match(raw)
    if m:
        name = m.group(1)
        args = _parse_args(m.group(2))
    else:
        parts = raw.split(":", 2)
        name = parts[0]
        args = parts[1:] if len(parts) > 1 else []

    if name == "trim":
        return str.strip
    elif name == "collapse_whitespace":
        return lambda s: re.sub(r"\s+", " ", s).strip()
    elif name == "lowercase":
        return str.lower
    elif name == "uppercase":
        return str.upper
    elif name == "prepend":
        prefix = _require_arg(name, raw, args)
        return lambda s: prefix + s
    elif name == "append":
        suffix = _require_arg(name, raw, args)
        return lambda s: s + suffix
    elif name == "replace":
        if len(args) < 2:
            raise ValueError(f"replace requires old and new, got '{raw}'")
        old, new = args[0], args[1]
        return lambda s: s.replace(old, new)
    elif name == "remove":
        needle = _require_arg(name, raw, args)
        return lambda s: s.replace(needle, "")
    elif name == "strip_prefix":
        prefix = _require_arg(name, raw, args)

        def _strip_prefix(value: str, p: str = prefix) -> str:
            return value.removeprefix(p)

        return _strip_prefix
    elif name == "strip_suffix":
        suffix = _require_arg(name, raw, args)

        def _strip_suffix(value: str, sf: str = suffix) -> str:
            return value.removesuffix(sf)

        return _strip_suffix
    elif name == "extract":
        compiled = _compile_regex(_require_arg(name, raw, args, "a pattern"))

        def _extract(value: str, c: regex.Pattern[str] = compiled) -> str:
            try:
                match = c.search(value, timeout=_regex_timeout_seconds())
            except TimeoutError as exc:
                raise TransformTimeoutError("Regex extract transform timed out") from exc
            if match is None:
                return ""
            if match.groups():
                return match.group(1)
            return match.group(0)

        return _extract
    elif name == "default":
        fallback = _require_arg(name, raw, args)
        return lambda s: s if s.strip() else fallback
    elif name == "regex":
        if len(args) < 2:
            raise ValueError(f"regex requires pattern and replacement, got '{raw}'")
        pattern, replacement = args[0], args[1]
        compiled = _compile_regex(pattern)

        def _replace_regex(value: str) -> str:
            try:
                return compiled.sub(
                    replacement,
                    value,
                    timeout=_regex_timeout_seconds(),
                )
            except TimeoutError as exc:
                raise TransformTimeoutError("Regex replacement transform timed out") from exc

        return _replace_regex
    elif name == "join":
        raise ValueError(
            f"'join' is a list-level operation not supported as a per-value transform. "
            f"Got '{raw}'"
        )
    else:
        raise ValueError(f"Unknown transform: '{name}'")


def apply_transforms(value: str, transforms: list[Callable[[str], str]]) -> str:
    """Chain transforms left-to-right, returning the final string."""
    for transform in transforms:
        value = transform(value)
    return value
