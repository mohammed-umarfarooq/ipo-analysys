"""One test per defect found in the original blueprint.

Several tests here run the blueprint's *original* algorithm, transcribed verbatim in
:func:`blueprint_original`, and assert that it is broken. That may look odd, but it
is what keeps the fixes honest: if someone later "simplifies" the engine back toward
the original shape, these tests say exactly which rule they broke and why.

Defect IDs match ``docs/DEVIATIONS.md``.
"""

from __future__ import annotations

import heapq
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain import IPOTask, floor_lots, money
from app.scheduler import IPOJobScheduler
from tests.conftest import D, assert_no_overdraft, make_ipo, make_pan


# --------------------------------------------------------------------- reference


class _LegacyTask:
    """Minimal stand-in for the blueprint's ``IPOTask`` (float money, single price)."""

    def __init__(self, name, price, lot_size, gmp_percent, close_date, allotment_date):
        self.name = name
        self.price = price
        self.lot_size = lot_size
        self.gmp_percent = gmp_percent
        self.close_date = close_date
        self.allotment_date = allotment_date

    @property
    def lot_cost(self) -> float:
        return self.price * self.lot_size


def blueprint_original(liquid_capital: float, pans: list[str], ipos, start_date):
    """The blueprint's ``IPOJobScheduler.execute_schedule``, transcribed as given."""
    pq = []
    for idx, ipo in enumerate(ipos):
        heapq.heappush(pq, (-ipo.gmp_percent, ipo.allotment_date, idx, ipo))

    current_capital = liquid_capital
    active_allocations: list[tuple[date, float, str, str]] = []
    events = []

    all_dates = {start_date}
    for ipo in ipos:
        all_dates.add(ipo.close_date)
        all_dates.add(ipo.allotment_date + timedelta(days=1))

    for cur_date in sorted(all_dates):
        if cur_date < start_date:
            continue
        unblocked, remaining = 0.0, []
        for unblock_d, amt, name, pan in active_allocations:
            if unblock_d <= cur_date:
                unblocked += amt
            else:
                remaining.append((unblock_d, amt, name, pan))
        active_allocations = remaining
        current_capital += unblocked

        for _, _, _, ipo in sorted(pq):
            if ipo.close_date == cur_date:
                max_lots = min(len(pans), int(current_capital // ipo.lot_cost))
                if max_lots > 0:
                    cost = max_lots * ipo.lot_cost
                    current_capital -= cost
                    unblock = ipo.allotment_date + timedelta(days=1)
                    for i in range(max_lots):
                        active_allocations.append((unblock, ipo.lot_cost, ipo.name, pans[i]))
                    events.append(
                        {
                            "ipo_name": ipo.name,
                            "lots_applied": max_lots,
                            "blocked_amount": cost,
                            "remaining_liquid_balance": round(current_capital, 2),
                        }
                    )
    return {"events": events}


# ------------------------------------------------------------------------- tests


class TestD1PriorityQueueDidNotPrioritise:
    """The GMP ranking was dead code, because bids are pinned to the close date."""

    JUNK = _LegacyTask("Junk Co", 100, 150, 5.0, date(2026, 3, 2), date(2026, 3, 10))
    STAR = _LegacyTask("Star Co", 100, 150, 80.0, date(2026, 3, 4), date(2026, 3, 12))

    def test_original_engine_starves_the_high_gmp_issue(self):
        out = blueprint_original(20000.0, ["PAN1"], [self.JUNK, self.STAR], date(2026, 3, 1))

        names = [e["ipo_name"] for e in out["events"]]
        assert names == ["Junk Co"], (
            "the 5% issue closing first consumed the capital the 80% issue needed"
        )
        assert "Star Co" not in names

    def test_fixed_engine_allocates_to_the_high_gmp_issue(self):
        pans = [make_pan("PAN1", "20000")]
        junk = make_ipo("Junk Co", "5", close=2, allot=10, ipo_id="junk")
        star = make_ipo("Star Co", "80", close=4, allot=12, ipo_id="star")

        result = IPOJobScheduler(pans).execute_schedule([junk, star], D(1))

        assert [e.ipo_id for e in result.events] == ["star"]

    def test_fix_does_not_double_commit_the_same_rupees(self):
        """Committing out of date order must still respect the freeze windows."""
        pans = [make_pan("PAN1", "20000")]
        junk = make_ipo("Junk Co", "5", close=2, allot=10, ipo_id="junk")
        star = make_ipo("Star Co", "80", close=4, allot=12, ipo_id="star")

        result = IPOJobScheduler(pans).execute_schedule([junk, star], D(1))

        # Junk (2nd–11th) and Star (4th–13th) overlap, so 30k would be needed at once.
        assert len(result.events) == 1
        assert_no_overdraft(result, pans, [junk, star])


class TestD2HeapTupleComparison:
    """The ``idx`` field was load-bearing; the model itself is not orderable."""

    def test_ipotask_is_not_orderable_so_it_must_never_enter_a_sort_key(self):
        a = make_ipo("A", "25", close=2, allot=9)
        b = make_ipo("B", "25", close=2, allot=9)

        with pytest.raises(TypeError):
            sorted([(-a.gmp_percent, a.allotment_date, a), (-b.gmp_percent, b.allotment_date, b)])

    def test_priority_key_is_fully_ordered_on_ties(self):
        """``priority_key`` ends in the name, so tied IPOs still sort deterministically."""
        a = make_ipo("Alpha", "25", close=2, allot=9)
        b = make_ipo("Beta", "25", close=2, allot=9)

        assert sorted([b.priority_key(), a.priority_key()]) == [
            a.priority_key(),
            b.priority_key(),
        ]
        assert all(isinstance(part, (Decimal, date, str)) for part in a.priority_key())


class TestD3FloatMoney:
    """Binary floats cannot represent NUMERIC(14,2), so lot counts drifted."""

    def test_float_floor_division_loses_a_lot(self):
        capital, lot_cost = 44999.09999999999, 14999.70
        assert int(capital // lot_cost) == 2, "float arithmetic silently drops a lot"

    def test_decimal_floor_division_is_exact(self):
        assert floor_lots(Decimal("44999.10"), Decimal("14999.70")) == 3

    def test_repeated_addition_stays_exact(self):
        total = sum((money(Decimal("14999.70")) for _ in range(3)), Decimal("0.00"))
        assert total == Decimal("44999.10")
        assert floor_lots(total, Decimal("14999.70")) == 3

    def test_engine_allocates_the_exact_affordable_lot_count(self):
        # Three PANs holding exactly one lot's worth of an awkward price each.
        # 1499.97 x 10 is the float-hostile 14,999.70 from the cases above.
        pans = [make_pan(f"PAN{i}", "14999.70") for i in range(3)]
        ipo = make_ipo("Awkward Co", "40", close=3, allot=8, price="1499.97", lot=10)

        assert ipo.lot_cost == Decimal("14999.70")
        result = IPOJobScheduler(pans).execute_schedule([ipo], D(1))

        assert result.events[0].lots_applied == 3, "no lot may vanish to rounding"

    def test_zero_lot_cost_is_rejected(self):
        with pytest.raises(ValueError, match="lot_cost must be positive"):
            floor_lots(Decimal("100"), Decimal("0"))


class TestD4SharedCapitalPool:
    """One ``total_bank_balance`` could fund bids from accounts that had no money."""

    def test_capital_cannot_cross_pan_boundaries(self):
        # A shared 45,000 pool would fund three lots. Split per-account, only one PAN can.
        pans = [make_pan("SELF", "45000"), make_pan("MOTHER", "0"), make_pan("FATHER", "0")]
        ipo = make_ipo("Costly Co", "50", close=3, allot=8, price="100", lot=150)

        result = IPOJobScheduler(pans).execute_schedule([ipo], D(1))

        assert result.events[0].lots_applied == 1, (
            "an ASBA mandate can only freeze money already in the holder's own account"
        )
        assert result.events[0].pans_used == ["SELF"]


class TestD5AllottedCapitalWasNeverDebited:
    def test_original_engine_returns_capital_even_when_certain_to_be_allotted(self):
        certain = _LegacyTask("Certain Co", 100, 150, 50.0, date(2026, 3, 2), date(2026, 3, 6))
        later = _LegacyTask("Later Co", 100, 150, 49.0, date(2026, 3, 8), date(2026, 3, 14))

        out = blueprint_original(15000.0, ["PAN1"], [certain, later], date(2026, 3, 1))

        assert [e["ipo_name"] for e in out["events"]] == ["Certain Co", "Later Co"], (
            "the original model recycled capital that allotment would have consumed"
        )


class TestD6MinGmpWasNeverApplied:
    def test_threshold_is_honoured_and_reported(self):
        pans = [make_pan("SELF", "150000")]
        weak = make_ipo("Weak Co", "4.5", close=3, allot=9, ipo_id="weak")

        result = IPOJobScheduler(pans, min_gmp=Decimal("10")).execute_schedule([weak], D(1))

        assert result.events == []
        assert result.skipped[0].reason.startswith("GMP 4.5% is below")


class TestD7BidPriceWasAmbiguous:
    """Retail applies at cut-off, i.e. the top of the band."""

    def test_lot_cost_uses_the_cutoff_not_the_floor_price(self):
        ipo = IPOTask(
            id="band",
            name="Band Co",
            min_price=Decimal("228"),
            max_price=Decimal("240"),
            lot_size=62,
            gmp_percent=Decimal("18"),
            close_date=D(3),
            allotment_date=D(9),
        )

        assert ipo.cutoff_price == Decimal("240.00")
        assert ipo.lot_cost == Decimal("14880.00")
        assert ipo.lot_cost != Decimal("228") * 62


class TestDomainValidation:
    """Guards that the original schema left to chance (D10)."""

    def test_allotment_before_close_is_rejected(self):
        with pytest.raises(ValidationError, match="allotment_date precedes close_date"):
            make_ipo("Backwards Co", "20", close=9, allot=3)

    def test_inverted_price_band_is_rejected(self):
        with pytest.raises(ValidationError, match="min_price exceeds max_price"):
            IPOTask(
                id="bad",
                name="Bad Band",
                min_price=Decimal("500"),
                max_price=Decimal("100"),
                lot_size=10,
                gmp_percent=Decimal("5"),
                close_date=D(3),
                allotment_date=D(9),
            )

    def test_non_positive_lot_size_is_rejected(self):
        with pytest.raises(ValidationError):
            make_ipo("Zero Lot", "20", close=3, allot=9, lot=0)

    def test_probability_outside_zero_to_one_is_rejected(self):
        with pytest.raises(ValidationError, match="between 0 and 1"):
            make_ipo("Impossible", "20", close=3, allot=9, allot_prob="1.5")

    def test_negative_pan_balance_is_rejected(self):
        with pytest.raises(ValidationError, match="cannot be negative"):
            make_pan("SELF", "-1")

    def test_unblock_date_is_derived_not_stored(self):
        ipo = make_ipo("Derived Co", "20", close=3, allot=9)
        assert ipo.unblock_date == ipo.allotment_date + timedelta(days=1)
