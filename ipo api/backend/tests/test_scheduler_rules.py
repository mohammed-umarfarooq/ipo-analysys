"""The four hard domain rules from the blueprint, tested directly."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.domain import AllotmentAssumption, PanAccount, SchedulingPolicy
from app.scheduler import IPOJobScheduler
from tests.conftest import (
    D,
    assert_no_overdraft,
    assert_sebi_one_lot_per_pan,
    make_ipo,
    make_pan,
)


class TestRule1SebiLottery:
    """Allocation is capped at one lot per unique PAN."""

    def test_lots_never_exceed_pan_count_even_with_surplus_cash(self):
        # Each PAN could afford 10 lots; the lottery rule means only 1 is useful.
        pans = [make_pan(f"PAN{i}", "150000") for i in range(3)]
        ipo = make_ipo("Surplus Co", "40", close=3, allot=8, price="100", lot=150)

        result = IPOJobScheduler(pans).execute_schedule([ipo], D(1))

        assert len(result.events) == 1
        assert result.events[0].lots_applied == 3, "one lot per PAN, no more"
        assert set(result.events[0].pans_used) == {"PAN0", "PAN1", "PAN2"}
        assert_sebi_one_lot_per_pan(result, pans)

    def test_single_pan_gets_one_lot_only(self):
        pans = [make_pan("SELF", "1000000")]
        ipo = make_ipo("Solo Co", "40", close=3, allot=8)

        result = IPOJobScheduler(pans).execute_schedule([ipo], D(1))

        assert result.events[0].lots_applied == 1

    def test_a_pan_may_hold_concurrent_bids_in_different_ipos(self):
        # The one-lot cap is per IPO, not per PAN globally.
        pans = [make_pan("SELF", "40000")]
        ipos = [
            make_ipo("Alpha", "50", close=2, allot=9),
            make_ipo("Beta", "45", close=3, allot=10),
        ]
        result = IPOJobScheduler(pans).execute_schedule(ipos, D(1))

        assert len(result.events) == 2, "both bids fit inside one PAN's balance"
        assert_no_overdraft(result, pans, ipos)


class TestRule2AsbaFreezeAndT1:
    """Blocked capital returns on allotment_date + 1, not before."""

    def test_unblock_date_is_allotment_plus_one_day(self):
        pans = [make_pan("SELF", "20000")]
        ipo = make_ipo("Freeze Co", "30", close=3, allot=9)

        result = IPOJobScheduler(pans).execute_schedule([ipo], D(1))
        event = result.events[0]

        assert event.allotment_date == D(9).isoformat()
        assert event.unblock_date == (D(9) + timedelta(days=1)).isoformat()

    def test_capital_is_reusable_exactly_on_the_unblock_date(self):
        """The boundary case: a second IPO closing on the unblock date must fund."""
        pans = [make_pan("SELF", "15000")]  # enough for exactly one lot at a time
        first = make_ipo("First", "50", close=2, allot=6, ipo_id="first")
        # First unblocks on the 7th. A bid closing on the 7th should succeed.
        second = make_ipo("Second", "49", close=7, allot=12, ipo_id="second")

        result = IPOJobScheduler(pans).execute_schedule([first, second], D(1))

        assert {e.ipo_id for e in result.events} == {"first", "second"}
        assert_no_overdraft(result, pans, [first, second])

    def test_capital_is_not_reusable_one_day_before_unblock(self):
        """Off-by-one guard: closing on the allotment date is still too early."""
        pans = [make_pan("SELF", "15000")]
        first = make_ipo("First", "50", close=2, allot=6, ipo_id="first")
        # First is still frozen on the 6th (unblocks the 7th).
        second = make_ipo("Second", "49", close=6, allot=12, ipo_id="second")

        result = IPOJobScheduler(pans).execute_schedule([first, second], D(1))

        placed = {e.ipo_id for e in result.events}
        assert placed == {"first"}, "the second bid must not be funded from frozen capital"
        assert any(s.ipo_id == "second" for s in result.skipped)


class TestRule3Priority:
    """GMP% descending, then allotment date ascending."""

    def test_higher_gmp_wins_scarce_capital(self):
        pans = [make_pan("SELF", "15000")]  # room for one lot only
        junk = make_ipo("Junk Co", "5", close=2, allot=10, ipo_id="junk")
        star = make_ipo("Star Co", "80", close=4, allot=12, ipo_id="star")

        result = IPOJobScheduler(pans).execute_schedule([junk, star], D(1))

        assert [e.ipo_id for e in result.events] == ["star"]
        assert result.skipped[0].ipo_id == "junk"

    def test_equal_gmp_breaks_toward_earlier_allotment(self):
        """Earlier allotment frees the capital sooner, so it ranks first."""
        pans = [make_pan("SELF", "15000")]
        slow = make_ipo("Slow Co", "30", close=2, allot=20, ipo_id="slow")
        fast = make_ipo("Fast Co", "30", close=2, allot=8, ipo_id="fast")

        result = IPOJobScheduler(pans).execute_schedule([slow, fast], D(1))

        assert [e.ipo_id for e in result.events] == ["fast"]

    def test_min_gmp_threshold_excludes_weak_issues(self):
        pans = [make_pan("SELF", "150000")]
        strong = make_ipo("Strong", "25", close=2, allot=8, ipo_id="strong")
        weak = make_ipo("Weak", "4", close=3, allot=9, ipo_id="weak")

        result = IPOJobScheduler(pans, min_gmp=Decimal("10")).execute_schedule(
            [strong, weak], D(1)
        )

        assert [e.ipo_id for e in result.events] == ["strong"]
        weak_skip = next(s for s in result.skipped if s.ipo_id == "weak")
        assert "below" in weak_skip.reason


class TestRule4JustInTimeBidding:
    """Bids land on the close date, never earlier."""

    def test_bid_date_equals_close_date(self):
        pans = [make_pan("SELF", "150000")]
        ipos = [
            make_ipo("Early", "30", close=2, allot=8),
            make_ipo("Late", "25", close=9, allot=15),
        ]

        result = IPOJobScheduler(pans).execute_schedule(ipos, D(1))

        for event in result.events:
            ipo = next(i for i in ipos if i.id == event.ipo_id)
            assert event.action_date == ipo.close_date.isoformat(), (
                "bidding before the close date freezes capital for longer at no benefit"
            )

    def test_already_closed_ipos_are_reported_not_silently_dropped(self):
        pans = [make_pan("SELF", "150000")]
        stale = make_ipo("Stale Co", "60", close=1, allot=5, ipo_id="stale")
        live = make_ipo("Live Co", "20", close=8, allot=14, ipo_id="live")

        result = IPOJobScheduler(pans).execute_schedule([stale, live], D(4))

        assert [e.ipo_id for e in result.events] == ["live"]
        stale_skip = next(s for s in result.skipped if s.ipo_id == "stale")
        assert "before the" in stale_skip.reason


class TestAllotmentAssumption:
    """Capital lost to a successful allotment does not come back (D5)."""

    def test_none_allotted_returns_all_capital(self):
        pans = [make_pan("SELF", "15000")]
        first = make_ipo("First", "50", close=2, allot=6, ipo_id="first", allot_prob="1.0")
        second = make_ipo("Second", "49", close=8, allot=14, ipo_id="second")

        result = IPOJobScheduler(
            pans, assumption=AllotmentAssumption.NONE_ALLOTTED
        ).execute_schedule([first, second], D(1))

        assert {e.ipo_id for e in result.events} == {"first", "second"}

    def test_expected_assumption_withholds_allotted_capital(self):
        """With certain allotment the money is spent, so the later bid cannot fund."""
        pans = [make_pan("SELF", "15000")]
        first = make_ipo("First", "50", close=2, allot=6, ipo_id="first", allot_prob="1.0")
        second = make_ipo("Second", "49", close=8, allot=14, ipo_id="second")

        result = IPOJobScheduler(
            pans, assumption=AllotmentAssumption.EXPECTED
        ).execute_schedule([first, second], D(1))

        assert [e.ipo_id for e in result.events] == ["first"]
        assert any(s.ipo_id == "second" for s in result.skipped)

    def test_expected_assumption_never_overdraws(self):
        pans = [make_pan("SELF", "60000"), make_pan("MOTHER", "60000")]
        ipos = [
            make_ipo("A", "50", close=2, allot=6, ipo_id="a", allot_prob="0.5"),
            make_ipo("B", "40", close=7, allot=12, ipo_id="b", allot_prob="0.5"),
            make_ipo("C", "30", close=13, allot=18, ipo_id="c", allot_prob="0.5"),
        ]

        result = IPOJobScheduler(
            pans, assumption=AllotmentAssumption.EXPECTED
        ).execute_schedule(ipos, D(1))

        assert_no_overdraft(result, pans, ipos, AllotmentAssumption.EXPECTED)


class TestPolicyComparison:
    """VALUE_FIRST should never earn less expected profit than the greedy baseline."""

    def test_value_first_beats_greedy_on_the_starvation_case(self):
        pans = [make_pan("SELF", "15000")]
        junk = make_ipo("Junk Co", "5", close=2, allot=10, ipo_id="junk")
        star = make_ipo("Star Co", "80", close=4, allot=12, ipo_id="star")

        good = IPOJobScheduler(pans, policy=SchedulingPolicy.VALUE_FIRST).execute_schedule(
            [junk, star], D(1)
        )
        baseline = IPOJobScheduler(pans, policy=SchedulingPolicy.JIT_GREEDY).execute_schedule(
            [junk, star], D(1)
        )

        assert [e.ipo_id for e in baseline.events] == ["junk"], "baseline reproduces the flaw"
        assert [e.ipo_id for e in good.events] == ["star"]
        assert good.total_expected_profit > baseline.total_expected_profit


class TestSharedPoolHelper:
    def test_shared_pool_splits_evenly_and_does_not_double_spend(self):
        scheduler = IPOJobScheduler.from_shared_pool("30000", ["SELF", "MOTHER"])

        assert scheduler.initial_capital == Decimal("30000.00")
        pans: list[PanAccount] = scheduler.pans
        assert all(p.available_balance == Decimal("15000.00") for p in pans)

        ipo = make_ipo("Pool Co", "30", close=2, allot=8, price="100", lot=150)
        result = scheduler.execute_schedule([ipo], D(1))

        assert result.events[0].lots_applied == 2
        assert_no_overdraft(result, pans, [ipo])
