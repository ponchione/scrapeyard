"""Transform vocabulary parser and applicator for selector transforms."""

from __future__ import annotations

import re
from collections.abc import Callable

# Pattern matching spec-style func("arg1", "arg2") syntax.
_FUNC_RE = re.compile(r'^(\w+)\((.+)\)$')


def _parse_args(raw_args: str) -> list[str]:
    """Parse comma-separated, optionally quoted arguments."""
    args: list[str] = []
    for part in raw_args.split(","):
        part = part.strip().strip('"').strip("'")
        args.append(part)
    return args


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
    elif name == "lowercase":
        return str.lower
    elif name == "uppercase":
        return str.upper
    elif name == "prepend":
        if not args:
            raise ValueError(f"prepend requires a value, got '{raw}'")
        prefix = args[0]
        return lambda s, p=prefix: p + s  # type: ignore[misc]
    elif name == "append":
        if not args:
            raise ValueError(f"append requires a value, got '{raw}'")
        suffix = args[0]
        return lambda s, sf=suffix: s + sf  # type: ignore[misc]
    elif name == "replace":
        if len(args) < 2:
            raise ValueError(f"replace requires old and new, got '{raw}'")
        old, new = args[0], args[1]
        return lambda s, o=old, n=new: s.replace(o, n)  # type: ignore[misc]
    elif name == "regex":
        if len(args) < 2:
            raise ValueError(f"regex requires pattern and replacement, got '{raw}'")
        pattern, replacement = args[0], args[1]
        compiled = re.compile(pattern)
        return lambda s, c=compiled, r=replacement: c.sub(r, s)  # type: ignore[misc]
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
