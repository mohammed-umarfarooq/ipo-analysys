"""Domain types for the IPO capital-allocation engine.

All money is :class:`~decimal.Decimal`. See ``docs/DEVIATIONS.md`` D3 for why: the
database stores ``NUMERIC(14,2)`` and binary floats cannot represent those values
exactly, which produces off-by-one lot counts under floor division.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Money is rounded to paise (2 dp) at every boundary.
PAISA = Decimal("0.01")

#: Sentinel "never unblocks" date, used for capital consumed by a successful allotment.
FOREVER = date.max


def money(value: Decimal | int | str) -> Decimal:
    """Coerce to a paise-quantised Decimal."""
    return Decimal(value).quantize(PAISA, rounding=ROUND_HALF_UP)


class IssueType(str, Enum):
    MAINBOARD = "Mainboard"
    SME = "SME"


class SchedulingPolicy(str, Enum):
    """How the engine resolves contention for scarce capital.

    ``VALUE_FIRST`` is the default and the corrected behaviour. ``JIT_GREEDY``
    reproduces the original blueprint algorithm and exists so the UI can show the
    user what ranking by GMP is actually worth. See D1 in ``docs/DEVIATIONS.md``.
    """

    VALUE_FIRST = "value_first"
    JIT_GREEDY = "jit_greedy"


class AllotmentAssumption(str, Enum):
    """What the planner assumes about capital that gets allotted.

    ASBA unblocks funds only for *un*allotted applications; allotted money is
    debited and becomes shares. See D5 in ``docs/DEVIATIONS.md``.

    ``NONE_ALLOTTED``
        Every application is assumed to fail, so 100% of blocked capital returns
        at ``allotment_date + 1``. Matches the original blueprint. Optimistic:
        it overstates future liquidity.
    ``EXPECTED``
        ``lot_cost * allotment_probability`` is treated as permanently spent from
        the allotment date onward. Conservative and the honest default for planning.
    """

    NONE_ALLOTTED = "none_allotted"
    EXPECTED = "expected"


class CapitalMode(str, Enum):
    """Whether capital is one shared war-chest or ring-fenced per PAN.

    ``POOLED``
        One fund, distributed across PANs and issues by the engine and recycled as
        ASBA unblocks at T+1. The capacity test compares the peak of *all* blocks
        against the single pool. This is how people actually think about IPO
        capital — "I have ₹1.4 lakh, spread it across the family" — and it is the
        default.

        The caveat is real and the UI states it: ASBA freezes money in the
        applicant's *own* bank account, so a pooled plan assumes cash can be moved
        into whichever account bids before its close date. See D4.
    ``PER_PAN``
        Each holder's balance is a separate constraint, which is what a bank
        actually enforces on the day. Pick this for an ASBA-accurate plan.
    """

    POOLED = "pooled"
    PER_PAN = "per_pan"



class PanAccount(BaseModel):
    """One PAN and the bank balance that ASBA can actually freeze.

    ``available_balance`` is per-PAN on purpose. An ASBA mandate blocks money in
    the *applicant's own* account, so a bid under a family member's PAN cannot be
    funded from your balance. See D4 in ``docs/DEVIATIONS.md``.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    holder_name: str
    relation: str = "Self"
    available_balance: Decimal = Decimal("0.00")

    @field_validator("available_balance", mode="before")
    @classmethod
    def _quantise(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))

    @field_validator("available_balance")
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("available_balance cannot be negative")
        return v


class IPOTask(BaseModel):
    """A single IPO the engine may bid on."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    lot_size: int = Field(gt=0)
    gmp_percent: Decimal = Decimal("0.00")
    close_date: date
    allotment_date: date
    #: When the shares list, if known. Not used for capacity — capital is released at
    #: ``unblock_date`` regardless — but the cashflow matrix reports it, because
    #: "when does the gain actually arrive" is the other half of the question.
    listing_date: date | None = None
    issue_type: IssueType = IssueType.MAINBOARD

    #: Retail applies at the cut-off price, i.e. the top of the band. The DB keeps
    #: both bounds; only ``max_price`` determines what gets blocked. See D7.
    min_price: Decimal
    max_price: Decimal

    #: Probability this application is allotted, used only when the assumption is
    #: ``EXPECTED``. 0.0 means "assume no allotment".
    allotment_probability: Decimal = Decimal("0")

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def _quantise_price(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))

    @field_validator("gmp_percent", "allotment_probability", mode="before")
    @classmethod
    def _to_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))

    @field_validator("allotment_probability")
    @classmethod
    def _probability_range(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") <= v <= Decimal("1")):
            raise ValueError("allotment_probability must be between 0 and 1")
        return v

    @model_validator(mode="after")
    def _check_ordering(self) -> IPOTask:
        if self.min_price > self.max_price:
            raise ValueError(f"{self.name}: min_price exceeds max_price")
        if self.allotment_date < self.close_date:
            raise ValueError(f"{self.name}: allotment_date precedes close_date")
        return self

    @property
    def cutoff_price(self) -> Decimal:
        """The price a retail bid is actually placed at."""
        return self.max_price

    @property
    def lot_cost(self) -> Decimal:
        """Capital frozen by one lot."""
        return money(self.cutoff_price * self.lot_size)

    @property
    def unblock_date(self) -> date:
        """First morning the funds are spendable again (allotment date T+1, Rule 2)."""
        return self.allotment_date + timedelta(days=1)

    @property
    def expected_profit_per_lot(self) -> Decimal:
        """Indicative listing gain for one lot at the current GMP."""
        return money(self.lot_cost * self.gmp_percent / Decimal("100"))

    def priority_key(self) -> tuple[Decimal, date, str]:
        """Rule 3 ordering: GMP% descending, then allotment date ascending.

        ``name`` is the final tiebreaker so the schedule is deterministic. Note the
        engine never puts an ``IPOTask`` inside a comparison tuple — the model is
        not orderable and doing so raises ``TypeError`` on ties. See D2.
        """
        return (-self.gmp_percent, self.allotment_date, self.name)


class FundBlock(BaseModel):
    """Capital frozen in one PAN's account over a half-open date interval.

    Covers ``[block_date, unblock_date)``. A block whose ``unblock_date`` is
    :data:`FOREVER` represents capital consumed by an allotment.
    """

    model_config = ConfigDict(frozen=True)

    pan_id: str
    ipo_id: str
    ipo_name: str
    amount: Decimal
    block_date: date
    unblock_date: date

    def covers(self, day: date) -> bool:
        return self.block_date <= day < self.unblock_date

    def overlaps(self, start: date, end: date) -> bool:
        """True if this block is frozen at any point in ``[start, end)``."""
        return self.block_date < end and start < self.unblock_date


class ScheduleEvent(BaseModel):
    """One bid instruction. Field names match the frontend Gantt table contract."""

    action_date: str
    ipo_id: str
    ipo_name: str
    gmp_percent: float
    lots_applied: int
    pans_used: list[str]
    blocked_amount: float
    allotment_date: str
    unblock_date: str
    remaining_liquid_balance: float
    expected_profit: float


class SkippedIPO(BaseModel):
    """An IPO the engine declined to bid on, and why.

    Surfacing these matters as much as the schedule itself: silently dropping an
    IPO looks identical to it not existing.
    """

    ipo_id: str
    ipo_name: str
    gmp_percent: float
    reason: str
    lots_short_by: float = 0.0


class DayRow(BaseModel):
    """One day of the capital lifecycle, as the user's own matrix draws it.

    The Gantt answers "which bids happen when". This answers the question that
    actually decides whether a plan is executable: **how much cash is left on any
    given day, and when does the frozen money come back?**

    Every figure is derived from the committed ledger rather than accumulated while
    placing bids, so the row cannot disagree with the schedule it describes. Emitted
    only for dates where something happens — a quiet week is not forty identical rows.
    """

    date: str
    #: Capital newly frozen by bids placed on this date.
    blocked_today: float
    #: Everything frozen across all PANs as of this date, including earlier bids.
    total_locked: float
    #: Capital released this morning by ASBA (allotment date + 1, Rule 2).
    unblocked_today: float
    #: Issues whose allotment is finalised on this date.
    allotments_finalized: list[str]
    #: Issues listing on this date, where a listing date is known.
    listings: list[str]
    #: Cash spendable across every PAN after the day's blocks and releases.
    spendable_balance: float
    #: Bids placed on this date, as ``"Issue name x2 lots"``.
    actions: list[str]


class ScheduleResult(BaseModel):
    initial_capital: float
    pans_used: list[str]
    policy: str
    allotment_assumption: str
    #: ``pooled`` or ``per_pan`` — which capacity test produced this plan.
    capital_mode: str = CapitalMode.PER_PAN.value
    events: list[ScheduleEvent]
    skipped: list[SkippedIPO]
    total_expected_profit: float
    peak_capital_deployed: float
    #: Day-by-day cashflow, for the matrix view. Empty when nothing was placed.
    daily_timeline: list[DayRow] = Field(default_factory=list)


def floor_lots(available: Decimal, lot_cost: Decimal) -> int:
    """How many whole lots ``available`` buys. Exact — see D3."""
    if lot_cost <= 0:
        raise ValueError("lot_cost must be positive")
    if available < lot_cost:
        return 0
    return int((available / lot_cost).to_integral_value(rounding=ROUND_DOWN))
