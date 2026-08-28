"""Smoke run: prints a full schedule from the seeded IPO calendar.

    uv run python -m app.demo

Exists so the capital lifecycle can be checked by eye before any UI exists, and so
the effect of the D1 fix is visible in rupees rather than only in a test assertion.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from decimal import Decimal

from app.domain import AllotmentAssumption, PanAccount, SchedulingPolicy
from app.providers.gmp import SeededProvider
from app.scheduler import IPOJobScheduler


def _init_console() -> tuple[str, str]:
    """Return (rupee, box-drawing char) that this console can actually render.

    The default Windows code page is cp1252, which cannot encode U+20B9. Try to
    switch stdout to UTF-8 and fall back to ASCII rather than dying on a print.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        pass
    try:
        "₹─".encode(sys.stdout.encoding or "ascii")
        return "₹", "─"
    except (UnicodeEncodeError, LookupError):  # pragma: no cover
        return "Rs.", "-"


RUPEE, BAR = _init_console()


def inr(amount: float | Decimal) -> str:
    """Indian digit grouping: 12,34,567.89 rather than 1,234,567.89."""
    whole, _, frac = f"{Decimal(str(amount)):.2f}".partition(".")
    negative = whole.startswith("-")
    whole = whole.lstrip("-")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"{'-' if negative else ''}{RUPEE}{whole}.{frac}"


def rule(width: int = 112) -> None:
    print(BAR * width)


async def main() -> None:
    as_of = date(2026, 3, 1)
    pans = [
        PanAccount(id="SELF", holder_name="Mohammed", relation="Self",
                   available_balance=Decimal("180000")),
        PanAccount(id="MOTHER", holder_name="Aisha", relation="Mother",
                   available_balance=Decimal("95000")),
        PanAccount(id="FATHER", holder_name="Rashid", relation="Father",
                   available_balance=Decimal("60000")),
    ]

    ipos = await SeededProvider(anchor=date(2026, 3, 2)).fetch_open_ipos(as_of)

    print()
    print("IPO Copilot — capital lifecycle plan")
    rule()
    print(f"As of {as_of}    "
          f"{len(pans)} PAN accounts    "
          f"{len(ipos)} open issues")
    for pan in pans:
        print(f"    {pan.relation:<8s} {pan.holder_name:<10s} {inr(pan.available_balance):>15s}")
    total = sum((p.available_balance for p in pans), Decimal("0"))
    print(f"    {'TOTAL':<19s} {inr(total):>15s}")
    print()

    scheduler = IPOJobScheduler(
        pans,
        policy=SchedulingPolicy.VALUE_FIRST,
        assumption=AllotmentAssumption.EXPECTED,
        min_gmp=Decimal("10"),
    )
    result = scheduler.execute_schedule(ipos, as_of)

    header = (
        f"{'BID DATE':<12}{'IPO':<28}{'GMP':>7}  {'LOTS':>4}  "
        f"{'BLOCKED':>14}{'UNBLOCKS':>12}{'CASH LEFT':>15}"
    )
    print(header)
    rule()
    for event in result.events:
        pans_note = "+".join(p[:3] for p in event.pans_used)
        print(
            f"{event.action_date:<12}{event.ipo_name[:27]:<28}"
            f"{event.gmp_percent:>6.2f}%  {event.lots_applied:>4}  "
            f"{inr(event.blocked_amount):>14}{event.unblock_date:>12}"
            f"{inr(event.remaining_liquid_balance):>15}   {pans_note}"
        )
    rule()
    print(f"{'':<12}{'expected listing gain':<28}{inr(result.total_expected_profit):>29}")
    print(f"{'':<12}{'peak capital deployed':<28}{inr(result.peak_capital_deployed):>29}")
    print()

    if result.skipped:
        print("Not bid on")
        rule()
        for skip in result.skipped:
            print(f"    {skip.ipo_name[:30]:<32}{skip.gmp_percent:>6.2f}%   {skip.reason}")
        print()

    # The D1 fix, in rupees. The scenario above is not capital-constrained — every
    # issue is affordable, so both policies agree and the ranking never has to break
    # a tie. Contention is where they diverge, so show that case explicitly.
    baseline = IPOJobScheduler(
        pans,
        policy=SchedulingPolicy.JIT_GREEDY,
        assumption=AllotmentAssumption.EXPECTED,
        min_gmp=Decimal("10"),
    ).execute_schedule(ipos, as_of)

    print("Policy comparison")
    rule()
    print(f"    unconstrained ({inr(total)} across {len(pans)} PANs) — both policies agree")
    print(f"        {'value-first (GMP ranking binds)':<38}{inr(result.total_expected_profit):>16}")
    print(f"        {'jit-greedy (original blueprint)':<38}{inr(baseline.total_expected_profit):>16}")
    print()

    # Same calendar, one PAN, room for only one lot at a time — so every bid has an
    # opportunity cost and the ranking is what decides the outcome.
    tight = [PanAccount(id="SELF", holder_name="Mohammed", available_balance=Decimal("15000"))]
    scarce_good = IPOJobScheduler(
        tight, policy=SchedulingPolicy.VALUE_FIRST, min_gmp=Decimal("10")
    ).execute_schedule(ipos, as_of)
    scarce_base = IPOJobScheduler(
        tight, policy=SchedulingPolicy.JIT_GREEDY, min_gmp=Decimal("10")
    ).execute_schedule(ipos, as_of)
    delta = scarce_good.total_expected_profit - scarce_base.total_expected_profit

    print(f"    constrained ({inr(tight[0].available_balance)} in one PAN) — ranking decides")
    print(f"        {'value-first':<38}{inr(scarce_good.total_expected_profit):>16}")
    for event in scarce_good.events:
        print(f"            {event.gmp_percent:>6.2f}%  {event.ipo_name}")
    print(f"        {'jit-greedy (original blueprint)':<38}"
          f"{inr(scarce_base.total_expected_profit):>16}")
    for event in scarce_base.events:
        print(f"            {event.gmp_percent:>6.2f}%  {event.ipo_name}")
    sign = "+" if delta >= 0 else "-"
    print(f"        {'difference':<38}{sign + inr(abs(delta)):>16}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
