"""Shared fixtures and independent verifiers.

The verifiers deliberately reconstruct the capital timeline from the scheduler's
*public output* (``ScheduleResult.events``) instead of inspecting its internal
ledger. A bug in the ledger would then show up as a failing invariant rather than
being masked by the test reading the same wrong state the engine wrote.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.domain import (
    AllotmentAssumption,
    CapitalMode,
    IPOTask,
    PanAccount,
    ScheduleResult,
    money,
)

ZERO = Decimal("0.00")


def D(day: int) -> date:
    """A date in March 2026, for terse fixtures."""
    return date(2026, 3, 1) + timedelta(days=day - 1)


def make_ipo(
    name: str,
    gmp: str,
    close: int,
    allot: int,
    *,
    price: str = "100",
    lot: int = 150,
    ipo_id: str | None = None,
    allot_prob: str = "0",
) -> IPOTask:
    return IPOTask(
        id=ipo_id or name.lower().replace(" ", "-"),
        name=name,
        min_price=Decimal(price),
        max_price=Decimal(price),
        lot_size=lot,
        gmp_percent=Decimal(gmp),
        close_date=D(close),
        allotment_date=D(allot),
        allotment_probability=Decimal(allot_prob),
    )


def make_pan(pan_id: str, balance: str) -> PanAccount:
    return PanAccount(id=pan_id, holder_name=pan_id, available_balance=Decimal(balance))


# --------------------------------------------------------------------- verifiers


def reconstruct_blocks(
    result: ScheduleResult,
    ipos: list[IPOTask],
    assumption: AllotmentAssumption = AllotmentAssumption.NONE_ALLOTTED,
) -> list[tuple[str, Decimal, date, date]]:
    """Rebuild ``(pan_id, amount, start, end)`` blocks from the published schedule."""
    by_id = {i.id: i for i in ipos}
    blocks: list[tuple[str, Decimal, date, date]] = []
    for event in result.events:
        ipo = by_id[event.ipo_id]
        start = date.fromisoformat(event.action_date)
        end = date.fromisoformat(event.unblock_date)
        for pan_id in event.pans_used:
            blocks.append((pan_id, ipo.lot_cost, start, end))
            if assumption is AllotmentAssumption.EXPECTED and ipo.allotment_probability > 0:
                consumed = money(ipo.lot_cost * ipo.allotment_probability)
                if consumed > 0:
                    blocks.append((pan_id, consumed, end, date.max))
    return blocks


def assert_no_overdraft(
    result: ScheduleResult,
    pans: list[PanAccount],
    ipos: list[IPOTask],
    assumption: AllotmentAssumption = AllotmentAssumption.NONE_ALLOTTED,
) -> None:
    """Frozen capital may never exceed the capital that actually backs it.

    This is the invariant that matters most: a schedule that overdraws is not a plan,
    it is a set of bids that will be rejected by the bank.

    What "backs it" means depends on the mode the result reports, and the mode is read
    from the published output rather than passed in — a plan that lied about which test
    produced it would then fail here:

    ``per_pan``
        Each PAN's frozen load against that PAN's own balance. What a bank enforces.
    ``pooled``
        The combined load across every PAN against the summed war-chest. A single PAN
        may exceed its own balance, because the pooled plan assumes cash moves into
        whichever account bids — that is the documented premise of the mode
        (:class:`~app.domain.CapitalMode`), not a leak in the check.
    """
    blocks = reconstruct_blocks(result, ipos, assumption)
    if result.capital_mode == CapitalMode.POOLED.value:
        pool = sum((p.available_balance for p in pans), ZERO)
        for probe in {b[2] for b in blocks}:
            load = sum((amt for _, amt, s, e in blocks if s <= probe < e), ZERO)
            assert load <= pool, (
                f"pooled fund overdrawn on {probe}: {load} frozen against a {pool} pool"
            )
        return

    balances = {p.id: p.available_balance for p in pans}
    for pan_id, balance in balances.items():
        mine = [b for b in blocks if b[0] == pan_id]
        for probe in {b[2] for b in mine}:
            load = sum((amt for _, amt, s, e in mine if s <= probe < e), ZERO)
            assert load <= balance, (
                f"PAN {pan_id} overdrawn on {probe}: "
                f"{load} frozen against a {balance} balance"
            )


def assert_sebi_one_lot_per_pan(result: ScheduleResult, pans: list[PanAccount]) -> None:
    """Rule 1: one lot per unique PAN per IPO, and never more lots than PANs."""
    for event in result.events:
        assert len(set(event.pans_used)) == len(event.pans_used), (
            f"{event.ipo_name}: the same PAN was used twice, which cannot improve "
            f"allotment odds under the SEBI retail lottery"
        )
        assert event.lots_applied == len(event.pans_used)
        assert event.lots_applied <= len(pans)


@pytest.fixture
def three_pans() -> list[PanAccount]:
    return [
        make_pan("SELF", "150000"),
        make_pan("MOTHER", "150000"),
        make_pan("FATHER", "150000"),
    ]
