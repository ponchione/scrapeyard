"""Transform vocabulary parser and applicator for selector transforms."""

from __future__ import annotations

import csv
import importlib
import io
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import regex

from scrapeyard.common.settings import get_settings

# Pattern matching spec-style func("arg1", "arg2") syntax.
_FUNC_RE = re.compile(r"^(\w+)\((.*)\)$")
_PIPELINE_PREFIX_RE = re.compile(r"\s*(?:(extract:|regex:)|\w+\s*\()")
_DEFAULT_MAX_PIPELINE_STEPS = 32
_DEFAULT_MAX_VALUE_BYTES = 1048576
# The regex module's compiler copies a counted repeat's body once for each of
# its minimum count: `(?:a{1000}){1000}` builds a million nodes, about 250 MB
# and three seconds, from 17 bytes, and `a{99999}` 250 times fits the pattern
# size limit. The module's own parser, which copies nothing, gives the counts
# before the compiler runs; it is internal to the pinned regex release.
_regex_core: Any = importlib.import_module("regex._regex_core")
# Parses a replacement template the way a match's expand() does.
_regex_main: Any = importlib.import_module("regex._main")
_MAX_REGEX_EXPANSION = 10_000
# With both the f and i flags (f is on by default in V1), the compiler turns a
# character range or set into a branch of itself and one string for each
# character it matches whose case folding is several characters: about a
# hundred more nodes, 40 KB, for `[\x00-\U0010FFFF]`.
_FULL_IGNORECASE = regex.FULLCASE | regex.IGNORECASE
_EXPAND_ON_FOLDING = tuple(ord(ch) for ch in _regex_core._regex.get_expand_on_folding())
# A target's extraction keeps every pattern its selectors use compiled, so
# one config's patterns are held to a budget together as well.
_MAX_CONFIG_REGEX_EXPANSION = 100_000
_config_regex_budget: ContextVar[list[int] | None] = ContextVar(
    "scrapeyard_config_regex_budget", default=None
)


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


def _parse_regex(pattern: str) -> Any:
    """Parse *pattern* as ``regex.compile`` does, without compiling it."""
    _regex_core.DEFAULT_VERSION = regex.DEFAULT_VERSION
    global_flags = 0
    while True:
        source = _regex_core.Source(pattern)
        info = _regex_core.Info(global_flags, source.char_type, {})
        info.guess_encoding = regex.UNICODE
        source.ignore_space = bool(info.flags & regex.VERBOSE)
        try:
            return _regex_core._parse_pattern(source, info)
        except _regex_core._UnscopedFlagSet:
            # A flag set in the pattern applies to all of it: parse again.
            global_flags = info.global_flags


def _node_weight(node: Any, *, in_set: bool) -> int:
    """Return how many nodes one copy of *node* compiles to, as far as it adds."""
    if (
        in_set
        or not isinstance(node, (_regex_core.Range, _regex_core.SetBase))
        or not node.positive
        or node.case_flags & _FULL_IGNORECASE != _FULL_IGNORECASE
    ):
        return 1
    return 1 + sum(1 for ch in _EXPAND_ON_FOLDING if node.matches(ch))


def _regex_expansion(pattern: str, *, limit: int) -> int:
    """Count the nodes compiling *pattern* builds, stopping once past *limit*.

    Each parsed node counts its weight once for each copy the compiler makes
    of it: a repeat's body is copied its minimum count of times (once at
    least). A set's members are compiled into the set, not expanded apart.
    """
    total = 0
    pending: list[tuple[Any, int, bool]] = [(_parse_regex(pattern), 1, False)]
    while pending:
        node, copies, in_set = pending.pop()
        total += copies * _node_weight(node, in_set=in_set)
        if total > limit:
            return total
        if isinstance(node, _regex_core.GreedyRepeat):
            copies *= max(1, node.min_count)
        in_set = in_set or isinstance(node, _regex_core.SetBase)
        for value in vars(node).values():
            children = value if isinstance(value, (list, tuple)) else (value,)
            pending.extend(
                (child, copies, in_set)
                for child in children
                if isinstance(child, _regex_core.RegexBase)
            )
    return total


def _compile_regex(pattern: str) -> regex.Pattern[str]:
    settings = get_settings()
    pattern_bytes = len(pattern.encode("utf-8"))
    if pattern_bytes > settings.transform_regex_max_pattern_bytes:
        raise ValueError(
            "Regex transform pattern exceeds "
            f"{settings.transform_regex_max_pattern_bytes} UTF-8 bytes"
        )
    try:
        expansion = _regex_expansion(pattern, limit=_MAX_REGEX_EXPANSION)
    except regex.error:
        # Compiling stops at the same error, before building anything.
        expansion = 0
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
    try:
        # Not cached: the module's cache would keep every distinct pattern a
        # client sends, compiled, for the life of the process.
        return regex.compile(pattern, cache_pattern=False)
    except regex.error as exc:
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
    fields = re.finditer(r'(?:^|,)( *"(?:[^"]|"")*"|[^,]*)', raw_args)
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
            return re.sub(r"\s+", " ", value).strip()

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
