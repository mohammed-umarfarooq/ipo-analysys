"""IPO / GMP data sources.

There is no official API for grey-market premium. It exists only on third-party
aggregator sites, as unlabelled HTML, under terms that generally prohibit scraping,
and it changes shape without notice. So this module defines an interface and ships a
deterministic seeded implementation; nothing here scrapes.

To wire a real source later, implement :class:`GmpProvider` and register it in
:func:`get_provider`. Everything upstream depends only on the interface.
"""

from __future__ import annotations

import abc
from datetime import date, timedelta
from decimal import Decimal

from app.domain import IPOTask, IssueType


class GmpProvider(abc.ABC):
    """Supplies the open IPO universe with current GMP attached."""

    @abc.abstractmethod
    async def fetch_open_ipos(self, as_of: date) -> list[IPOTask]:
        """IPOs that are biddable on or after ``as_of``."""

    @abc.abstractmethod
    async def fetch_gmp(self, ipo_name: str) -> Decimal | None:
        """Latest GMP percent for one issue, or ``None`` if unknown."""


class SeededProvider(GmpProvider):
    """Fixed, realistic IPO calendar anchored to a supplied date.

    Dates are expressed as offsets from ``anchor`` so the fixtures never go stale.
    Values are illustrative, not real market data.
    """

    # (name, symbol, band_low, band_high, lot_size, gmp%, open, close, allotment, allot_prob)
    _CALENDAR: tuple[tuple[str, str, str, str, int, str, int, int, int, str], ...] = (
        ("Vertex Semiconductors", "VERTEXSEMI", "985", "1040", 14, "62.50", 0, 2, 5, "0.09"),
        ("Meridian Logistics", "MERIDLOG", "228", "240", 62, "18.75", 1, 3, 6, "0.22"),
        ("Sunhaven Renewables", "SUNHAVEN", "412", "435", 34, "41.00", 2, 4, 8, "0.14"),
        ("Kaveri Agro Foods", "KAVERIAGRO", "94", "99", 150, "6.25", 2, 4, 7, "0.35"),
        ("Northline Speciality Chem", "NORTHCHEM", "1180", "1245", 12, "27.80", 5, 7, 10, "0.11"),
        ("Pragati Microfinance", "PRAGATIMFI", "156", "165", 90, "3.40", 6, 8, 11, "0.48"),
        ("Aether Data Centres", "AETHERDC", "705", "742", 20, "55.20", 8, 10, 13, "0.07"),
        ("Cobalt Tools SME", "COBALTSME", "68", "72", 1600, "12.10", 9, 11, 14, "0.30"),
    )

    def __init__(self, anchor: date | None = None) -> None:
        self.anchor = anchor or date(2026, 3, 2)

    def _build(self) -> list[IPOTask]:
        tasks: list[IPOTask] = []
        for (
            name,
            symbol,
            low,
            high,
            lot,
            gmp,
            open_off,
            close_off,
            allot_off,
            prob,
        ) in self._CALENDAR:
            tasks.append(
                IPOTask(
                    id=symbol,
                    name=name,
                    min_price=Decimal(low),
                    max_price=Decimal(high),
                    lot_size=lot,
                    gmp_percent=Decimal(gmp),
                    close_date=self.anchor + timedelta(days=close_off),
                    allotment_date=self.anchor + timedelta(days=allot_off),
                    issue_type=IssueType.SME if symbol.endswith("SME") else IssueType.MAINBOARD,
                    allotment_probability=Decimal(prob),
                )
            )
        return tasks

    async def fetch_open_ipos(self, as_of: date) -> list[IPOTask]:
        return [ipo for ipo in self._build() if ipo.close_date >= as_of]

    async def fetch_gmp(self, ipo_name: str) -> Decimal | None:
        for ipo in self._build():
            if ipo.name == ipo_name:
                return ipo.gmp_percent
        return None


def get_provider(kind: str = "seeded", *, anchor: date | None = None) -> GmpProvider:
    if kind == "seeded":
        return SeededProvider(anchor=anchor)
    raise ValueError(
        f"unknown GMP provider {kind!r}. Implement GmpProvider and register it here."
    )
