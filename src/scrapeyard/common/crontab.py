"""Read a standard five-field crontab expression into APScheduler's fields."""

from __future__ import annotations

_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _day_number(token: str) -> int:
    lowered = token.lower()
    if lowered in _DAY_NAMES:
        return _DAY_NAMES.index(lowered)
    if token.isdigit() and int(token) <= 7:
        return int(token)
    raise ValueError(f"Invalid day of week {token!r}")


def standard_day_of_week(field: str) -> str:
    """Give a cron day-of-week field APScheduler's spelling of its meaning.

    Cron counts from Sunday, 0 and 7 both being Sunday. APScheduler 3 counts
    from Monday, so `1-5` meant Tuesday to Saturday there. The days are
    spelled out by name, which both agree on.
    """
    if field == "*":
        return field
    days: set[int] = set()
    for part in field.split(","):
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"Invalid day-of-week step in {part!r}")
            step = int(step_text)
        if base == "*":
            first, last = 0, 6
        elif "-" in base:
            start, _, end = base.partition("-")
            first, last = _day_number(start), _day_number(end)
            # A range may end on Sunday as its last day (`sat-sun`), which
            # APScheduler, counting from Monday, accepted too.
            if end.lower() == "sun" and first > 0:
                last = 7
            if first > last:
                raise ValueError(f"Invalid day-of-week range {base!r}")
        else:
            first = _day_number(base)
            last = 7 if slash else first
        days.update(day % 7 for day in range(first, last + 1, step))
    return ",".join(_DAY_NAMES[day] for day in sorted(days))


def crontab_fields(expression: str) -> dict[str, str]:
    """Split a five-field crontab expression into CronTrigger's keywords."""
    values = expression.split()
    if len(values) != 5:
        raise ValueError(f"Wrong number of fields; got {len(values)}, expected 5")
    minute, hour, day, month, day_of_week = values
    return {
        "minute": minute,
        "hour": hour,
        "day": day,
        "month": month,
        "day_of_week": standard_day_of_week(day_of_week),
    }
