"""Derived pharmacy intelligence for expiry and batch review."""
from __future__ import annotations

import calendar
import re
from datetime import date


def expiry_intelligence(
    expiry: str | None,
    *,
    today: date | None = None,
    near_expiry_days: int = 180,
) -> tuple[str, int | None]:
    """Return (status, days_to_expiry) using the final day of the expiry month."""
    if not expiry:
        return "unknown", None
    match = re.search(
        r"\b(0?[1-9]|1[0-2])[/\-.](\d{2}|\d{4})\b",
        str(expiry),
    )
    if not match:
        return "unknown", None
    month = int(match.group(1))
    year = int(match.group(2))
    if year < 100:
        year += 2000
    try:
        end_date = date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        return "unknown", None
    days = (end_date - (today or date.today())).days
    if days < 0:
        return "expired", days
    if days <= near_expiry_days:
        return "near_expiry", days
    return "valid", days
