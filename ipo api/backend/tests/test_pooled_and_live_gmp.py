"""Pooled capital, estimated dates, live GMP, and the day-by-day cashflow matrix.

Four behaviours a user would notice, tested at the level they would notice them:

* one fund funds bids under any PAN, and recycles when ASBA releases it;
* the cashflow matrix agrees with the schedule printed above it;
* a live premium fills a blank issue and refuses to touch one you typed;
* the aggregator being down is a 502 with advice, not a 500.

No network. The GMP fixture is a trimmed copy of the real ipowatch.in markup, so the
parser is checked against the shape it actually has to survive.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dates import estimated_dates, is_working_day
from app.domain import CapitalMode, SchedulingPolicy
from app.main import app, get_session
from app.models import Base
from app.providers import gmp_live
from app.providers.gmp_live import normalise_name, parse_gmp_table
from app.repository import ensure_seeded
from app.scheduler import IPOJobScheduler
from tests.conftest import D, assert_sebi_one_lot_per_pan, make_ipo, make_pan


# ─────────────────────────────────────────────────────────── pooled capital

ONE_LOT = Decimal("15000")  # price 100 x lot 150


def pooled(pans, **kw) -> IPOJobScheduler:
    return IPOJobScheduler(pans, capital_mode=CapitalMode.POOLED, **kw)


class TestPooledFund:
    """Requirement 1: "the fund is 1"."""

    def test_one_pan_s_idle_cash_funds_another_pan_s_bid(self):
        """The whole point of pooling, in one assertion.

        SELF holds everything and MOTHER holds nothing. Per-PAN, MOTHER can never bid,
        so a two-PAN household gets one lot. Pooled, the war-chest covers both.
        """
        pans = [make_pan("SELF", "30000"), make_pan("MOTHER", "0")]
        ipos = [make_ipo("Alpha", "20", close=10, allot=12)]

        ring_fenced = IPOJobScheduler(pans).execute_schedule(ipos, D(1))
        assert ring_fenced.events[0].pans_used == ["SELF"]

        shared = pooled(pans).execute_schedule(ipos, D(1))
        assert sorted(shared.events[0].pans_used) == ["MOTHER", "SELF"]
        assert shared.events[0].blocked_amount == float(ONE_LOT * 2)
        assert_sebi_one_lot_per_pan(shared, pans)

    def test_the_pool_is_a_ceiling_not_a_suggestion(self):
        """Two lots fit in ₹30,000; a third does not, and is refused with a reason."""
        pans = [make_pan("A", "15000"), make_pan("B", "15000"), make_pan("C", "0")]
        result = pooled(pans).execute_schedule([make_ipo("Alpha", "20", close=10, allot=12)], D(1))

        assert result.events[0].lots_applied == 2
        assert result.peak_capital_deployed == float(ONE_LOT * 2)

    def test_capital_recycles_when_asba_releases_it(self):
        """One fund, two issues, no overlap — the same money pays for both.

        Alpha unblocks on day 13 and Beta closes on day 20, so a pool that only covers
        one bid at a time still funds two bids in sequence. That recycling is the reason
        a small war-chest can chase a whole calendar.
        """
        pans = [make_pan("SELF", "15000")]
        ipos = [
            make_ipo("Alpha", "30", close=10, allot=12),
            make_ipo("Beta", "20", close=20, allot=22),
        ]
        result = pooled(pans).execute_schedule(ipos, D(1))

        assert [e.ipo_name for e in result.events] == ["Alpha", "Beta"]
        # Never more than one lot frozen at once, so the fund was reused, not doubled.
        assert result.peak_capital_deployed == float(ONE_LOT)

    def test_overlapping_issues_cannot_both_draw_the_same_rupees(self):
        """Beta closes while Alpha is still frozen, so the pool cannot stretch."""
        pans = [make_pan("SELF", "15000")]
        ipos = [
            make_ipo("Alpha", "30", close=10, allot=12),
            make_ipo("Beta", "20", close=11, allot=13),
        ]
        result = pooled(pans).execute_schedule(ipos, D(1))

        assert [e.ipo_name for e in result.events] == ["Alpha"]
        assert [s.ipo_name for s in result.skipped] == ["Beta"]
        assert "pooled fund" in result.skipped[0].reason

    def test_the_result_says_which_mode_produced_it(self):
        pans = [make_pan("SELF", "15000")]
        ipos = [make_ipo("Alpha", "30", close=10, allot=12)]
        assert pooled(pans).execute_schedule(ipos, D(1)).capital_mode == "pooled"
        assert IPOJobScheduler(pans).execute_schedule(ipos, D(1)).capital_mode == "per_pan"


# ────────────────────────────────────────────────────── cashflow matrix


class TestCashflowMatrix:
    """Requirement: the day-by-day war-chest table, not just a bid list."""

    @staticmethod
    def _plan():
        pans = [make_pan("SELF", "15000")]
        ipos = [
            make_ipo("Alpha", "30", close=10, allot=12),
            make_ipo("Beta", "20", close=20, allot=22),
        ]
        return pooled(pans).execute_schedule(ipos, D(1)), ipos

    def test_a_bid_drains_the_balance_and_the_unblock_restores_it(self):
        result, _ = self._plan()
        by_date = {r.date: r for r in result.daily_timeline}

        bid = by_date[D(10).isoformat()]
        assert bid.blocked_today == float(ONE_LOT)
        assert bid.total_locked == float(ONE_LOT)
        assert bid.spendable_balance == 0.0
        assert bid.actions == ["Alpha ×1 lot"]

        release = by_date[D(13).isoformat()]  # allotment day 12, T+1
        assert release.unblocked_today == float(ONE_LOT)
        assert release.total_locked == 0.0
        assert release.spendable_balance == float(ONE_LOT)

    def test_allotment_and_listing_days_are_named(self):
        result, _ = self._plan()
        by_date = {r.date: r for r in result.daily_timeline}
        assert by_date[D(12).isoformat()].allotments_finalized == ["Alpha"]
        # No listing date on these fixtures, so nothing is claimed about listings.
        assert all(not r.listings for r in result.daily_timeline)

    def test_every_row_reconciles_with_the_one_before_it(self):
        """total_locked must be explained entirely by the day's blocks and releases.

        This is what makes the table trustworthy: if the matrix and the Gantt ever
        disagreed, one of them would be lying about where the money is.
        """
        result, _ = self._plan()
        running = 0.0
        for row in result.daily_timeline:
            running += row.blocked_today - row.unblocked_today
            assert row.total_locked == pytest.approx(running), row.date

    def test_quiet_days_get_no_row(self):
        result, _ = self._plan()
        dates = [r.date for r in result.daily_timeline]
        assert dates == sorted(dates)
        # Bid, allotment, release, then the same three for Beta — not 20 idle days.
        assert len(dates) == 6

    def test_nothing_placed_means_an_empty_matrix_not_a_fake_one(self):
        result = pooled([make_pan("SELF", "0")]).execute_schedule(
            [make_ipo("Alpha", "30", close=10, allot=12)], D(1)
        )
        assert result.events == []
        assert result.daily_timeline == []


# ───────────────────────────────────────────────────────────── live GMP

#: Trimmed from the real page: the live table, a header row, and a stale duplicate
#: further down (which must not win over the live figure above it).
GMP_HTML = """
<table><tbody>
<tr><th>IPO Name</th><th>IPO GMP*</th><th>Trend</th><th>Price Band</th></tr>
<tr><td><a href="/x">Augmont Enterprises</a></td><td>&#8377;310</td><td>&#128994;</td><td>&#8377;786</td></tr>
<tr><td>Tempsens Instruments</td><td>&#8377;290</td><td>&#128308;</td><td>&#8377;300</td></tr>
<tr><td>Shankesh Jewellers</td><td>&#8377;0</td><td>&#128993;</td><td>&#8377;93</td></tr>
<tr><td>Rays of Belief</td><td>&#8377;-</td><td>&#128993;</td><td>&#8377;-</td></tr>
</tbody></table>
<table><tbody>
<tr><td>Augmont Enterprises Limited</td><td>&#8377;5</td><td>&#128308;</td><td>&#8377;786</td></tr>
</tbody></table>
"""


class TestGmpParser:
    def test_it_reads_the_real_table_shape(self):
        premiums = parse_gmp_table(GMP_HTML)
        assert premiums[normalise_name("Augmont Enterprises Limited")] == Decimal("310")
        assert premiums[normalise_name("Tempsens Instruments (India) Limited")] == Decimal("290")

    def test_zero_is_a_quote_but_a_dash_is_not(self):
        """"No premium today" and "not quoted yet" are different facts.

        A dash written as zero would look like a real ₹0 reading and, worse, would let a
        refresh overwrite a premium with a number nobody quoted.
        """
        premiums = parse_gmp_table(GMP_HTML)
        assert premiums[normalise_name("Shankesh Jewellers")] == Decimal("0")
        assert normalise_name("Rays of Belief") not in premiums

    def test_the_live_table_wins_over_a_stale_repeat(self):
        premiums = parse_gmp_table(GMP_HTML)
        assert premiums[normalise_name("Augmont Enterprises")] == Decimal("310")

    def test_headers_and_junk_are_not_quotes(self):
        assert "iponame" not in parse_gmp_table(GMP_HTML)
        assert parse_gmp_table("<p>nothing here</p>") == {}

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Augmont Enterprises Limited", "Augmont Enterprises"),
            ("Hy-Tech Recycling India Limited", "Hy Tech Recycling"),
            ("Gaja Alternative Asset Management", "Gaja Alternative Asset Management Ltd."),
        ],
    )
    def test_the_two_sites_name_one_issue_two_ways(self, a, b):
        assert normalise_name(a) == normalise_name(b)


# ──────────────────────────────────────────────────── estimated T+3 dates


class TestWorkingDayEstimates:
    """Requirement 3/4: a close date is enough to plan from."""

    def test_a_friday_close_allots_on_monday_and_lists_on_wednesday(self):
        """The weekend is not a working day for the registrar either.

        This is the case a naive ``close + 1 day`` gets wrong, and the user's own example
        has it: an issue closing Friday is allotted the following Monday.
        """
        friday = date(2026, 8, 21)
        assert friday.weekday() == 4
        assert estimated_dates(friday) == (date(2026, 8, 24), date(2026, 8, 26))

    def test_a_midweek_close_needs_no_skipping(self):
        assert estimated_dates(date(2026, 8, 25)) == (date(2026, 8, 26), date(2026, 8, 28))

    def test_the_estimate_never_lands_on_a_weekend(self):
        for offset in range(7):
            allotment, listing = estimated_dates(date(2026, 8, 17) + timedelta(days=offset))
            assert is_working_day(allotment) and is_working_day(listing)


# ──────────────────────────────────────────────────── live GMP endpoint


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with maker() as session:
            yield session

    async with maker() as session:
        await ensure_seeded(session)

    app.dependency_overrides[get_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


TOMORROW = date.today() + timedelta(days=1)


async def add_issue(client, name: str, *, latest_gmp: str = "0") -> dict:
    return (
        await client.post(
            "/api/ipos",
            json={
                "name": name,
                "symbol": name.split()[0].upper(),
                "min_price": "780",
                "max_price": "786",
                "lot_size": 19,
                "latest_gmp": latest_gmp,
                "open_date": TOMORROW.isoformat(),
                "close_date": (TOMORROW + timedelta(days=3)).isoformat(),
            },
        )
    ).json()


@pytest.fixture
def served_page(monkeypatch):
    """Serve GMP_HTML to the provider without touching the network."""

    def install(handler) -> None:
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def factory(**kwargs):
            kwargs.pop("transport", None)
            return original(transport=transport, **kwargs)

        monkeypatch.setattr(gmp_live.httpx, "AsyncClient", factory)

    return install


class TestRefreshGmpEndpoint:
    async def test_it_fills_a_blank_premium_and_says_it_is_unofficial(
        self, client, served_page
    ):
        served_page(lambda _: httpx.Response(200, text=GMP_HTML))
        await add_issue(client, "Augmont Enterprises Limited")

        body = (await client.post("/api/ipos/refresh-gmp")).json()
        assert body["updated"] == ["Augmont Enterprises Limited"]
        assert "unregulated" in body["disclaimer"]

        issue = (await client.get("/api/ipos")).json()[0]
        assert issue["latest_gmp"] == 310.0
        assert issue["gmp_source"] == "live"
        # Percent is derived from the stored cut-off, never read off the page.
        assert issue["gmp_percent"] == pytest.approx(39.44, abs=0.01)

    async def test_a_premium_you_typed_is_never_overwritten(self, client, served_page):
        served_page(lambda _: httpx.Response(200, text=GMP_HTML))
        await add_issue(client, "Augmont Enterprises Limited", latest_gmp="94.56")

        body = (await client.post("/api/ipos/refresh-gmp")).json()
        assert body["unchanged_because_edited"] == ["Augmont Enterprises Limited"]
        assert body["updated"] == []
        assert (await client.get("/api/ipos")).json()[0]["latest_gmp"] == 94.56

    async def test_an_issue_the_aggregator_never_heard_of_is_reported(
        self, client, served_page
    ):
        served_page(lambda _: httpx.Response(200, text=GMP_HTML))
        await add_issue(client, "Entirely Fictional Ventures Limited")

        body = (await client.post("/api/ipos/refresh-gmp")).json()
        assert body["unmatched"] == ["Entirely Fictional Ventures Limited"]
        # Three of the fixture's four rows carry a number; the "₹-" row is not a quote.
        assert body["quotes_seen"] == 3

    async def test_editing_a_live_premium_takes_ownership_of_it(self, client, served_page):
        served_page(lambda _: httpx.Response(200, text=GMP_HTML))
        created = await add_issue(client, "Augmont Enterprises Limited")
        await client.post("/api/ipos/refresh-gmp")

        patched = (
            await client.patch(f"/api/ipos/{created['id']}", json={"latest_gmp": "400"})
        ).json()
        assert patched["gmp_source"] == "user"
        # And a second refresh now leaves it alone.
        assert (await client.post("/api/ipos/refresh-gmp")).json()["updated"] == []

    @pytest.mark.parametrize(
        "handler,why",
        [
            (lambda _: httpx.Response(503, text="down"), "upstream error"),
            (lambda _: httpx.Response(200, text="<p>redesigned</p>"), "markup changed"),
        ],
    )
    async def test_a_broken_aggregator_is_a_502_that_tells_you_what_to_do(
        self, client, served_page, handler, why
    ):
        """Manual entry is the fallback, and the error message says so."""
        served_page(handler)
        response = await client.post("/api/ipos/refresh-gmp")
        assert response.status_code == 502, why
        assert "hand" in response.json()["detail"]

    async def test_a_dead_network_is_also_a_502(self, client, served_page):
        def boom(_):
            raise httpx.ConnectError("no route to host")

        served_page(boom)
        response = await client.post("/api/ipos/refresh-gmp")
        assert response.status_code == 502
        assert "could not reach" in response.json()["detail"]

class TestAllotmentTickBox:
    """"Add a check box for if the IPO is allotted or not allotted."""

    @staticmethod
    async def _commit_one(client) -> dict:
        await client.post(
            "/api/pans",
            json={
                "holder_name": "Mohammed",
                "relation": "Self",
                "pan_number": "ABCDE1234F",
                "upi_id": "mohammed@okhdfc",
                "opening_balance": "40000",
            },
        )
        await add_issue(client, "Augmont Enterprises Limited", latest_gmp="94.56")
        await client.post("/api/schedule/commit", json={})
        return (await client.get("/api/portfolio/history")).json()[0]

    async def test_a_fresh_application_has_no_answer_yet(self, client):
        """Unknown is its own state — the registrar has not published anything."""
        row = await self._commit_one(client)
        assert row["allotted"] is None
        assert row["allotment_status"] == "APPLIED"

    async def test_ticking_and_unticking_both_stick(self, client):
        row = await self._commit_one(client)

        allotted = (
            await client.patch(f"/api/applications/{row['id']}", json={"allotted": True})
        ).json()
        assert allotted["allotted"] is True
        assert allotted["allotment_status"] == "ALLOTTED"

        rejected = (
            await client.patch(f"/api/applications/{row['id']}", json={"allotted": False})
        ).json()
        assert rejected["allotment_status"] == "NOT_ALLOTTED"

        # Ticking it by mistake is undoable, back to "not known yet".
        cleared = (
            await client.patch(f"/api/applications/{row['id']}", json={"allotted": None})
        ).json()
        assert cleared["allotted"] is None
        assert (await client.get("/api/portfolio/history")).json()[0]["allotment_status"] == "APPLIED"

    async def test_an_unknown_application_is_a_404_and_an_empty_body_a_422(self, client):
        row = await self._commit_one(client)
        assert (
            await client.patch("/api/applications/not-a-real-id", json={"allotted": True})
        ).status_code == 404
        # No default on the field, so "{}" cannot silently reset a recorded result.
        assert (await client.patch(f"/api/applications/{row['id']}", json={})).status_code == 422


# ───────────────────────────────────────────────── pooled mode, end to end


class TestPooledModeThroughTheApi:
    async def test_the_mode_persists_and_changes_the_plan(self, client, served_page):
        """The user's own example: one war-chest, two PANs, bids under both.

        One lot of this issue costs ₹14,934, so ₹40,000 in Mohammed's account covers two
        lots — but only if Aisha may draw on it. That is the whole difference between the
        two modes, and it is worth two lots here.
        """
        for holder, pan, balance in (
            ("Mohammed", "ABCDE1234F", "40000"),
            ("Aisha", "BCDEF2345G", "0"),
        ):
            await client.post(
                "/api/pans",
                json={
                    "holder_name": holder,
                    "relation": "Self",
                    "pan_number": pan,
                    "upi_id": f"{holder.lower()}@okhdfc",
                    "opening_balance": balance,
                },
            )
        await add_issue(client, "Augmont Enterprises Limited", latest_gmp="94.56")

        state = (await client.get("/api/user/state")).json()
        assert state["capital_mode"] == "pooled"  # the default the user asked for

        plan = (await client.post("/api/schedule", json={})).json()
        assert plan["capital_mode"] == "pooled"
        assert plan["events"][0]["lots_applied"] == 2  # Aisha bids on Mohammed's cash
        assert plan["daily_timeline"], "the matrix must come back with the plan"

        # Switching to ASBA-accurate planning drops the funded-by-someone-else lot.
        await client.patch("/api/user", json={"capital_mode": "per_pan"})
        strict = (await client.post("/api/schedule", json={})).json()
        assert strict["capital_mode"] == "per_pan"
        assert strict["events"][0]["lots_applied"] == 1

    async def test_an_unknown_mode_is_a_422(self, client):
        response = await client.patch("/api/user", json={"capital_mode": "whatever"})
        assert response.status_code == 422


def test_the_policy_comparison_still_works_pooled():
    """Pooled mode must not quietly disable the D1 finding the UI shows.

    The two closes overlap, so ₹15,000 funds exactly one of them: earliest-close-first
    takes the 12% issue and value-first takes the 40% one. If pooling had flattened the
    capacity test, both policies would place both issues and the comparison panel would
    always report a delta of zero.
    """
    pans = [make_pan("A", "15000"), make_pan("B", "0")]
    ipos = [
        make_ipo("Cheap", "12", close=10, allot=12),
        make_ipo("Rich", "40", close=11, allot=13),
    ]
    greedy = pooled(pans, policy=SchedulingPolicy.JIT_GREEDY).execute_schedule(ipos, D(1))
    value = pooled(pans, policy=SchedulingPolicy.VALUE_FIRST).execute_schedule(ipos, D(1))
    assert [e.ipo_name for e in greedy.events] == ["Cheap"]
    assert [e.ipo_name for e in value.events] == ["Rich"]
    assert value.total_expected_profit > greedy.total_expected_profit
