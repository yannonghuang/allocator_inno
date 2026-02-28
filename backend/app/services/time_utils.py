"""Time/period handling for allocation. Period 0 = preexisting (null date)."""
from datetime import datetime, timedelta
from typing import Any


def _parse_date(v: Any) -> datetime | None:
    if v is None or v == "" or (isinstance(v, str) and v.upper() in ("NULL", "NONE", "")):
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")[:10])
        except (ValueError, TypeError):
            return None
    return None


def build_period_index(supply_list: list[dict], demand_list: list[dict]) -> tuple[dict[str, int], list[str]]:
    """
    Collect all supply_date and request_due_time; null → period 0 (preexisting).
    Returns (date_str -> period_index, sorted_dates for reference).
    Period 0 = preexisting; periods 1,2,... = chronological order of distinct dates.
    """
    dates: set[datetime] = set()
    for s in supply_list:
        d = _parse_date(s.get("supply_date"))
        if d is not None:
            dates.add(d)
    for d in demand_list:
        t = _parse_date(d.get("request_due_time"))
        if t is not None:
            dates.add(t)
    sorted_dates = sorted(dates)
    date_to_period: dict[str, int] = {}
    for i, dt in enumerate(sorted_dates):
        date_to_period[dt.strftime("%Y-%m-%d")] = i + 1
    return date_to_period, [dt.strftime("%Y-%m-%d") for dt in sorted_dates]


def period_to_date(period: int | None, sorted_dates: list[str]) -> str | None:
    """
    Map period index to display date. Period 0 = preexisting; 1,2,... = sorted_dates[0], [1], ...
    Returns date string (YYYY-MM-DD) or 'preexisting' for period 0, or None if invalid.
    """
    if period is None:
        return None
    if period <= 0:
        return "preexisting"
    i = period - 1
    if i < 0 or i >= len(sorted_dates):
        return None
    return sorted_dates[i]


def supply_period(supply_date: Any, date_to_period: dict[str, int]) -> int:
    """Period for a supply: 0 if null (preexisting), else period index."""
    d = _parse_date(supply_date)
    if d is None:
        return 0
    key = d.strftime("%Y-%m-%d")
    return date_to_period.get(key, 0)


def demand_due_period(request_due_time: Any, date_to_period: dict[str, int]) -> int:
    """Due period for a demand: 0 = as early as possible (or no date); else period index."""
    d = _parse_date(request_due_time)
    if d is None:
        return 0
    key = d.strftime("%Y-%m-%d")
    return date_to_period.get(key, 0)


def period_plus_days(
    period: int,
    days: float,
    date_to_period: dict[str, int],
    sorted_dates: list[str],
) -> int:
    """
    Return period index that is `days` after the given period (for lead_time / transit_time).
    period 0 = preexisting; use first date in sorted_dates as base if adding days.
    """
    if not sorted_dates or days <= 0:
        return period
    if period <= 0:
        base_str = sorted_dates[0]
    else:
        base_str = sorted_dates[min(period - 1, len(sorted_dates) - 1)]
    base = _parse_date(base_str)
    if base is None:
        return period
    new_date = base + timedelta(days=days)
    new_str = new_date.strftime("%Y-%m-%d")
    if new_str in date_to_period:
        return date_to_period[new_str]
    # Find smallest period whose date >= new_str
    for i, d_str in enumerate(sorted_dates):
        if d_str >= new_str:
            return i + 1
    return len(sorted_dates)


def period_minus_days(
    period: int,
    days: float,
    date_to_period: dict[str, int],
    sorted_dates: list[str],
) -> int:
    """
    Return period index that is `days` before the given period (for lead_time / transit_time).
    period 0 = preexisting; returns 0 if result would be before first date.
    """
    if not sorted_dates or days <= 0 or period is None or period <= 0:
        return max(0, period)
    i = min(period - 1, len(sorted_dates) - 1)
    base_str = sorted_dates[i]
    base = _parse_date(base_str)
    if base is None:
        return period
    new_date = base - timedelta(days=days)
    new_str = new_date.strftime("%Y-%m-%d")
    if new_str in date_to_period:
        return date_to_period[new_str]
    for i, d_str in enumerate(sorted_dates):
        if d_str >= new_str:
            return i + 1
    return 0
