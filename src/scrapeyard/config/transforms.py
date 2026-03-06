"""Transform vocabulary parser and applicator for selector transforms."""

from __future__ import annotations

import re
from collections.abc import Callable


def parse_transform(raw: str) -> Callable[[str], str]:
    """Parse a single transform string into a callable.

    Raises ValueError for unknown transforms or bad syntax.
    """
    parts = raw.split(":", 2)
    name = parts[0]

    if name == "trim":
        return str.strip
    elif name == "lowercase":
        return str.lower
    elif name == "uppercase":
        return str.upper
    elif name == "prepend":
        if len(parts) < 2:
            raise ValueError(f"prepend requires a value: 'prepend:<value>', got '{raw}'")
        prefix = parts[1]
        return lambda s, p=prefix: p + s
    elif name == "append":
        if len(parts) < 2:
            raise ValueError(f"append requires a value: 'append:<value>', got '{raw}'")
        suffix = parts[1]
        return lambda s, sf=suffix: s + sf
    elif name == "replace":
        if len(parts) < 3:
            raise ValueError(
                f"replace requires old and new: 'replace:<old>:<new>', got '{raw}'"
            )
        old, new = parts[1], parts[2]
        return lambda s, o=old, n=new: s.replace(o, n)
    elif name == "regex":
        if len(parts) < 3:
            raise ValueError(
                f"regex requires pattern and replacement: 'regex:<pattern>:<replacement>', got '{raw}'"
            )
        pattern, replacement = parts[1], parts[2]
        compiled = re.compile(pattern)
        return lambda s, c=compiled, r=replacement: c.sub(r, s)
    elif name == "join":
        if len(parts) < 2:
            raise ValueError(f"join requires a separator: 'join:<separator>', got '{raw}'")
        return lambda s: s
    else:
        raise ValueError(f"Unknown transform: '{name}'")


def apply_transforms(value: str, transforms: list[Callable[[str], str]]) -> str:
    """Chain transforms left-to-right, returning the final string."""
    for transform in transforms:
        value = transform(value)
    return value
