"""Working-day arithmetic for SEBI's T+3 listing regime.

NSE publishes an issue's open and close dates but **no forward allotment or listing
date** — the registrar fixes those after the book closes. That left every imported
issue unplannable, because the engine needs an allotment date to know when ASBA
releases the money (see :func:`app.repository.to_ipo_task`).

Since 1 December 2023 the T+3 timeline is mandatory, so those dates are estimable
rather than unknowable:

* allotment ≈ close + 1 working day
* listing   ≈ close + 3 working days

Estimating and flagging beats skipping. A schedule built on "close + 1 working day"
is approximately right and can be corrected; an issue silently dropped from the plan
is invisible. Rows filled this way carry ``ipos.dates_estimated = True`` so the UI can
badge them and a human can confirm the registrar's real dates later.

**Limitation, stated rather than hidden:** only weekends are skipped. Indian trading
holidays (Diwali, Holi, and the rest) are not modelled — the exchange holiday list
changes annually and no authoritative feed ships with this project, so a hardcoded
list would rot silently and be wrong in a way nobody notices. An estimate landing on
a holiday is therefore one working day early. That is why these dates are flagged as
estimates and are always user-overridable.
"""

from __future__ import annotations

from datetime import date, timedelta

#: ``date.weekday()`` values for Saturday and Sunday.
_WEEKEND = frozenset({5, 6})

#: Allotment is finalised one working day after the book closes (T+1).
ALLOTMENT_WORKING_DAYS = 1

#: Listing happens within three working days of close (SEBI T+3).
LISTING_WORKING_DAYS = 3


def is_working_day(day: date) -> bool:
    """True for Monday–Friday. Exchange holidays are not modelled — see the module docstring."""
    return day.weekday() not in _WEEKEND


def next_working_day(day: date) -> date:
    """The first working day strictly after ``day``."""
    nxt = day + timedelta(days=1)
    while not is_working_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def add_working_days(start: date, count: int) -> date:
    """Advance ``count`` working days from ``start``.

    ``count`` of 0 returns ``start`` unchanged, even if it is a weekend: the caller is
    asking for an offset, not for normalisation.
    """
    if count < 0:
        raise ValueError("count cannot be negative")
    day = start
    for _ in range(count):
        day = next_working_day(day)
    return day


def estimate_allotment(close_date: date) -> date:
    """Estimated allotment date: one working day after close (T+1).

    A Friday close estimates to the following Monday, which is what the T+3 calendar
    actually does — the weekend is not a working day for the registrar either.
    """
    return add_working_days(close_date, ALLOTMENT_WORKING_DAYS)


def estimate_listing(close_date: date) -> date:
    """Estimated listing date: three working days after close (SEBI T+3)."""
    return add_working_days(close_date, LISTING_WORKING_DAYS)


def estimated_dates(close_date: date) -> tuple[date, date]:
    """Both estimates at once, as ``(allotment, listing)``.

    Returned together because they are only ever written together — a row with an
    estimated allotment date and no listing date would be a state no caller wants.
    """
    return estimate_allotment(close_date), estimate_listing(close_date)
