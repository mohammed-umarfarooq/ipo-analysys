"""Live IPO calendar from NSE.

**This module fetches from an undocumented endpoint and will break.** That is a
deliberate, recorded reversal of the project's earlier "no scraping" decision (D17),
taken because the alternative was a hardcoded fixture calendar.

What NSE actually publishes at ``/api/all-upcoming-issues``:

    companyName, symbol, series (EQ|SME), issuePrice ("Rs.750 to Rs.788"),
    issueStartDate, issueEndDate ("25-Aug-2026"), status, issueSize

What it does **not** publish, at this or any other NSE endpoint:

    lot_size          — estimated here from the SEBI minimum application value,
                        and flagged, because the issuer sets the real number.
    allotment_date    — left unknown, which the scheduler already handles: an IPO
                        with no allotment date is skipped with a reason rather than
                        planned against a guessed freeze window.
    GMP               — grey-market premium has no official feed anywhere. It is
                        always the user's input.

So this is not a :class:`~app.providers.gmp.GmpProvider`. That interface returns
:class:`~app.domain.IPOTask`, whose ``allotment_date`` is required and non-nullable —
an imported issue cannot satisfy it, and pretending otherwise would mean inventing
the one date the whole fund-freeze calculation depends on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal

import httpx

from app.config import settings
from app.domain import IssueType, money

#: NSE rejects non-browser clients outright, and the JSON endpoints 403 unless the
#: request carries cookies set by a page load. Hence the handshake in :func:`fetch`.
_HOME = "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"
_API = "https://www.nseindia.com/api/all-upcoming-issues?category=ipo"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: SEBI's ICDR regulations fix the retail minimum application value, so one lot
#: costs roughly this much. It is the only principled way to estimate a lot size
#: without the issuer's prospectus — and it is still an estimate.
_LOT_VALUE_TARGET: dict[IssueType, tuple[Decimal, Decimal]] = {
    IssueType.MAINBOARD: (Decimal("15000"), Decimal("16000")),
    IssueType.SME: (Decimal("100000"), Decimal("120000")),
}

_PRICE = re.compile(r"(\d+(?:\.\d+)?)")


class NseUnavailable(RuntimeError):
    """NSE could not be reached, or answered with something that was not the feed."""


@dataclass(frozen=True)
class ImportedIssue:
    """One issue as NSE describes it, plus what had to be estimated.

    Deliberately not an ``IPOTask``: this is an incomplete record awaiting human
    input, and the type reflects that.
    """

    name: str
    symbol: str
    issue_type: IssueType
    min_price: Decimal
    max_price: Decimal
    open_date: date
    close_date: date
    status: str
    lot_size_estimate: int


@dataclass(frozen=True)
class SkippedIssue:
    """An issue NSE returned that could not be understood. Never silently dropped."""

    name: str
    reason: str


def estimate_lot_size(cutoff_price: Decimal, issue_type: IssueType) -> int:
    """Smallest lot whose value reaches the SEBI minimum application band.

    Checked against live data: Augmont at ₹788 gives 20 lots = ₹15,760, Tempsens at
    ₹300 gives 50 = ₹15,000. Close enough to plan with and wrong often enough to
    flag — real lot sizes are round numbers chosen by the issuer, not derived.

    The ceiling is the answer on its own: it is by definition the smallest lot at or
    above the floor. The upper bound of the band is documented in
    :data:`_LOT_VALUE_TARGET` for the reader, not applied as a clamp — for an issue
    priced above the whole band, one lot is the smallest possible answer and
    trimming it to fit would mean returning zero.
    """
    if cutoff_price <= 0:
        raise ValueError("cutoff price must be positive")
    floor, _ceiling = _LOT_VALUE_TARGET[issue_type]
    lot = (floor / cutoff_price).to_integral_value(rounding=ROUND_CEILING)
    return max(int(lot), 1)


def parse_price_band(raw: str) -> tuple[Decimal, Decimal] | None:
    """``"Rs.750 to Rs.788"`` -> ``(750, 788)``. ``None`` when unparseable.

    A fixed-price issue quotes one number, which becomes both bounds. Anything else
    returns ``None`` so the caller can skip the row with a reason — a guessed price
    band would silently misstate how much capital every bid freezes.
    """
    found = _PRICE.findall(raw or "")
    if not found:
        return None
    low = money(Decimal(found[0]))
    high = money(Decimal(found[-1]))
    if low <= 0 or high <= 0:
        return None
    return (low, high) if low <= high else (high, low)


def parse_nse_date(raw: str) -> date | None:
    """``"25-Aug-2026"`` -> a date. ``None`` when absent or in another format."""
    try:
        return datetime.strptime((raw or "").strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _issue_type(series: str) -> IssueType:
    return IssueType.SME if (series or "").strip().upper() == "SME" else IssueType.MAINBOARD


def parse_rows(rows: list[dict]) -> tuple[list[ImportedIssue], list[SkippedIssue]]:
    """Turn NSE's payload into issues plus an explicit list of what was dropped."""
    issues: list[ImportedIssue] = []
    skipped: list[SkippedIssue] = []

    for row in rows:
        name = str(row.get("companyName") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        if not name:
            skipped.append(SkippedIssue(name=symbol or "(unnamed)", reason="no company name"))
            continue

        band = parse_price_band(str(row.get("issuePrice") or ""))
        if band is None:
            skipped.append(
                SkippedIssue(
                    name=name,
                    reason=f"could not read the price band from {row.get('issuePrice')!r}",
                )
            )
            continue

        close_date = parse_nse_date(str(row.get("issueEndDate") or ""))
        if close_date is None:
            skipped.append(
                SkippedIssue(
                    name=name,
                    reason=f"could not read a close date from {row.get('issueEndDate')!r}",
                )
            )
            continue

        # An open date is recoverable: NSE sometimes omits it for forthcoming issues,
        # and the close date alone is enough to place the fund-freeze window.
        open_date = parse_nse_date(str(row.get("issueStartDate") or "")) or close_date
        if open_date > close_date:
            open_date = close_date

        issue_type = _issue_type(str(row.get("series") or ""))
        issues.append(
            ImportedIssue(
                name=name,
                symbol=symbol or name[:50],
                issue_type=issue_type,
                min_price=band[0],
                max_price=band[1],
                open_date=open_date,
                close_date=close_date,
                status=str(row.get("status") or "").strip() or "Unknown",
                lot_size_estimate=estimate_lot_size(band[1], issue_type),
            )
        )

    return issues, skipped


async def fetch_issues() -> tuple[list[ImportedIssue], list[SkippedIssue]]:
    """Fetch and parse the live calendar.

    Raises :class:`NseUnavailable` rather than returning an empty list, because
    "NSE is down" and "no issues are open this week" must not look the same to the
    caller — one is a fault to report and the other is a fact to display.
    """
    timeout = httpx.Timeout(settings.nse_timeout_seconds)
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as c:
            # Cookie handshake. The JSON endpoint 403s without the cookies this sets.
            await c.get(_HOME, headers={"Accept": "text/html,application/xhtml+xml"})
            response = await c.get(_API, headers={"Accept": "*/*", "Referer": _HOME})
    except httpx.HTTPError as exc:
        raise NseUnavailable(
            f"could not reach NSE ({exc.__class__.__name__}). The endpoint is "
            f"undocumented and unsupported; add issues manually in the meantime."
        ) from exc

    if response.status_code != 200:
        raise NseUnavailable(
            f"NSE answered HTTP {response.status_code}. Its public endpoints throttle "
            f"and change without notice; add issues manually in the meantime."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        # A block page or an interstitial: HTML where JSON was expected.
        raise NseUnavailable(
            "NSE returned a page rather than the issue feed, which usually means the "
            "endpoint moved or the request was blocked. Add issues manually."
        ) from exc

    if not isinstance(payload, list):
        raise NseUnavailable(
            f"NSE's feed changed shape: expected a list of issues, got "
            f"{type(payload).__name__}. Add issues manually."
        )

    return parse_rows([row for row in payload if isinstance(row, dict)])
