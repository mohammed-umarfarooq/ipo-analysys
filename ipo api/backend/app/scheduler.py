"""Deterministic IPO capital-allocation engine.

Given a set of IPOs, a set of PAN accounts with real bank balances, and a start
date, decide **which PAN bids on which IPO on which day** so that a limited pool of
cash is recycled through ASBA freeze cycles to capture the most grey-market premium.

Domain rules enforced
---------------------
Rule 1 (SEBI retail lottery)
    Extra lots under one PAN do not improve odds, so allocation is capped at one
    lot per unique PAN per IPO. Enforced structurally: each PAN is considered at
    most once per IPO.
Rule 2 (ASBA freeze, T+1)
    Capital blocked on the bid date stays frozen through the allotment date and is
    spendable again on ``allotment_date + 1``. Modelled as the half-open interval
    ``[close_date, allotment_date + 1)``.
Rule 3 (priority)
    IPOs rank by GMP% descending, then allotment date ascending.
Rule 4 (just-in-time bidding)
    Bids are placed on the close date. Bidding earlier only freezes capital for
    longer at no benefit, so the engine never does it.

Why the capacity check is an interval check
-------------------------------------------
Rules 3 and 4 pull against each other. Ranking by GMP is meaningless if a bid is
pinned to its close date and a *low*-GMP issue closing earlier has already eaten
the cash. The original blueprint sorted a heap per-day, which only orders IPOs
that close on the *same* day, so its GMP ranking never bound (D1).

This engine instead commits capital in global priority order, and tests each
prospective bid against the *peak* concurrent load on that PAN's account across
the whole footprint of the new block. That makes ranking binding while keeping
bids just-in-time, and it is what stops the schedule from ever overdrawing.

Pooled vs per-PAN capital
-------------------------
The same peak test answers both questions; only its scope changes. Per-PAN compares
one PAN's blocks to that PAN's balance (what a bank enforces). Pooled compares *every*
PAN's blocks to the summed war-chest, which is how people actually plan — "I have this
much, spread it across the family, recycle it as ASBA releases". See
:class:`~app.domain.CapitalMode` for the caveat pooled planning carries.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from app.domain import (
    FOREVER,
    AllotmentAssumption,
    CapitalMode,
    DayRow,
    FundBlock,
    IPOTask,
    PanAccount,
    ScheduleEvent,
    ScheduleResult,
    SchedulingPolicy,
    SkippedIPO,
    money,
)

ZERO = Decimal("0.00")


class _Placement(NamedTuple):
    ipo: IPOTask
    pan_ids: list[str]


class _Ledger:
    """Fund blocks over time, with an exact concurrent-capacity test.

    Frozen capital is a step function that only steps *up* at a block's
    ``block_date``. So the peak is found by evaluating the load at every block start
    date — no need to walk a calendar.

    Two capacity modes, and the difference is the whole pooled-fund feature:

    ``pooled=False`` (per-PAN)
        Each PAN's blocks are tested against that PAN's own balance. This is what a
        bank enforces: ASBA freezes money in the applicant's own account (D4).
    ``pooled=True``
        All blocks are tested against one shared total. A bid under any PAN may draw
        on the whole war-chest, which is how the user thinks about their capital and
        what makes "the fund is 1" schedulable. It assumes cash can be moved into
        whichever account bids before its close date — stated in the UI, not implied.
    """

    def __init__(self, pans: Sequence[PanAccount], *, pooled: bool = False) -> None:
        self._balance: dict[str, Decimal] = {p.id: p.available_balance for p in pans}
        self._by_pan: dict[str, list[FundBlock]] = {p.id: [] for p in pans}
        self._pooled = pooled
        self._pool: Decimal = sum(self._balance.values(), ZERO)

    def balance(self, pan_id: str) -> Decimal:
        return self._balance[pan_id]

    def frozen_at(self, pan_id: str, day: date) -> Decimal:
        return sum((b.amount for b in self._by_pan[pan_id] if b.covers(day)), ZERO)

    def _all_blocks(self) -> list[FundBlock]:
        return [b for blocks in self._by_pan.values() for b in blocks]

    @staticmethod
    def _peak(blocks: Sequence[FundBlock]) -> Decimal:
        if not blocks:
            return ZERO
        # Load only rises at block starts, so the maximum is attained at one of them.
        return max(
            sum((b.amount for b in blocks if b.covers(day)), ZERO)
            for day in {b.block_date for b in blocks}
        )

    def _peak_including(self, pan_id: str, extra: Iterable[FundBlock]) -> Decimal:
        return self._peak([*self._by_pan[pan_id], *extra])

    def can_place(self, pan_id: str, candidate: Sequence[FundBlock]) -> bool:
        """Would adding ``candidate`` ever exceed the capital backing this bid?

        Pooled mode compares the **global** peak across every PAN to the single pool;
        per-PAN mode compares this PAN's peak to its own balance. Splitting a pool
        evenly across PANs up front is *not* equivalent — it would stop one holder
        from using another's idle cash, which is exactly the capital a pooled plan
        exists to recycle.
        """
        if self._pooled:
            return self._peak([*self._all_blocks(), *candidate]) <= self._pool
        return self._peak_including(pan_id, candidate) <= self._balance[pan_id]

    def place(self, candidate: Sequence[FundBlock]) -> None:
        for block in candidate:
            self._by_pan[block.pan_id].append(block)

    def all_blocks(self) -> list[FundBlock]:
        """Every committed block, for reporting. Order is not significant."""
        return self._all_blocks()

    def total_frozen_at(self, day: date) -> Decimal:
        return sum((self.frozen_at(pan_id, day) for pan_id in self._by_pan), ZERO)

    def total_liquid_at(self, day: date) -> Decimal:
        """Cash spendable across every account on ``day``.

        Identical arithmetic in both modes — total capital minus what is frozen. The
        modes differ in what they *allow*, not in what a day's liquidity means.
        """
        return sum(
            (self._balance[pan_id] - self.frozen_at(pan_id, day) for pan_id in self._by_pan),
            ZERO,
        )


class IPOJobScheduler:
    """Plans ASBA bids across multiple PAN accounts.

    Args:
        pans: PAN accounts with the balance ASBA can actually freeze in each.
        policy: :class:`SchedulingPolicy.VALUE_FIRST` (default, correct) or
            ``JIT_GREEDY`` to reproduce the original blueprint for comparison.
        assumption: how to treat capital lost to a successful allotment.
        min_gmp: IPOs below this GMP% are not bid on at all.
        capital_mode: :class:`CapitalMode.PER_PAN` (default, ASBA-accurate) tests each
            PAN's blocks against its own balance; ``POOLED`` tests all blocks against
            the sum, planning one war-chest across every holder.

    Pooled mode still holds N distinct ``PanAccount``s rather than collapsing them into
    one synthetic account, and that is deliberate: Rule 1 (one lot per PAN per issue) is
    enforced by *iterating* PANs, and the commit path writes one application row per
    real PAN. Only the capacity test changes.
    """

    def __init__(
        self,
        pans: Sequence[PanAccount],
        *,
        policy: SchedulingPolicy = SchedulingPolicy.VALUE_FIRST,
        assumption: AllotmentAssumption = AllotmentAssumption.NONE_ALLOTTED,
        min_gmp: Decimal = ZERO,
        capital_mode: CapitalMode = CapitalMode.PER_PAN,
    ) -> None:
        if not pans:
            raise ValueError("at least one PAN account is required")
        seen = {p.id for p in pans}
        if len(seen) != len(pans):
            raise ValueError("duplicate PAN ids")
        self.pans = list(pans)
        self.policy = policy
        self.assumption = assumption
        self.min_gmp = Decimal(str(min_gmp))
        self.capital_mode = capital_mode

    @classmethod
    def from_shared_pool(
        cls,
        liquid_capital: Decimal | float | str,
        active_pans: Sequence[str],
        **kwargs: object,
    ) -> IPOJobScheduler:
        """Build from a single cash pool split evenly across PANs.

        Convenience for the blueprint's original ``(liquid_capital, active_pans)``
        signature. The even split is an ASBA-accurate reading: a mandate can only
        freeze money already sitting in each holder's own account (D4).

        To plan the pool as one war-chest instead — any PAN drawing on the whole
        balance — pass ``capital_mode=CapitalMode.POOLED``. The split then becomes
        irrelevant, since the pooled test only ever looks at the sum.
        """
        if not active_pans:
            raise ValueError("at least one PAN is required")
        total = money(Decimal(str(liquid_capital)))
        share = money(total / len(active_pans))
        pans = [
            PanAccount(id=name, holder_name=name, available_balance=share) for name in active_pans
        ]
        return cls(pans, **kwargs)  # type: ignore[arg-type]

    @property
    def initial_capital(self) -> Decimal:
        return sum((p.available_balance for p in self.pans), ZERO)

    # ------------------------------------------------------------------ public

    def execute_schedule(self, ipos: Sequence[IPOTask], start_date: date) -> ScheduleResult:
        eligible, skipped = self._triage(ipos, start_date)
        ledger = _Ledger(self.pans, pooled=self.capital_mode is CapitalMode.POOLED)

        if self.policy is SchedulingPolicy.VALUE_FIRST:
            placements = self._allocate_value_first(eligible, ledger)
        else:
            placements = self._allocate_jit_greedy(eligible, ledger)

        placed_ids = {p.ipo.id for p in placements}
        shortfall_reason = (
            "the pooled fund was already committed to higher-priority issues on this close date"
            if self.capital_mode is CapitalMode.POOLED
            else (
                "no PAN had enough uncommitted capital on the close date; "
                "capital was already committed to higher-priority issues"
            )
        )
        for ipo in eligible:
            if ipo.id not in placed_ids:
                skipped.append(
                    SkippedIPO(
                        ipo_id=ipo.id,
                        ipo_name=ipo.name,
                        gmp_percent=float(ipo.gmp_percent),
                        reason=shortfall_reason,
                        lots_short_by=float(ipo.lot_cost),
                    )
                )

        return self._report(placements, skipped, ledger, start_date)

    # --------------------------------------------------------------- internals

    def _triage(
        self, ipos: Sequence[IPOTask], start_date: date
    ) -> tuple[list[IPOTask], list[SkippedIPO]]:
        """Split IPOs into biddable and explicitly-rejected, never silently dropped."""
        eligible: list[IPOTask] = []
        skipped: list[SkippedIPO] = []
        for ipo in ipos:
            if ipo.close_date < start_date:
                skipped.append(
                    SkippedIPO(
                        ipo_id=ipo.id,
                        ipo_name=ipo.name,
                        gmp_percent=float(ipo.gmp_percent),
                        reason=f"closed on {ipo.close_date}, before the {start_date} start date",
                    )
                )
            elif ipo.gmp_percent < self.min_gmp:
                skipped.append(
                    SkippedIPO(
                        ipo_id=ipo.id,
                        ipo_name=ipo.name,
                        gmp_percent=float(ipo.gmp_percent),
                        reason=f"GMP {ipo.gmp_percent}% is below the {self.min_gmp}% threshold",
                    )
                )
            else:
                eligible.append(ipo)
        return eligible, skipped

    def _candidate_blocks(self, ipo: IPOTask, pan_id: str) -> list[FundBlock]:
        """The fund blocks one lot under ``pan_id`` would create.

        Always a freeze over ``[close_date, unblock_date)``. Under the ``EXPECTED``
        assumption a second, permanent block represents the share of capital that
        allotment consumes and that therefore never returns (D5).
        """
        blocks = [
            FundBlock(
                pan_id=pan_id,
                ipo_id=ipo.id,
                ipo_name=ipo.name,
                amount=ipo.lot_cost,
                block_date=ipo.close_date,
                unblock_date=ipo.unblock_date,
            )
        ]
        if self.assumption is AllotmentAssumption.EXPECTED and ipo.allotment_probability > 0:
            consumed = money(ipo.lot_cost * ipo.allotment_probability)
            if consumed > 0:
                blocks.append(
                    FundBlock(
                        pan_id=pan_id,
                        ipo_id=ipo.id,
                        ipo_name=f"{ipo.name} (allotted)",
                        amount=consumed,
                        block_date=ipo.unblock_date,
                        unblock_date=FOREVER,
                    )
                )
        return blocks

    def _fill_one_ipo(self, ipo: IPOTask, ledger: _Ledger) -> list[str]:
        """Take up to one lot per PAN, in deterministic PAN order (Rule 1)."""
        used: list[str] = []
        for pan in self.pans:
            candidate = self._candidate_blocks(ipo, pan.id)
            if ledger.can_place(pan.id, candidate):
                ledger.place(candidate)
                used.append(pan.id)
        return used

    def _allocate_value_first(
        self, eligible: Sequence[IPOTask], ledger: _Ledger
    ) -> list[_Placement]:
        """Commit capital in global GMP order, regardless of close date.

        This is what makes Rule 3 binding: the best issue claims capital first, so
        a mediocre issue closing sooner can no longer starve it.
        """
        placements: list[_Placement] = []
        for ipo in sorted(eligible, key=lambda i: i.priority_key()):
            used = self._fill_one_ipo(ipo, ledger)
            if used:
                placements.append(_Placement(ipo, used))
        return placements

    def _allocate_jit_greedy(
        self, eligible: Sequence[IPOTask], ledger: _Ledger
    ) -> list[_Placement]:
        """Original blueprint behaviour: first-come-by-close-date wins.

        Retained only as a baseline to quantify what VALUE_FIRST gains. It leaves
        money on the table whenever a low-GMP issue closes before a high-GMP one.
        """
        placements: list[_Placement] = []
        by_date: dict[date, list[IPOTask]] = {}
        for ipo in eligible:
            by_date.setdefault(ipo.close_date, []).append(ipo)
        for day in sorted(by_date):
            for ipo in sorted(by_date[day], key=lambda i: i.priority_key()):
                used = self._fill_one_ipo(ipo, ledger)
                if used:
                    placements.append(_Placement(ipo, used))
        return placements

    def _daily_timeline(
        self, placements: Sequence[_Placement], ledger: _Ledger
    ) -> list[DayRow]:
        """Project the committed ledger into a day-by-day cashflow matrix.

        Every figure is read back out of the ledger rather than accumulated while bids
        were being placed, so a row cannot disagree with the schedule it describes.
        Only dates where something actually happens get a row — a quiet fortnight is
        not fourteen identical lines.

        The rows reconcile: ``total_locked`` on any date equals the previous row's
        ``total_locked + blocked_today - unblocked_today``. Under the ``EXPECTED``
        assumption the permanent allotment block (D5) appears as ``blocked_today`` on
        the unblock date, which is exactly what it is — the slice of the freeze that
        converts into shares instead of coming back.
        """
        bids: dict[date, list[_Placement]] = {}
        allotments: dict[date, list[str]] = {}
        listings: dict[date, list[str]] = {}
        for placement in placements:
            ipo = placement.ipo
            bids.setdefault(ipo.close_date, []).append(placement)
            allotments.setdefault(ipo.allotment_date, []).append(ipo.name)
            if ipo.listing_date is not None:
                listings.setdefault(ipo.listing_date, []).append(ipo.name)

        blocked: dict[date, Decimal] = {}
        unblocked: dict[date, Decimal] = {}
        for block in ledger.all_blocks():
            blocked[block.block_date] = blocked.get(block.block_date, ZERO) + block.amount
            if block.unblock_date != FOREVER:
                unblocked[block.unblock_date] = (
                    unblocked.get(block.unblock_date, ZERO) + block.amount
                )

        # FOREVER is a sentinel, not a date anyone should see in a table.
        days = sorted(
            (set(bids) | set(allotments) | set(listings) | set(blocked) | set(unblocked))
            - {FOREVER}
        )

        rows: list[DayRow] = []
        for day in days:
            actions = [
                f"{p.ipo.name} ×{len(p.pan_ids)} lot{'s' if len(p.pan_ids) != 1 else ''}"
                for p in sorted(bids.get(day, []), key=lambda p: p.ipo.priority_key())
            ]
            rows.append(
                DayRow(
                    date=day.isoformat(),
                    blocked_today=float(blocked.get(day, ZERO)),
                    total_locked=float(ledger.total_frozen_at(day)),
                    unblocked_today=float(unblocked.get(day, ZERO)),
                    allotments_finalized=sorted(allotments.get(day, [])),
                    listings=sorted(listings.get(day, [])),
                    spendable_balance=float(ledger.total_liquid_at(day)),
                    actions=actions,
                )
            )
        return rows

    def _report(
        self,
        placements: Sequence[_Placement],
        skipped: list[SkippedIPO],
        ledger: _Ledger,
        start_date: date,
    ) -> ScheduleResult:
        """Replay the committed ledger chronologically to produce the Gantt rows."""
        events: list[ScheduleEvent] = []
        total_profit = ZERO

        for placement in sorted(
            placements, key=lambda p: (p.ipo.close_date, p.ipo.priority_key())
        ):
            ipo, pan_ids = placement.ipo, placement.pan_ids
            lots = len(pan_ids)
            blocked = money(ipo.lot_cost * lots)
            profit = money(ipo.expected_profit_per_lot * lots)
            total_profit += profit
            events.append(
                ScheduleEvent(
                    action_date=ipo.close_date.isoformat(),
                    ipo_id=ipo.id,
                    ipo_name=ipo.name,
                    gmp_percent=float(ipo.gmp_percent),
                    lots_applied=lots,
                    pans_used=pan_ids,
                    blocked_amount=float(blocked),
                    allotment_date=ipo.allotment_date.isoformat(),
                    unblock_date=ipo.unblock_date.isoformat(),
                    # Cash spendable across every PAN on the bid date, after this bid.
                    remaining_liquid_balance=float(ledger.total_liquid_at(ipo.close_date)),
                    expected_profit=float(profit),
                )
            )

        timeline = {start_date}
        for placement in placements:
            timeline.add(placement.ipo.close_date)
            timeline.add(placement.ipo.unblock_date)
        peak = max((ledger.total_frozen_at(day) for day in timeline), default=ZERO)

        return ScheduleResult(
            initial_capital=float(self.initial_capital),
            pans_used=[p.id for p in self.pans],
            policy=self.policy.value,
            allotment_assumption=self.assumption.value,
            capital_mode=self.capital_mode.value,
            events=events,
            skipped=skipped,
            total_expected_profit=float(total_profit),
            peak_capital_deployed=float(peak),
            daily_timeline=self._daily_timeline(placements, ledger),
        )
