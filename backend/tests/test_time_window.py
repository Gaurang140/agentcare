"""Deterministic English/German scheduling-window parsing."""

from datetime import datetime

from app.agents.time_window import parse_requested_window

_NOW = datetime(2026, 7, 28, 17, 30)  # Tuesday


def test_english_next_week_is_the_following_monday_through_sunday():
    window = parse_requested_window(
        "Book a general doctor appointment next week",
        now=_NOW,
    )

    assert window.explicit is True
    assert window.start == datetime(2026, 8, 3, 0, 0)
    assert window.end == datetime(2026, 8, 9, 23, 59, 59, 999999)


def test_german_next_week_matches_the_same_calendar_window():
    window = parse_requested_window(
        "Bitte nächste Woche einen Termin in der Allgemeinmedizin buchen",
        now=_NOW,
    )

    assert window.explicit is True
    assert window.start == datetime(2026, 8, 3, 0, 0)
    assert window.end == datetime(2026, 8, 9, 23, 59, 59, 999999)


def test_tomorrow_is_a_single_day_and_never_starts_before_now():
    window = parse_requested_window("tomorrow morning please", now=_NOW)

    assert window.explicit is True
    assert window.start == datetime(2026, 7, 29, 0, 0)
    assert window.end == datetime(2026, 7, 29, 23, 59, 59, 999999)


def test_no_time_expression_uses_bounded_future_default():
    window = parse_requested_window("Book a general doctor", now=_NOW)

    assert window.explicit is False
    assert window.start == _NOW
    assert window.end == datetime(2026, 8, 11, 23, 59, 59, 999999)
