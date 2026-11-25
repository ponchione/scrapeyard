"""Transform vocabulary parser and applicator for selector transforms."""

from __future__ import annotations

import csv
import importlib
import io
import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from typing import Any

import regex

from scrapeyard.common.settings import get_settings

# Pattern matching spec-style func("arg1", "arg2") syntax.
_FUNC_RE = re.compile(r"^(\w+)\((.*)\)$")
_PIPELINE_PREFIX_RE = re.compile(r"\s*(?:(extract:|regex:)|\w+\s*\()")
_CSV_FIELD_RE = re.compile(r'(?:^|,)( *"(?:[^"]|"")*"|[^,]*)')
_WHITESPACE_RE = re.compile(r"\s+")
_DEFAULT_MAX_PIPELINE_STEPS = 32
_DEFAULT_MAX_VALUE_BYTES = 1048576
# Parses a replacement template the way a match's expand() does.
_regex_main: Any = importlib.import_module("regex._main")
_MAX_REGEX_EXPANSION = 10_000
_MAX_REGEX_NESTING = 200
# With both the f and i flags (f is on by default in V1), the compiler turns a
# character range or set into a branch of itself and one string for each
# character it matches whose case folding is several characters: about a
# hundred more nodes, 40 KB, for `[\x00-\U0010FFFF]`.
_FULL_IGNORECASE = int(regex.FULLCASE | regex.IGNORECASE)
# A target's extraction keeps every pattern its selectors use compiled, so
# one config's patterns are held to a budget together as well.
_MAX_CONFIG_REGEX_EXPANSION = 100_000
_config_regex_budget: ContextVar[list[int] | None] = ContextVar(
    "scrapeyard_config_regex_budget", default=None
)


@cache
def _expand_on_folding() -> tuple[int, ...]:
    """Return sorted Unicode code points whose case folding produces >1 char."""
    return tuple(cp for cp in range(0x110000) if len(chr(cp).casefold()) > 1)


@contextmanager
def config_regex_budget() -> Iterator[None]:
    """Hold every regex compiled within, together, to one config's budget."""
    token = _config_regex_budget.set([_MAX_CONFIG_REGEX_EXPANSION])
    try:
        yield
    finally:
        _config_regex_budget.reset(token)


class TransformTimeoutError(ValueError):
    """Raised when a user-configured regex exceeds its execution deadline."""


class TransformOutputLimitError(ValueError):
    """Raised before extraction can retain an oversized selector value.

    The historical exception name is retained for compatibility.  The limit
    now applies to every selector value, whether or not it has transforms.
    """


def transform_pipeline_limit() -> int:
    """Return the configured maximum number of transforms per selector."""

    return int(
        getattr(
            get_settings(),
            "transform_max_pipeline_steps",
            _DEFAULT_MAX_PIPELINE_STEPS,
        )
    )


def split_transform_pipeline(raw: str) -> list[str]:
    """Split a pipeline only at top-level pipes outside quoted arguments."""

    steps: list[str] = []
    start = 0
    quote_open = False
    parentheses = 0
    character_class = False
    index = 0
    while index < len(raw):
        if index == start:
            # Classify each step once; never copy a growing argument prefix.
            prefix = _PIPELINE_PREFIX_RE.match(raw, start)
            prefix_end = prefix.end() if prefix else -1
            pattern_end = -1
            if prefix and prefix[1] == "extract:":
                pattern_end = len(raw)
            elif prefix and prefix[1] == "regex:":
                pattern_end = raw.find(":", prefix_end)
                if pattern_end == -1:
                    pattern_end = len(raw)
        character = raw[index]
        colon_pattern = index <= pattern_end
        if quote_open:
            if character == '"':
                if index + 1 < len(raw) and raw[index + 1] == '"':
                    index += 2
                    continue
                quote_open = False
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character == '"' and parentheses:
            quote_open = True
        elif character == "[" and colon_pattern:
            character_class = True
        elif character == "]" and character_class:
            character_class = False
        elif not character_class:
            if character == "(":
                if parentheses or colon_pattern or index + 1 == prefix_end:
                    parentheses += 1
            elif character == ")":
                if parentheses:
                    parentheses -= 1
            elif character == "|" and parentheses == 0:
                steps.append(raw[start:index].strip())
                start = index + 1
        index += 1
    if quote_open:
        raise ValueError("Unbalanced quote in selector transform pipeline")
    if parentheses:
        raise ValueError("Unbalanced parentheses in selector transform pipeline")
    if character_class:
        raise ValueError("Unbalanced character class in selector transform pipeline")
    steps.append(raw[start:].strip())
    return steps


def parse_transform_pipeline(raw: str) -> list[Callable[[str], str]]:
    """Parse and enforce the configured limit for a complete pipeline."""

    steps = split_transform_pipeline(raw)
    limit = transform_pipeline_limit()
    if len(steps) > limit:
        raise ValueError(f"Selector transform pipeline exceeds {limit} steps")
    if any(not step for step in steps):
        raise ValueError("Selector transform steps must not be blank")
    return [parse_transform(step) for step in steps]


def selector_value_limit() -> int:
    """Return the UTF-8 byte ceiling applied to every selector value."""

    return int(
        getattr(
            get_settings(),
            "transform_max_value_bytes",
            _DEFAULT_MAX_VALUE_BYTES,
        )
    )


def _raise_output_limit(observed: int, limit: int) -> None:
    raise TransformOutputLimitError(
        f"Selector value exceeds {limit} UTF-8 bytes (predicted {observed})"
    )


def checked_selector_value_size(value: str, *, limit: int | None = None) -> int:
    """Return a value's UTF-8 size, rejecting it before avoidable allocation."""

    resolved_limit = selector_value_limit() if limit is None else limit
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
        checked_selector_value_size(value)
        transformed = transform(value)
        checked_selector_value_size(transformed)
        return transformed

    return _apply


def _bounded_concat(value: str, addition: str, *, prepend: bool) -> str:
    limit = selector_value_limit()
    predicted = checked_selector_value_size(value, limit=limit) + len(addition.encode("utf-8"))
    if predicted > limit:
        _raise_output_limit(predicted, limit)
    return addition + value if prepend else value + addition


def _bounded_literal_replace(value: str, old: str, new: str) -> str:
    limit = selector_value_limit()
    current = checked_selector_value_size(value, limit=limit)
    occurrences = value.count(old)
    predicted = current + occurrences * (len(new.encode("utf-8")) - len(old.encode("utf-8")))
    if predicted > limit:
        _raise_output_limit(predicted, limit)
    return value.replace(old, new)


def _bounded_regex_replace(
    compiled: regex.Pattern[str],
    replacement: str,
    value: str,
) -> str:
    """Build a regex replacement incrementally under the value byte ceiling."""

    limit = selector_value_limit()
    checked_selector_value_size(value, limit=limit)
    output = io.StringIO()
    output_bytes = 0
    last_end = 0
    try:
        for match in compiled.finditer(value, timeout=_regex_timeout_seconds()):
            unchanged = value[last_end : match.start()]
            expanded = match.expand(replacement)
            output_bytes += len(unchanged.encode("utf-8")) + len(expanded.encode("utf-8"))
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


def _check_replacement(compiled: regex.Pattern[str], replacement: str) -> None:
    """Refuse a replacement that names a group the pattern lacks, or a bad escape.

    Otherwise it passes validation and fails only on a page whose text matches.
    """
    try:
        parts = _regex_main._compile_replacement_helper(compiled, replacement)
    except (regex.error, IndexError) as exc:
        raise ValueError(f"Invalid regex replacement {replacement!r}: {exc}") from exc
    for part in parts:
        if isinstance(part, int) and part > compiled.groups:
            raise ValueError(
                f"Invalid regex replacement {replacement!r}: the pattern has no group {part}"
            )


_FLAG_LETTERS: dict[str, int] = {
    "i": int(regex.IGNORECASE),
    "f": int(regex.FULLCASE),
    "x": int(regex.VERBOSE),
}
_SIMPLE_ESCAPES: dict[str, int] = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
}


def _apply_inline_flags(
    spec: str,
    current_flags: int,
    *,
    version_override: int | None = None,
) -> tuple[int, int | None]:
    """Apply an inline regex flag string like 'fi', '-i', or 'V1i'."""
    add_part, _, remove_part = spec.partition("-")
    flags = current_flags
    version = version_override
    idx = 0
    while idx < len(add_part):
        if add_part[idx : idx + 2] == "V1":
            version = int(regex.VERSION1)
            flags |= int(regex.VERSION1) | int(regex.FULLCASE)
            idx += 2
            continue
        if add_part[idx : idx + 2] == "V0":
            version = int(regex.VERSION0)
            flags = (flags | int(regex.VERSION0)) & ~int(regex.FULLCASE)
            idx += 2
            continue
        flags |= _FLAG_LETTERS.get(add_part[idx], 0)
        idx += 1
    for ch in remove_part:
        flags &= ~_FLAG_LETTERS.get(ch, 0)
    return flags, version


def _read_escape_codepoint(pattern: str, index: int) -> tuple[int | None, int]:
    """Parse an escape starting after '\\' and return (codepoint_or_None, next_index)."""
    length = len(pattern)
    if index >= length:
        return None, length
    ch = pattern[index]
    if ch in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[ch], index + 1
    if ch == "x" and index + 3 <= length:
        hex_digits = pattern[index + 1 : index + 3]
        if all(c in "0123456789abcdefABCDEF" for c in hex_digits):
            return int(hex_digits, 16), index + 3
    if ch == "u" and index + 5 <= length:
        hex_digits = pattern[index + 1 : index + 5]
        if all(c in "0123456789abcdefABCDEF" for c in hex_digits):
            return int(hex_digits, 16), index + 5
    if ch == "U" and index + 9 <= length:
        hex_digits = pattern[index + 1 : index + 9]
        if all(c in "0123456789abcdefABCDEF" for c in hex_digits):
            value = int(hex_digits, 16)
            return (value if value <= 0x10FFFF else None), index + 9
    if ch in "pPN" and index + 1 < length and pattern[index + 1] == "{":
        close = pattern.find("}", index + 2)
        if close != -1:
            return None, close + 1
    if ch in "dDsSwW":
        return None, index + 1
    if ch in "01234567":
        end = index + 1
        while end < min(length, index + 3) and pattern[end] in "01234567":
            end += 1
        return int(pattern[index:end], 8), end
    return ord(ch), index + 1


def _skip_verbose_trivia(pattern: str, index: int, flags: int) -> int:
    if not (flags & int(regex.VERBOSE)):
        return index
    length = len(pattern)
    while index < length:
        ch = pattern[index]
        if ch in " \t\n\r\f\v":
            index += 1
        elif ch == "#":
            newline = pattern.find("\n", index + 1)
            index = length if newline == -1 else newline + 1
        else:
            break
    return index


def _scan_char_class(pattern: str, start: int, flags: int) -> tuple[int, int]:
    """Scan a `[...]` character class starting at `[` and return (weight, next_index)."""
    length = len(pattern)
    v1 = bool(flags & int(regex.VERSION1))
    index = start + 1
    positive = True
    if index < length and pattern[index] == "^":
        positive = False
        index += 1

    ranges: list[tuple[int, int]] = []
    prev_cp: int | None = None
    in_range = False
    first = True
    depth = 1
    while index < length and depth > 0:
        ch = pattern[index]
        if ch == "\\":
            cp, index = _read_escape_codepoint(pattern, index + 1)
            if in_range and prev_cp is not None and cp is not None:
                ranges.append((min(prev_cp, cp), max(prev_cp, cp)))
                prev_cp = None
                in_range = False
            else:
                if prev_cp is not None:
                    ranges.append((prev_cp, prev_cp))
                prev_cp = cp
                in_range = False
            first = False
            continue
        if ch == "[" and index + 1 < length and pattern[index + 1] in ":=.":
            delim = pattern[index + 1]
            end_token = pattern.find(f"{delim}]", index + 2)
            if end_token != -1:
                index = end_token + 2
                first = False
                continue
        if v1 and ch == "[" and not first:
            depth += 1
            index += 1
            first = True
            continue
        if ch == "]" and not first:
            depth -= 1
            index += 1
            if depth == 0:
                break
            continue
        if ch == "-" and prev_cp is not None and not in_range and index + 1 < length and pattern[index + 1] != "]":
            in_range = True
            index += 1
            continue
        cp = ord(ch)
        if in_range and prev_cp is not None:
            ranges.append((min(prev_cp, cp), max(prev_cp, cp)))
            prev_cp = None
            in_range = False
        else:
            if prev_cp is not None:
                ranges.append((prev_cp, prev_cp))
            prev_cp = cp
        index += 1
        first = False
    if prev_cp is not None:
        ranges.append((prev_cp, prev_cp))

    if not positive or (flags & _FULL_IGNORECASE) != _FULL_IGNORECASE or not ranges:
        return 1, index

    folding = _expand_on_folding()
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    extra = sum(bisect_right(folding, hi) - bisect_left(folding, lo) for lo, hi in merged)
    return 1 + extra, index


def _scan_quantifier(pattern: str, index: int, flags: int) -> tuple[int, bool, int]:
    """Inspect a trailing quantifier and return (copies, is_repeat, next_index)."""
    pos = _skip_verbose_trivia(pattern, index, flags)
    length = len(pattern)
    if pos >= length:
        return 1, False, index
    ch = pattern[pos]
    if ch in "*+?":
        pos += 1
        if pos < length and pattern[pos] in "?+":
            pos += 1
        return 1, True, pos
    if ch == "{":
        scan = pos + 1
        while scan < length and pattern[scan].isascii() and pattern[scan].isdigit():
            scan += 1
        min_digits = pattern[pos + 1 : scan]
        valid = False
        if scan < length and pattern[scan] == "}" and min_digits:
            scan += 1
            valid = True
        elif scan < length and pattern[scan] == ",":
            scan += 1
            max_start = scan
            while scan < length and pattern[scan].isascii() and pattern[scan].isdigit():
                scan += 1
            if scan < length and pattern[scan] == "}" and (min_digits or scan > max_start):
                scan += 1
                valid = True
        if valid:
            if scan < length and pattern[scan] in "?+":
                scan += 1
            min_count = int(min_digits) if min_digits else 0
            return max(1, min_count), True, scan
    return 1, False, index


def _scan_inline_group_header(pattern: str, index: int) -> tuple[str, int, str]:
    """Classify `(?...)` starting at the `?` and return (kind, body_start, flag_spec)."""
    length = len(pattern)
    if index >= length:
        return "group", index, ""
    ch = pattern[index]
    if ch == "#":
        close = pattern.find(")", index + 1)
        return "comment", (length if close == -1 else close + 1), ""
    if ch == ":":
        return "group", index + 1, ""
    if ch in "=!|>":
        return "group", index + 1, ""
    if ch == "<" and index + 1 < length and pattern[index + 1] in "=!":
        return "group", index + 2, ""
    if ch == "P" and index + 1 < length:
        nxt = pattern[index + 1]
        if nxt == "<":
            close = pattern.find(">", index + 2)
            return "group", (length if close == -1 else close + 1), ""
        if nxt == "=":
            close = pattern.find(")", index + 2)
            return "atom", (length if close == -1 else close + 1), ""
    if ch == "(":
        close = pattern.find(")", index + 1)
        return "group", (length if close == -1 else close + 1), ""
    # Inline flags: (?aiLmsuxfV0V1-imsxf) or (?flags:...)
    scan = index
    while scan < length and pattern[scan] in "aiLmsuxfbuprweV01-":
        scan += 1
    if scan > index and scan < length:
        if pattern[scan] == ")":
            return "unscoped_flags", scan + 1, pattern[index:scan]
        if pattern[scan] == ":":
            return "scoped_flags", scan + 1, pattern[index:scan]
    return "group", index + 1, ""


def _collect_global_flags(pattern: str) -> int:
    """Collect unscoped inline flags (x, i, f, V0, V1) that apply to *pattern*."""
    global_flags = 0
    while True:
        found = global_flags
        index = 0
        length = len(pattern)
        while index < length:
            index = _skip_verbose_trivia(pattern, index, found)
            if index >= length:
                break
            ch = pattern[index]
            if ch == "\\":
                _, index = _read_escape_codepoint(pattern, index + 1)
            elif ch == "[":
                _, index = _scan_char_class(pattern, index, found)
            elif ch == "(" and index + 1 < length and pattern[index + 1] == "?":
                kind, next_idx, flag_spec = _scan_inline_group_header(pattern, index + 2)
                if kind == "unscoped_flags":
                    found, _ = _apply_inline_flags(flag_spec, found)
                index = next_idx
            else:
                index += 1
        if found == global_flags:
            return global_flags
        global_flags = found


def _regex_expansion(pattern: str, *, limit: int) -> tuple[int, int]:
    """Estimate compiled regex node expansion without private regex internals.

    Returns ``(node_count, global_flags)`` and short-circuits once past *limit*.
    """
    global_flags = _collect_global_flags(pattern)
    length = len(pattern)

    def _parse_expr(start: int, flags: int, depth: int) -> tuple[int, int]:
        if depth > _MAX_REGEX_NESTING:
            raise RecursionError("Regex nesting exceeds maximum depth")
        total = 0
        index = start
        local_flags = flags
        while index < length:
            index = _skip_verbose_trivia(pattern, index, local_flags)
            if index >= length:
                break
            ch = pattern[index]
            if ch == ")":
                return total, index + 1
            if ch == "|":
                total += 1
                index += 1
                continue
            if ch == "\\":
                _, index = _read_escape_codepoint(pattern, index + 1)
                atom_cost = 1
            elif ch == "[":
                atom_cost, index = _scan_char_class(pattern, index, local_flags)
            elif ch == "(":
                if index + 1 < length and pattern[index + 1] == "?":
                    kind, body_start, flag_spec = _scan_inline_group_header(pattern, index + 2)
                    if kind == "comment":
                        index = body_start
                        continue
                    if kind == "unscoped_flags":
                        local_flags, _ = _apply_inline_flags(flag_spec, local_flags)
                        index = body_start
                        continue
                    if kind == "atom":
                        index = body_start
                        atom_cost = 1
                    elif kind == "scoped_flags":
                        inner_flags, _ = _apply_inline_flags(flag_spec, local_flags)
                        inner_cost, index = _parse_expr(body_start, inner_flags, depth + 1)
                        atom_cost = 1 + inner_cost
                    else:
                        inner_cost, index = _parse_expr(body_start, local_flags, depth + 1)
                        atom_cost = 1 + inner_cost
                else:
                    inner_cost, index = _parse_expr(index + 1, local_flags, depth + 1)
                    atom_cost = 1 + inner_cost
            else:
                index += 1
                atom_cost = 1

            while True:
                copies, is_repeat, next_index = _scan_quantifier(pattern, index, local_flags)
                if not is_repeat:
                    break
                atom_cost = 1 + atom_cost * copies
                index = next_index
                if total + atom_cost > limit:
                    return total + atom_cost, index

            total += atom_cost
            if total > limit:
                return total, index
        return total, index

    count, _ = _parse_expr(0, global_flags, 0)
    return count, global_flags


def _compile_regex(pattern: str) -> regex.Pattern[str]:
    settings = get_settings()
    pattern_bytes = len(pattern.encode("utf-8"))
    if pattern_bytes > settings.transform_regex_max_pattern_bytes:
        raise ValueError(
            "Regex transform pattern exceeds "
            f"{settings.transform_regex_max_pattern_bytes} UTF-8 bytes"
        )
    try:
        expansion, global_flags = _regex_expansion(pattern, limit=_MAX_REGEX_EXPANSION)
    except RecursionError:
        raise ValueError("Invalid regular expression: nested too deeply") from None
    if expansion > _MAX_REGEX_EXPANSION:
        raise ValueError(
            "Regex transform pattern repeats too much: compiled, it would "
            f"exceed {_MAX_REGEX_EXPANSION} nodes; use smaller counted repeats"
        )
    budget = _config_regex_budget.get()
    if budget is not None:
        budget[0] -= expansion
        if budget[0] < 0:
            raise ValueError(
                "Regex transform patterns repeat too much together: compiled, "
                f"one config's would exceed {_MAX_CONFIG_REGEX_EXPANSION} nodes"
            )
    has_v1 = bool(global_flags & int(regex.VERSION1))
    has_v0 = bool(global_flags & int(regex.VERSION0))
    version_flag = regex.VERSION1 if (has_v1 and not has_v0) else (0 if has_v1 else regex.VERSION0)
    try:
        # Not cached: the module's cache would keep every distinct pattern a
        # client sends, compiled, for the life of the process.
        return regex.compile(pattern, flags=version_flag, cache_pattern=False)
    except (regex.error, ValueError) as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc


def _regex_timeout_seconds() -> float:
    return get_settings().transform_regex_timeout_seconds


def _parse_args(raw_args: str) -> list[str]:
    """Parse CSV arguments, preserving literal whitespace inside quotes."""
    if not raw_args:
        return []
    try:
        parts = next(csv.reader([raw_args], skipinitialspace=True, strict=True))
    except csv.Error as exc:
        raise ValueError(f"Invalid transform arguments: {exc}") from exc
    # CSV removes quote markers; inspect raw fields before trimming unquoted values.
    fields = _CSV_FIELD_RE.finditer(raw_args)
    return [
        part if field.group(1).lstrip(" ").startswith('"') else part.strip()
        for part, field in zip(parts, fields, strict=True)
    ]


_TRANSFORM_ARITY = {
    "trim": 0,
    "collapse_whitespace": 0,
    "lowercase": 0,
    "uppercase": 0,
    "prepend": 1,
    "append": 1,
    "remove": 1,
    "strip_prefix": 1,
    "strip_suffix": 1,
    "extract": 1,
    "default": 1,
    "replace": 2,
    "regex": 2,
}


def _validate_arity(name: str, raw: str, args: list[str]) -> None:
    expected = _TRANSFORM_ARITY.get(name)
    if expected is not None and len(args) != expected:
        noun = "argument" if expected == 1 else "arguments"
        raise ValueError(
            f"{name} requires exactly {expected} {noun}, received {len(args)} in '{raw}'"
        )


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
    _validate_arity(name, raw, args)

    transform: Callable[[str], str]
    if name == "trim":
        transform = str.strip
    elif name == "collapse_whitespace":

        def _collapse_whitespace(value: str) -> str:
            return _WHITESPACE_RE.sub(" ", value).strip()

        transform = _collapse_whitespace
    elif name == "lowercase":
        transform = str.lower
    elif name == "uppercase":
        transform = str.upper
    elif name == "prepend":
        prefix = args[0]

        def _prepend(value: str) -> str:
            return _bounded_concat(value, prefix, prepend=True)

        transform = _prepend
    elif name == "append":
        suffix = args[0]

        def _append(value: str) -> str:
            return _bounded_concat(value, suffix, prepend=False)

        transform = _append
    elif name == "replace":
        old, new = args[0], args[1]

        def _replace(value: str) -> str:
            return _bounded_literal_replace(value, old, new)

        transform = _replace
    elif name == "remove":
        needle = args[0]

        def _remove(value: str) -> str:
            return _bounded_literal_replace(value, needle, "")

        transform = _remove
    elif name == "strip_prefix":
        prefix = args[0]

        def _strip_prefix(value: str, p: str = prefix) -> str:
            return value.removeprefix(p)

        transform = _strip_prefix
    elif name == "strip_suffix":
        suffix = args[0]

        def _strip_suffix(value: str, sf: str = suffix) -> str:
            return value.removesuffix(sf)

        transform = _strip_suffix
    elif name == "extract":
        compiled = _compile_regex(args[0])

        def _extract(value: str, c: regex.Pattern[str] = compiled) -> str:
            try:
                match = c.search(value, timeout=_regex_timeout_seconds())
            except TimeoutError as exc:
                raise TransformTimeoutError("Regex extract transform timed out") from exc
            if match is None:
                return ""
            if match.groups():
                return match.group(1) or ""
            return match.group(0)

        transform = _extract
    elif name == "default":
        fallback = args[0]

        def _default(value: str) -> str:
            return value if value.strip() else _bounded_concat("", fallback, prepend=False)

        transform = _default
    elif name == "regex":
        pattern, replacement = args[0], args[1]
        compiled = _compile_regex(pattern)
        _check_replacement(compiled, replacement)

        def _replace_regex(value: str) -> str:
            return _bounded_regex_replace(compiled, replacement, value)

        transform = _replace_regex
    elif name == "join":
        raise ValueError(
            f"'join' is a list-level operation not supported as a per-value transform. Got '{raw}'"
        )
    else:
        raise ValueError(f"Unknown transform: '{name}'")
    return _bounded(transform)


def apply_transforms(value: str, transforms: list[Callable[[str], str]]) -> str:
    """Chain transforms left-to-right, returning the final string."""
    checked_selector_value_size(value)
    for transform in transforms:
        value = transform(value)
        checked_selector_value_size(value)
    return value
