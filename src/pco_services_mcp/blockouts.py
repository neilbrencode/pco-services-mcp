"""Build Blockout payloads from friendly inputs.

Planning Center stores a blockout as a start/end instant plus an optional
recurrence: `repeat_period` (daily/weekly/monthly/yearly), `repeat_frequency`
(`every_1`..`every_8`), `repeat_interval` (how a monthly repeat lands) and
`repeat_until`. Whole-day blockouts made in Planning Center itself run from
00:00:00 to 23:59:59 in the organization's time zone, so these do too.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pco_services_mcp.client import PcoError

Repeat = Literal["none", "daily", "weekly", "monthly", "yearly"]
MonthlyBy = Literal["day_of_month", "week_1", "week_2", "week_3", "week_4", "last_week"]

_MONTHLY_INTERVALS = {
    "day_of_month": "exact_day_of_month",
    "week_1": "week_of_month_1",
    "week_2": "week_of_month_2",
    "week_3": "week_of_month_3",
    "week_4": "week_of_month_4",
    "last_week": "week_of_month_last",
}


def resolve_zone(name: str | None) -> ZoneInfo:
    """An IANA zone, or a refusal: a guessed zone would block the wrong hours."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    raise PcoError(
        f"Can't use the time zone {name!r}. Set PCO_TIME_ZONE to an IANA name such as "
        "America/Chicago."
    )


def _localize(naive: datetime, zone: ZoneInfo) -> datetime:
    """Attach `zone`, refusing a wall-clock time that a DST change skips.

    An ambiguous time (the repeated hour when clocks go back) resolves to its
    first occurrence.
    """
    local = naive.replace(tzinfo=zone, fold=0)
    if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        raise PcoError(
            f"{naive:%Y-%m-%d %H:%M} doesn't exist in {zone.key} (daylight saving change)."
        )
    return local


def _parse(value: str) -> date | datetime:
    value = value.strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PcoError(f"{value!r} is not an ISO date (YYYY-MM-DD) or date-time.") from None


def _instant(value: date | datetime, zone: ZoneInfo, *, end: bool) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else _localize(value, zone)
    return _localize(datetime.combine(value, time(23, 59, 59) if end else time.min), zone)


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_attributes(
    *,
    start: str,
    end: str | None,
    reason: str | None,
    repeat: Repeat,
    every: int,
    monthly_by: MonthlyBy | None,
    repeat_until: str | None,
    zone: ZoneInfo,
) -> dict:
    first = _parse(start)
    if isinstance(first, datetime) and end is None:
        raise PcoError("A blockout that starts at a time needs an end time too.")
    last = _parse(end) if end else first
    starts_at = _instant(first, zone, end=False)
    ends_at = _instant(last, zone, end=True)
    if ends_at <= starts_at:
        raise PcoError("The blockout must end after it starts.")
    attrs: dict = {"starts_at": _utc(starts_at), "ends_at": _utc(ends_at)}
    if reason:
        attrs["reason"] = reason
    if repeat == "none":
        if monthly_by or repeat_until or every != 1:
            raise PcoError("every, monthly_by and repeat_until only apply to a repeating blockout.")
        return attrs
    if not 1 <= every <= 8:
        raise PcoError("Planning Center can repeat every 1 to 8 periods.")
    attrs["repeat_period"] = repeat
    attrs["repeat_frequency"] = f"every_{every}"
    if repeat == "monthly":
        attrs["repeat_interval"] = _MONTHLY_INTERVALS[monthly_by or "day_of_month"]
    elif monthly_by:
        raise PcoError("monthly_by only applies to a monthly repeat.")
    if repeat_until:
        until = _parse(repeat_until)
        if isinstance(until, datetime):
            raise PcoError("repeat_until is a date (YYYY-MM-DD).")
        if until < starts_at.astimezone(zone).date():
            raise PcoError("repeat_until is before the blockout starts.")
        attrs["repeat_until"] = until.isoformat()
    return attrs
