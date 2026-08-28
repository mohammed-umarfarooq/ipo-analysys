"""Application services: load state, run the engine, persist decisions.

Thin on purpose. All the domain reasoning lives in :mod:`app.scheduler`; this
module's only jobs are to assemble the engine's inputs from the database and to
make sure nothing an IPO row can express is silently dropped on the way in.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import (
    AllotmentAssumption,
    CapitalMode,
    IPOTask,
    PanAccount,
    ScheduleResult,
    SchedulingPolicy,
    SkippedIPO,
)
from app.models import Ipo, IpoApplication, PanAccountRow, UserProfile
from app.repository import load_applications, load_ipos, to_ipo_task, to_pan_account
from app.scheduler import IPOJobScheduler


class NoCapitalError(RuntimeError):
    """No active PAN account has any balance, so nothing can be planned."""


async def _engine_inputs(
    session: AsyncSession, user: UserProfile
) -> tuple[list[PanAccount], list[IPOTask], list[SkippedIPO]]:
    """Domain inputs plus the IPOs that could not be turned into tasks at all."""
    pans = [to_pan_account(row) for row in user.pans if row.is_active]
    if not pans:
        raise NoCapitalError("no active PAN accounts")

    tasks: list[IPOTask] = []
    unschedulable: list[SkippedIPO] = []
    for row in await load_ipos(session):
        task = to_ipo_task(row)
        if task is None:
            unschedulable.append(
                SkippedIPO(
                    ipo_id=row.id,
                    ipo_name=row.name,
                    gmp_percent=float(row.gmp_percent),
                    reason="the registrar has not fixed an allotment date yet, "
                    "so the fund-freeze window is unknown",
                )
            )
        else:
            tasks.append(task)
    return pans, tasks, unschedulable


async def build_schedule(
    session: AsyncSession,
    user: UserProfile,
    *,
    policy: SchedulingPolicy,
    assumption: AllotmentAssumption,
    min_gmp: Decimal,
    start_date: date,
) -> ScheduleResult:
    pans, tasks, unschedulable = await _engine_inputs(session, user)
    engine = IPOJobScheduler(
        pans,
        policy=policy,
        assumption=assumption,
        min_gmp=min_gmp,
        # Read from the profile rather than the request: it is durable user state, not
        # a per-run knob, and the same setting has to hold for `compare_policies` and
        # for the plan that gets committed.
        capital_mode=CapitalMode(user.capital_mode),
    )
    result = engine.execute_schedule(tasks, start_date)
    # Prepend rather than discard: an issue missing from both lists is
    # indistinguishable from one that does not exist.
    result.skipped = [*unschedulable, *result.skipped]
    return result


async def compare_policies(
    session: AsyncSession,
    user: UserProfile,
    *,
    assumption: AllotmentAssumption,
    min_gmp: Decimal,
    start_date: date,
) -> dict[str, object]:
    """Run both policies on identical inputs — the D1 finding, in rupees.

    The frontend shows this directly, because "the ranking now binds" is only
    convincing as a number.
    """
    corrected = await build_schedule(
        session,
        user,
        policy=SchedulingPolicy.VALUE_FIRST,
        assumption=assumption,
        min_gmp=min_gmp,
        start_date=start_date,
    )
    baseline = await build_schedule(
        session,
        user,
        policy=SchedulingPolicy.JIT_GREEDY,
        assumption=assumption,
        min_gmp=min_gmp,
        start_date=start_date,
    )
    return {
        "value_first": corrected,
        "jit_greedy": baseline,
        "delta_expected_profit": round(
            corrected.total_expected_profit - baseline.total_expected_profit, 2
        ),
        "capital_constrained": len(corrected.skipped) > len(baseline.skipped)
        or corrected.total_expected_profit != baseline.total_expected_profit,
    }


async def commit_schedule(
    session: AsyncSession, user: UserProfile, result: ScheduleResult
) -> dict[str, int]:
    """Persist a plan as one application row per (IPO, PAN).

    One row per PAN and never more than one lot, so the database constraints that
    encode Rule 1 are the last line of defence rather than decoration (D10).
    Already-recorded pairs are left alone, so re-committing the same plan is a
    no-op instead of an integrity error.
    """
    existing = {(a.ipo_id, a.pan_id) for a in await load_applications(session, user.id)}
    ipos_by_id = {row.id: row for row in await load_ipos(session)}
    valid_pans = {row.id for row in user.pans}

    created = 0
    for event in result.events:
        ipo = ipos_by_id.get(event.ipo_id)
        if ipo is None:
            continue
        lot_cost = Decimal(str(event.blocked_amount)) / event.lots_applied
        for pan_id in event.pans_used:
            # The plan is generated server-side, but never trust an id blindly:
            # a PAN outside this user's set must not be written to.
            if pan_id not in valid_pans or (ipo.id, pan_id) in existing:
                continue
            session.add(
                IpoApplication(
                    ipo_id=ipo.id,
                    pan_id=pan_id,
                    lots_applied=1,
                    blocked_amount=lot_cost,
                    bid_date=date.fromisoformat(event.action_date),
                    unblock_date=date.fromisoformat(event.unblock_date),
                    allotment_status="APPLIED",
                )
            )
            created += 1
    await session.commit()
    return {"applications_created": created, "already_recorded": len(existing)}


def rank_ipos(rows: list[Ipo]) -> dict[str, int]:
    """Rule 3 rank per IPO id. Issues with no allotment date are unranked."""
    tasks = [t for t in (to_ipo_task(r) for r in rows) if t is not None]
    ordered = sorted(tasks, key=lambda t: t.priority_key())
    return {task.id: position for position, task in enumerate(ordered, start=1)}


def pan_summary(row: PanAccountRow) -> dict[str, object]:
    """What the UI is allowed to see about a PAN — never the number itself."""
    return {
        "id": row.id,
        "holder_name": row.holder_name,
        "relation": row.relation,
        "pan_masked": row.pan_masked,
        "upi_id": row.upi_id,
        "available_balance": float(row.available_balance),
        "is_active": row.is_active,
    }
