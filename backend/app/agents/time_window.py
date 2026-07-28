"""Turn supported patient time expressions into bounded scheduling windows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from dateparser.search import search_dates

_DEFAULT_WINDOW_DAYS = 14
_NEXT_WEEK = re.compile(r"\bnext\s+week\b|\bn[aä]chste[rsn]?\s+woche\b", re.IGNORECASE)
_THIS_WEEK = re.compile(r"\bthis\s+week\b|\bdiese[rsn]?\s+woche\b", re.IGNORECASE)
_TOMORROW = re.compile(r"\btomorrow\b|\bmorgen\b", re.IGNORECASE)
_IN_DAYS = re.compile(
    r"\b(?:in|after(?:\s+these)?|nach)\s+(\d{1,2})\s+(?:days?|tagen?)\b",
    re.IGNORECASE,
)
_DATE_CUE = re.compile(
    r"\b(?:on|am|zum|für|for)\b|"
    r"\b(?:next|n[aä]chste[rsn]?)\s+"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b|"
    r"\b\d{4}-\d{1,2}-\d{1,2}\b|"
    r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|"
    r"\b\d{1,2}\s+"
    r"(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december|januar|februar|m[aä]rz|mai|juni|juli|"
    r"oktober|november|dezember)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RequestedWindow:
    start: datetime
    end: datetime
    explicit: bool
    label: str

    def as_state(self) -> dict[str, str | bool]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "explicit": self.explicit,
            "label": self.label,
        }


def _day_window(value: datetime, label: str) -> RequestedWindow:
    return RequestedWindow(
        start=datetime.combine(value.date(), time.min),
        end=datetime.combine(value.date(), time.max),
        explicit=True,
        label=label,
    )


def _calendar_week(now: datetime, *, following: bool, label: str) -> RequestedWindow:
    if following:
        monday = now.date() + timedelta(days=7 - now.weekday())
    else:
        monday = now.date() - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    return RequestedWindow(
        start=max(datetime.combine(monday, time.min), now),
        end=datetime.combine(sunday, time.max),
        explicit=True,
        label=label,
    )


def _parsed_date(text: str, now: datetime) -> datetime | None:
    if not _DATE_CUE.search(text):
        return None
    matches = search_dates(
        text,
        languages=["en", "de"],
        settings={
            "RELATIVE_BASE": now,
            "PREFER_DATES_FROM": "future",
            "DATE_ORDER": "DMY",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if not matches:
        return None
    future = [value for _, value in matches if value.date() >= now.date()]
    return future[0] if future else None


def parse_requested_window(
    text: str,
    *,
    now: datetime | None = None,
    default_days: int = _DEFAULT_WINDOW_DAYS,
) -> RequestedWindow:
    """Parse a conservative set of administrative scheduling expressions.

    Ambiguous prose deliberately returns a bounded default rather than
    pretending to understand it. The LLM receives the resulting dates but
    never decides the allowed window.
    """
    current = now or datetime.now()
    normalized = " ".join(text.split())

    if _NEXT_WEEK.search(normalized):
        return _calendar_week(current, following=True, label="next week")
    if _THIS_WEEK.search(normalized):
        return _calendar_week(current, following=False, label="this week")
    if _TOMORROW.search(normalized):
        return _day_window(current + timedelta(days=1), "tomorrow")

    relative_days = _IN_DAYS.search(normalized)
    if relative_days:
        count = min(int(relative_days.group(1)), 365)
        return _day_window(current + timedelta(days=count), f"in {count} days")

    parsed = _parsed_date(normalized, current)
    if parsed is not None:
        return _day_window(parsed, parsed.date().isoformat())

    return RequestedWindow(
        start=current,
        end=datetime.combine(
            (current + timedelta(days=default_days)).date(),
            time.max,
        ),
        explicit=False,
        label=f"next {default_days} days",
    )
