"""Transform vocabulary parser and applicator for selector transforms."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable

import regex

from scrapeyard.common.settings import get_settings

# Pattern matching spec-style func("arg1", "arg2") syntax.
_FUNC_RE = re.compile(r"^(\w+)\((.*)\)$")
_DEFAULT_MAX_PIPELINE_STEPS = 32
_DEFAULT_MAX_VALUE_BYTES = 1048576


class TransformTimeoutError(ValueError):
    """Raised when a user-configured regex exceeds its execution deadline."""


class TransformOutputLimitError(ValueError):
    """Raised before a transform can allocate an oversized selector value."""


def transform_pipeline_limit() -> int:
    """Return the configured maximum number of transforms per selector."""

    return int(
        getattr(
            get_settings(),
            "transform_max_pipeline_steps",
            _DEFAULT_MAX_PIPELINE_STEPS,
        )
    )


def _transform_value_limit() -> int:
    return int(
        getattr(
            get_settings(),
            "transform_max_value_bytes",
            _DEFAULT_MAX_VALUE_BYTES,
        )
    )


def _raise_output_limit(observed: int, limit: int) -> None:
    raise TransformOutputLimitError(
        f"Selector transform value exceeds {limit} UTF-8 bytes "
        f"(predicted {observed})"
    )


def _checked_utf8_size(value: str, *, limit: int | None = None) -> int:
    resolved_limit = _transform_value_limit() if limit is None else limit
    # Every Unicode code point occupies at least one UTF-8 byte. Avoid making a
    # second large allocation merely to discover an already-over-limit value.
    if len(value) > resolved_limit:
        _raise_output_limit(len(value), resolved_limit)
    size = len(value.encode("utf-8"))
    if size > resolved_limit:
        _raise_output_limit(size, resolved_limit)
    return size


def _bounded(transform: Callable[[str], str]) -> Callable[[str], str]:
    def _apply(value: str) -> str:
        _checked_utf8_size(value)
        transformed = transform(value)
        _checked_utf8_size(transformed)
        return transformed

    return _apply


def _bounded_concat(value: str, addition: str, *, prepend: bool) -> str:
    limit = _transform_value_limit()
    predicted = _checked_utf8_size(value, limit=limit) + len(addition.encode("utf-8"))
    if predicted > limit:
        _raise_output_limit(predicted, limit)
    return addition + value if prepend else value + addition


def _bounded_literal_replace(value: str, old: str, new: str) -> str:
    limit = _transform_value_limit()
    current = _checked_utf8_size(value, limit=limit)
    occurrences = value.count(old)
    predicted = current + occurrences * (
        len(new.encode("utf-8")) - len(old.encode("utf-8"))
    )
    if predicted > limit:
        _raise_output_limit(predicted, limit)
    return value.replace(old, new)


def _bounded_regex_replace(
    compiled: regex.Pattern[str],
    replacement: str,
    value: str,
) -> str:
    """Build a regex replacement incrementally under the value byte ceiling."""

    limit = _transform_value_limit()
    _checked_utf8_size(value, limit=limit)
    output = io.StringIO()
    output_bytes = 0
    last_end = 0
    try:
        for match in compiled.finditer(value, timeout=_regex_timeout_seconds()):
            unchanged = value[last_end : match.start()]
            expanded = match.expand(replacement)
            output_bytes += len(unchanged.encode("utf-8")) + len(
                expanded.encode("utf-8")
            )
            if output_bytes > limit:
                _raise_output_limit(output_bytes, limit)
            output.write(unchanged)
            output.write(expanded)
            last_end = match.end()
    except TimeoutError as exc:
        raise TransformTimeoutError("Regex replacement transform timed out") from exc
    tail = value[last_end:]
    output_bytes += len(tail.encode("utf-8"))
    if output_bytes > limit:
        _raise_output_limit(output_bytes, limit)
    output.write(tail)
    return output.getvalue()


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

    transform: Callable[[str], str]
    if name == "trim":
        transform = str.strip
    elif name == "collapse_whitespace":
        def _collapse_whitespace(value: str) -> str:
            return re.sub(r"\s+", " ", value).strip()

        transform = _collapse_whitespace
    elif name == "lowercase":
        transform = str.lower
    elif name == "uppercase":
        transform = str.upper
    elif name == "prepend":
        prefix = _require_arg(name, raw, args)

        def _prepend(value: str) -> str:
            return _bounded_concat(value, prefix, prepend=True)

        transform = _prepend
    elif name == "append":
        suffix = _require_arg(name, raw, args)

        def _append(value: str) -> str:
            return _bounded_concat(value, suffix, prepend=False)

        transform = _append
    elif name == "replace":
        if len(args) < 2:
            raise ValueError(f"replace requires old and new, got '{raw}'")
        old, new = args[0], args[1]

        def _replace(value: str) -> str:
            return _bounded_literal_replace(value, old, new)

        transform = _replace
    elif name == "remove":
        needle = _require_arg(name, raw, args)

        def _remove(value: str) -> str:
            return _bounded_literal_replace(value, needle, "")

        transform = _remove
    elif name == "strip_prefix":
        prefix = _require_arg(name, raw, args)

        def _strip_prefix(value: str, p: str = prefix) -> str:
            return value.removeprefix(p)

        transform = _strip_prefix
    elif name == "strip_suffix":
        suffix = _require_arg(name, raw, args)

        def _strip_suffix(value: str, sf: str = suffix) -> str:
            return value.removesuffix(sf)

        transform = _strip_suffix
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

        transform = _extract
    elif name == "default":
        fallback = _require_arg(name, raw, args)

        def _default(value: str) -> str:
            return (
                value
                if value.strip()
                else _bounded_concat("", fallback, prepend=False)
            )

        transform = _default
    elif name == "regex":
        if len(args) < 2:
            raise ValueError(f"regex requires pattern and replacement, got '{raw}'")
        pattern, replacement = args[0], args[1]
        compiled = _compile_regex(pattern)

        def _replace_regex(value: str) -> str:
            return _bounded_regex_replace(compiled, replacement, value)

        transform = _replace_regex
    elif name == "join":
        raise ValueError(
            f"'join' is a list-level operation not supported as a per-value transform. "
            f"Got '{raw}'"
        )
    else:
        raise ValueError(f"Unknown transform: '{name}'")
    return _bounded(transform)


def apply_transforms(value: str, transforms: list[Callable[[str], str]]) -> str:
    """Chain transforms left-to-right, returning the final string."""
    _checked_utf8_size(value)
    for transform in transforms:
        value = transform(value)
        _checked_utf8_size(value)
    return value
