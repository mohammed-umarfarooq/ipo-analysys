"""Invariants that must hold for every schedule the engine can produce.

The rule tests cover specific scenarios. These cover *all* scenarios: a seeded
random sweep generates hundreds of IPO calendars and asserts the same properties
on each. Seeds are fixed, so a failure is always reproducible.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.domain import (
    AllotmentAssumption,
    PanAccount,
    ScheduleResult,
    SchedulingPolicy,
)
from app.providers.gmp import SeededProvider
from app.scheduler import IPOJobScheduler
from tests.conftest import (
    D,
    assert_no_overdraft,
    assert_sebi_one_lot_per_pan,
    make_ipo,
    make_pan,
    reconstruct_blocks,
)

ZERO = Decimal("0.00")


def random_scenario(rng: random.Random) -> tuple[list[PanAccount], list, date]:
    """A plausible but arbitrary IPO calendar and PAN set."""
    start = date(2026, 3, 1)
    pans = [
        make_pan(f"PAN{i}", str(rng.choice([15000, 40000, 90000, 250000])))
        for i in range(rng.randint(1, 4))
    ]
    ipos = []
    for n in range(rng.randint(1, 12)):
        close_off = rng.randint(0, 40)
        allot_off = close_off + rng.randint(0, 6)
        ipos.append(
            make_ipo(
                f"Co {n}",
                gmp=str(rng.choice([0, 3.5, 12, 27.25, 55, 90])),
                close=close_off + 1,
                allot=allot_off + 1,
                price=str(rng.choice([72, 99, 240, 435, 1245])),
                lot=rng.choice([12, 34, 62, 150, 1600]),
                ipo_id=f"co-{n}",
                allot_prob=str(rng.choice([0, 0.1, 0.5, 1.0])),
            )
        )
    return pans, ipos, start


@pytest.mark.parametrize("policy", list(SchedulingPolicy))
@pytest.mark.parametrize("assumption", list(AllotmentAssumption))
def test_sweep_never_overdraws_any_pan(policy, assumption):
    """The load-bearing invariant, across 150 generated calendars per configuration."""
    for seed in range(150):
        rng = random.Random(seed)
        pans, ipos, start = random_scenario(rng)
        result = IPOJobScheduler(
            pans, policy=policy, assumption=assumption
        ).execute_schedule(ipos, start)

        try:
            assert_no_overdraft(result, pans, ipos, assumption)
            assert_sebi_one_lot_per_pan(result, pans)
        except AssertionError as exc:  # pragma: no cover - only on regression
            raise AssertionError(
                f"seed={seed} policy={policy.value} assumption={assumption.value}: {exc}"
            ) from exc


def test_sweep_every_ipo_is_either_scheduled_or_explained():
    """Nothing may vanish. A dropped IPO must come with a reason."""
    for seed in range(150):
        rng = random.Random(seed)
        pans, ipos, start = random_scenario(rng)
        result = IPOJobScheduler(pans).execute_schedule(ipos, start)

        accounted = {e.ipo_id for e in result.events} | {s.ipo_id for s in result.skipped}
        assert accounted == {i.id for i in ipos}, f"seed={seed}: IPOs unaccounted for"
        assert all(s.reason for s in result.skipped), f"seed={seed}: skip without a reason"


def test_sweep_no_ipo_is_both_scheduled_and_skipped():
    for seed in range(100):
        rng = random.Random(seed)
        pans, ipos, start = random_scenario(rng)
        result = IPOJobScheduler(pans).execute_schedule(ipos, start)

        scheduled = {e.ipo_id for e in result.events}
        skipped = {s.ipo_id for s in result.skipped}
        assert not (scheduled & skipped), f"seed={seed}: {scheduled & skipped}"


def test_sweep_reported_balance_matches_reconstructed_balance():
    """``remaining_liquid_balance`` must be derivable from the published blocks.

    Guards against the reported cash reserve drifting from the engine's own model,
    which is the number a user would actually act on.
    """
    total_capital = lambda pans: sum((p.available_balance for p in pans), ZERO)  # noqa: E731

    for seed in range(100):
        rng = random.Random(seed)
        pans, ipos, start = random_scenario(rng)
        result = IPOJobScheduler(pans).execute_schedule(ipos, start)
        blocks = reconstruct_blocks(result, ipos)

        for event in result.events:
            probe = date.fromisoformat(event.action_date)
            frozen = sum((amt for _, amt, s, e in blocks if s <= probe < e), ZERO)
            expected = total_capital(pans) - frozen
            assert Decimal(str(event.remaining_liquid_balance)) == expected, (
                f"seed={seed} {event.ipo_name} on {probe}: reported "
                f"{event.remaining_liquid_balance}, reconstructed {expected}"
            )


def test_sweep_is_deterministic():
    """Same inputs, byte-identical schedule. Required for a reviewable plan."""
    for seed in range(40):
        rng = random.Random(seed)
        pans, ipos, start = random_scenario(rng)
        first = IPOJobScheduler(pans).execute_schedule(ipos, start)
        second = IPOJobScheduler(pans).execute_schedule(ipos, start)
        assert first.model_dump() == second.model_dump(), f"seed={seed}"


def test_sweep_pan_order_does_not_change_the_outcome_totals():
    """Shuffling the PAN list may reassign lots but must not change how many."""
    for seed in range(60):
        rng = random.Random(seed)
        pans, ipos, start = random_scenario(rng)
        shuffled = list(pans)
        random.Random(seed + 1).shuffle(shuffled)

        a = IPOJobScheduler(pans).execute_schedule(ipos, start)
        b = IPOJobScheduler(shuffled).execute_schedule(ipos, start)

        assert {(e.ipo_id, e.lots_applied) for e in a.events} == {
            (e.ipo_id, e.lots_applied) for e in b.events
        }, f"seed={seed}"


def test_sweep_value_first_dominates_greedy_on_aggregate():
    """VALUE_FIRST must beat the greedy baseline overall, and rarely lose.

    Note the assertion is *aggregate*, not universal. Ranking by GMP% is a greedy
    heuristic over what is really a resource-constrained scheduling problem, so it
    is not profit-optimal and cannot be: see ``test_rule3_is_a_heuristic_not_an_optimum``
    for a concrete counterexample. What must hold is that it wins far more than it
    loses and never loses much.
    """
    wins = losses = 0
    good_total = base_total = 0.0
    worst_loss = 0.0

    for seed in range(200):
        rng = random.Random(seed)
        pans, ipos, start = random_scenario(rng)
        good = IPOJobScheduler(pans, policy=SchedulingPolicy.VALUE_FIRST).execute_schedule(
            ipos, start
        )
        base = IPOJobScheduler(pans, policy=SchedulingPolicy.JIT_GREEDY).execute_schedule(
            ipos, start
        )
        good_total += good.total_expected_profit
        base_total += base.total_expected_profit

        if good.total_expected_profit > base.total_expected_profit:
            wins += 1
        elif good.total_expected_profit < base.total_expected_profit:
            losses += 1
            shortfall = base.total_expected_profit - good.total_expected_profit
            worst_loss = max(worst_loss, shortfall / base.total_expected_profit)

    assert good_total > base_total, "VALUE_FIRST must earn more in total"
    assert wins > losses * 10, f"only {wins} wins against {losses} losses"
    assert worst_loss < 0.02, f"worst-case shortfall {worst_loss:.2%} is too large"


def test_rule3_is_a_heuristic_not_an_optimum():
    """Pins the known limit of Rule 3 so nobody mistakes it for an optimiser.

    Two issues tie on GMP%, so Rule 3 breaks the tie on the earlier allotment date.
    But the issue with the *later* allotment has a larger lot cost, and therefore
    earns more absolute rupees per lot at the same percentage. Rule 3 sends the
    scarce lots to the wrong one.
    """
    pans = [make_pan("SELF", "15000")]
    # Same GMP. `cheap` wins the Rule 3 tiebreak (earlier allotment) but earns less.
    cheap = make_ipo("Cheap", "27.25", close=3, allot=8, price="1479", lot=10, ipo_id="cheap")
    rich = make_ipo("Rich", "27.25", close=4, allot=12, price="1494", lot=10, ipo_id="rich")

    result = IPOJobScheduler(pans).execute_schedule([cheap, rich], D(1))

    assert [e.ipo_id for e in result.events] == ["cheap"], "Rule 3 prefers earlier allotment"
    assert rich.expected_profit_per_lot > cheap.expected_profit_per_lot, (
        "yet the skipped issue would have earned more per lot"
    )
    # Documented in docs/DEVIATIONS.md D13. A true optimum needs a solver over the
    # capital-lockup profile, not a greedy sort.


class TestPerPanSilos:
    """D4: ASBA freezes money in the applicant's own account, not a shared pot."""

    def test_a_rich_pan_cannot_fund_a_poor_pans_bid(self):
        pans = [make_pan("RICH", "1000000"), make_pan("POOR", "100")]
        ipo = make_ipo("Costly Co", "50", close=3, allot=8, price="100", lot=150)

        result = IPOJobScheduler(pans).execute_schedule([ipo], D(1))

        assert result.events[0].pans_used == ["RICH"]
        assert "POOR" not in result.events[0].pans_used
        assert result.events[0].lots_applied == 1

    def test_zero_balance_pan_is_never_allocated(self):
        pans = [make_pan("SELF", "150000"), make_pan("EMPTY", "0")]
        ipo = make_ipo("Any Co", "50", close=3, allot=8)

        result = IPOJobScheduler(pans).execute_schedule([ipo], D(1))

        assert "EMPTY" not in result.events[0].pans_used


class TestPeakCapitalReporting:
    def test_peak_never_exceeds_total_capital(self):
        for seed in range(60):
            rng = random.Random(seed)
            pans, ipos, start = random_scenario(rng)
            result = IPOJobScheduler(pans).execute_schedule(ipos, start)
            total = float(sum((p.available_balance for p in pans), ZERO))
            assert result.peak_capital_deployed <= total, f"seed={seed}"


class TestEmptyAndDegenerateInputs:
    def test_no_ipos_yields_an_empty_schedule(self):
        result = IPOJobScheduler([make_pan("SELF", "150000")]).execute_schedule([], D(1))
        assert result.events == []
        assert result.skipped == []
        assert result.total_expected_profit == 0.0

    def test_zero_capital_schedules_nothing_but_explains_why(self):
        pans = [make_pan("SELF", "0")]
        ipo = make_ipo("Any Co", "50", close=3, allot=8)

        result = IPOJobScheduler(pans).execute_schedule([ipo], D(1))

        assert result.events == []
        assert len(result.skipped) == 1
        assert "capital" in result.skipped[0].reason

    def test_no_pans_is_rejected_at_construction(self):
        with pytest.raises(ValueError, match="at least one PAN"):
            IPOJobScheduler([])

    def test_duplicate_pan_ids_are_rejected(self):
        with pytest.raises(ValueError, match="duplicate PAN"):
            IPOJobScheduler([make_pan("SELF", "100"), make_pan("SELF", "200")])

    def test_same_day_close_and_allotment_still_freezes_one_day(self):
        pans = [make_pan("SELF", "15000")]
        ipo = make_ipo("SameDay", "50", close=5, allot=5)

        result = IPOJobScheduler(pans).execute_schedule([ipo], D(1))

        assert result.events[0].unblock_date == (D(5) + timedelta(days=1)).isoformat()


class TestSeededProvider:
    async def test_provider_supplies_a_schedulable_universe(self):
        provider = SeededProvider(anchor=date(2026, 3, 2))
        ipos = await provider.fetch_open_ipos(date(2026, 3, 1))

        assert len(ipos) == 8
        pans = [make_pan("SELF", "200000"), make_pan("MOTHER", "200000")]
        result: ScheduleResult = IPOJobScheduler(pans).execute_schedule(ipos, date(2026, 3, 1))

        assert result.events, "the seeded calendar should produce a usable schedule"
        assert_no_overdraft(result, pans, ipos)
        assert_sebi_one_lot_per_pan(result, pans)

    async def test_provider_filters_by_as_of_date(self):
        provider = SeededProvider(anchor=date(2026, 3, 2))
        later = await provider.fetch_open_ipos(date(2026, 3, 10))
        assert all(i.close_date >= date(2026, 3, 10) for i in later)

    async def test_unknown_gmp_returns_none(self):
        assert await SeededProvider().fetch_gmp("Nonexistent Ltd") is None
