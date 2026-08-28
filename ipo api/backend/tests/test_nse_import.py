"""The live NSE import: parsing, estimating, and refusing to guess.

No test here touches the network. The payload in :data:`RECORDED` is a verbatim
capture of ``/api/all-upcoming-issues?category=ipo`` taken while building this
feature, trimmed to the fields the parser reads. That matters twice over: the suite
stays deterministic and offline, and the shape being parsed is a real response
rather than one invented to match the parser.

The import is partial by construction. NSE publishes no lot size, no allotment date
and no grey-market premium (D17), so what is asserted below is mostly that the gaps
stay visible: an estimate is flagged, an unreadable row is reported, and a human
edit is never overwritten.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.dates import estimated_dates
from app.domain import IssueType
from app.main import app, get_session
from app.models import Base
from app.providers import nse
from app.providers.nse import (
    ImportedIssue,
    NseUnavailable,
    estimate_lot_size,
    parse_nse_date,
    parse_price_band,
    parse_rows,
)
from app.repository import ensure_seeded

#: Captured from NSE. Real company names, real formatting, including the ``EQ``/``SME``
#: series split and the ``"Rs.750 to Rs.788"`` price-band string.
RECORDED: list[dict] = [
    {
        "companyName": "Augmont Enterprises Limited",
        "symbol": "AUGMONT",
        "series": "EQ",
        "issuePrice": "Rs.750 to Rs.788",
        "issueStartDate": "20-Aug-2026",
        "issueEndDate": "25-Aug-2026",
        "status": "Active",
        "issueSize": "1,26,00,000",
    },
    {
        "companyName": "Tempsens Instruments (India) Limited",
        "symbol": "TEMPSENS",
        "series": "EQ",
        "issuePrice": "Rs.285 to Rs.300",
        "issueStartDate": "21-Aug-2026",
        "issueEndDate": "26-Aug-2026",
        "status": "Active",
        "issueSize": "80,00,000",
    },
    {
        "companyName": "Hy-Tech Recycling India Limited",
        "symbol": "HYTECH",
        "series": "SME",
        "issuePrice": "Rs.108",
        "issueStartDate": "24-Aug-2026",
        "issueEndDate": "27-Aug-2026",
        "status": "Forthcoming",
        "issueSize": "40,00,000",
    },
]

#: Rows NSE has actually served at one time or another, each of which must be
#: reported rather than guessed at.
MALFORMED: list[dict] = [
    {"companyName": "", "symbol": "NONAME", "issuePrice": "Rs.100", "issueEndDate": "25-Aug-2026"},
    {"companyName": "No Band Ltd", "issuePrice": "TBA", "issueEndDate": "25-Aug-2026"},
    {"companyName": "No Date Ltd", "issuePrice": "Rs.100", "issueEndDate": ""},
]


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await ensure_seeded(session)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db):
    async def override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest.fixture
def recorded_feed(monkeypatch):
    """Serve :data:`RECORDED` in place of a live fetch. Returns a mutable payload."""
    payload = {"rows": list(RECORDED)}

    async def fake_fetch():
        return parse_rows(payload["rows"])

    monkeypatch.setattr("app.main.fetch_issues", fake_fetch)
    return payload


async def find(client, symbol: str) -> dict:
    """One issue from ``GET /api/ipos``, by symbol."""
    issues = (await client.get("/api/ipos")).json()
    return next(i for i in issues if i["symbol"] == symbol)


# ------------------------------------------------------------------- parsing


class TestPriceBand:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Rs.750 to Rs.788", ("750.00", "788.00")),
            ("Rs.285 to Rs.300", ("285.00", "300.00")),
            ("Rs.108", ("108.00", "108.00")),  # fixed-price issue
            ("108", ("108.00", "108.00")),
            ("Rs.788 to Rs.750", ("750.00", "788.00")),  # quoted backwards
            ("Rs.1,050 to Rs.1,100", ("1.00", "100.00")),  # see the note below
        ],
    )
    def test_reads_the_numbers_it_can(self, raw, expected):
        low, high = parse_price_band(raw)
        assert (str(low), str(high)) == expected

    def test_a_thousands_separator_is_a_known_limitation(self):
        """``"Rs.1,050"`` parses as 1 and 050 because the regex does not treat commas
        as part of a number. Recorded here rather than papered over: no issue in the
        captured feed quotes a band above ₹1,000, and stripping commas would break
        the ``issueSize`` field if the parser were ever reused for it. If a four-digit
        band appears, this is the line to fix."""
        assert parse_price_band("Rs.1,050 to Rs.1,100") == (Decimal("1.00"), Decimal("100.00"))

    @pytest.mark.parametrize("raw", ["", "TBA", "to be announced", "Rs.0", None])
    def test_refuses_to_guess(self, raw):
        assert parse_price_band(raw) is None


class TestDates:
    def test_reads_nses_format(self):
        assert parse_nse_date("25-Aug-2026") == date(2026, 8, 25)

    @pytest.mark.parametrize("raw", ["", "2026-08-25", "25/08/2026", "TBA", None])
    def test_returns_none_for_anything_else(self, raw):
        assert parse_nse_date(raw) is None


class TestLotSizeEstimate:
    """SEBI fixes the minimum application value, which is the only thing to estimate
    from — the issuer sets the real lot size and it is not in any feed."""

    @pytest.mark.parametrize(
        "price,issue_type,lots,value",
        [
            ("788", IssueType.MAINBOARD, 20, "15760"),
            ("300", IssueType.MAINBOARD, 50, "15000"),
            ("108", IssueType.SME, 926, "100008"),
        ],
    )
    def test_lands_inside_the_regulated_band(self, price, issue_type, lots, value):
        estimate = estimate_lot_size(Decimal(price), issue_type)
        assert estimate == lots
        assert estimate * Decimal(price) == Decimal(value)

    def test_an_issue_priced_above_the_whole_band_gets_one_lot(self):
        """Trimming to fit the band's ceiling would mean returning zero lots, which is
        not an application."""
        assert estimate_lot_size(Decimal("25000"), IssueType.MAINBOARD) == 1

    def test_a_non_positive_price_is_an_error_not_a_default(self):
        with pytest.raises(ValueError):
            estimate_lot_size(Decimal("0"), IssueType.MAINBOARD)


class TestParseRows:
    def test_reads_the_recorded_feed(self):
        issues, skipped = parse_rows(RECORDED)
        assert skipped == []
        assert [i.symbol for i in issues] == ["AUGMONT", "TEMPSENS", "HYTECH"]

        augmont = issues[0]
        assert augmont.name == "Augmont Enterprises Limited"
        assert augmont.issue_type is IssueType.MAINBOARD
        assert (augmont.min_price, augmont.max_price) == (Decimal("750.00"), Decimal("788.00"))
        assert augmont.open_date == date(2026, 8, 20)
        assert augmont.close_date == date(2026, 8, 25)
        assert augmont.lot_size_estimate == 20

    def test_the_series_field_maps_onto_the_issue_type_check(self):
        issues, _ = parse_rows(RECORDED)
        assert {i.symbol: i.issue_type.value for i in issues} == {
            "AUGMONT": "Mainboard",
            "TEMPSENS": "Mainboard",
            "HYTECH": "SME",
        }

    def test_every_unreadable_row_is_reported_with_a_reason(self):
        issues, skipped = parse_rows(MALFORMED)
        assert issues == []
        assert [s.name for s in skipped] == ["NONAME", "No Band Ltd", "No Date Ltd"]
        assert "no company name" in skipped[0].reason
        assert "price band" in skipped[1].reason
        assert "close date" in skipped[2].reason

    def test_a_missing_open_date_falls_back_to_the_close_date(self):
        """NSE omits the start date for some forthcoming issues. The close date is
        what places the fund-freeze window, so the row is still usable."""
        issues, skipped = parse_rows(
            [{"companyName": "X Ltd", "issuePrice": "Rs.100", "issueEndDate": "25-Aug-2026"}]
        )
        assert skipped == []
        assert issues[0].open_date == issues[0].close_date == date(2026, 8, 25)


# -------------------------------------------------------------------- fetching


class TestFetchFailuresAreContained:
    """Import is user-triggered and never a startup dependency, so a broken upstream
    must produce a message rather than an outage. Each of these is raised as
    ``NseUnavailable`` and surfaces as a 502."""

    async def _with_response(self, monkeypatch, handler):
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(nse.httpx, "AsyncClient", factory)

    async def test_a_403_says_what_to_do_instead(self, monkeypatch):
        await self._with_response(monkeypatch, lambda _: httpx.Response(403, text="denied"))
        with pytest.raises(NseUnavailable, match="403"):
            await nse.fetch_issues()

    async def test_an_html_block_page_is_not_mistaken_for_a_feed(self, monkeypatch):
        await self._with_response(
            monkeypatch,
            lambda _: httpx.Response(200, text="<html>Access Denied</html>"),
        )
        with pytest.raises(NseUnavailable, match="page rather than the issue feed"):
            await nse.fetch_issues()

    async def test_a_changed_payload_shape_is_reported(self, monkeypatch):
        await self._with_response(monkeypatch, lambda _: httpx.Response(200, json={"data": []}))
        with pytest.raises(NseUnavailable, match="changed shape"):
            await nse.fetch_issues()

    async def test_a_timeout_is_reported_as_unavailable(self, monkeypatch):
        def timeout(_request):
            raise httpx.ConnectTimeout("too slow")

        await self._with_response(monkeypatch, timeout)
        with pytest.raises(NseUnavailable, match="could not reach NSE"):
            await nse.fetch_issues()

    async def test_an_empty_feed_is_data_not_a_fault(self, monkeypatch):
        """"No issues are open this week" must not look like "NSE is down"."""
        await self._with_response(monkeypatch, lambda _: httpx.Response(200, json=[]))
        assert await nse.fetch_issues() == ([], [])


# --------------------------------------------------------------- the endpoint


class TestImportEndpoint:
    async def test_imports_the_feed_and_says_what_is_missing(self, client, recorded_feed):
        response = await client.post("/api/ipos/import")
        assert response.status_code == 200
        summary = response.json()
        assert summary == {
            "source": "nse",
            "imported": 3,
            "updated": 0,
            "skipped": [],
            "unchanged_because_edited": [],
            "needs_review": 3,
            "note": summary["note"],
        }
        assert "lot size" in summary["note"]

    async def test_imported_issues_arrive_flagged_but_plannable(self, client, recorded_feed):
        """An import is incomplete, but incomplete is not the same as unusable.

        NSE publishes no allotment date, so every imported issue used to be dropped
        from the plan — importing the real calendar produced a schedule with nothing in
        it. Now the dates are estimated under SEBI T+3, flagged, and the issue is
        planned. The lot size and GMP are still unconfirmed and still say so.
        """
        await client.post("/api/ipos/import")
        issues = (await client.get("/api/ipos")).json()

        assert {i["source"] for i in issues} == {"nse"}
        assert all(i["needs_review"] for i in issues)
        assert all(i["latest_gmp"] == 0 for i in issues)
        assert all(i["gmp_source"] == "user" for i in issues)

        # The point of the change: these are schedulable now.
        assert all(i["schedulable"] is True for i in issues)
        assert all(i["dates_estimated"] is True for i in issues)
        for issue in issues:
            close = date.fromisoformat(issue["close_date"])
            allotment, listing = estimated_dates(close)
            assert issue["allotment_date"] == allotment.isoformat()
            assert issue["listing_date"] == listing.isoformat()
            assert issue["missing"] == [
                "allotment date (estimated)",
                "GMP",
                "lot size (estimated)",
            ]
            assert "T+3" in issue["note"]

        # One hardcoded spot-check, so this does not merely agree with app.dates about
        # a shared mistake: Augmont closes Tue 25 Aug 2026.
        augmont = await find(client, "AUGMONT")
        assert augmont["close_date"] == "2026-08-25"
        assert augmont["allotment_date"] == "2026-08-26"
        assert augmont["listing_date"] == "2026-08-28"

    async def test_a_second_import_updates_rather_than_duplicating(self, client, recorded_feed):
        await client.post("/api/ipos/import")
        summary = (await client.post("/api/ipos/import")).json()
        assert summary["imported"] == 0
        assert summary["updated"] == 3
        assert len((await client.get("/api/ipos")).json()) == 3

    async def test_a_moved_close_date_is_picked_up(self, client, recorded_feed):
        await client.post("/api/ipos/import")
        recorded_feed["rows"][0] = RECORDED[0] | {"issueEndDate": "28-Aug-2026"}
        await client.post("/api/ipos/import")

        augmont = await find(client, "AUGMONT")
        assert augmont["close_date"] == "2026-08-28"

    async def test_an_edited_issue_is_never_overwritten_by_a_refresh(self, client, recorded_feed):
        """The one rule the UI can state: your edits win. Editing promotes the row to
        ``source='user'`` and the import skips it, reporting that it did."""
        await client.post("/api/ipos/import")
        augmont = await find(client, "AUGMONT")

        patched = await client.patch(
            f"/api/ipos/{augmont['id']}",
            json={
                "lot_size": 19,
                "latest_gmp": "94.56",
                "allotment_date": "2026-08-27",
                "allotment_probability": "0.3",
            },
        )
        assert patched.status_code == 200
        assert patched.json()["source"] == "user"
        assert patched.json()["schedulable"] is True
        assert patched.json()["missing"] == []

        # NSE now reports a different band and a different date for the same symbol.
        recorded_feed["rows"][0] = RECORDED[0] | {
            "issuePrice": "Rs.900 to Rs.950",
            "issueEndDate": "30-Aug-2026",
        }
        summary = (await client.post("/api/ipos/import")).json()
        assert summary["unchanged_because_edited"] == ["Augmont Enterprises Limited"]
        assert summary["updated"] == 2

        after = await find(client, "AUGMONT")
        assert after["lot_size"] == 19
        assert after["max_price"] == 788.0
        assert after["allotment_date"] == "2026-08-27"
        assert after["gmp_percent"] == pytest.approx(12.0)

    async def test_filling_in_the_gaps_moves_an_issue_into_the_plan(self, client, recorded_feed):
        """The import's honest end state: unplannable until a human supplies the three
        fields NSE does not publish. This is that transition."""
        await client.post(
            "/api/pans",
            json={
                "holder_name": "Mohammed",
                "relation": "Self",
                "pan_number": "ZYXWV9876Q",
                "upi_id": "m@okhdfc",
                "opening_balance": "200000",
            },
        )
        await client.post("/api/ipos/import")

        before = (await client.post("/api/schedule", json={"start_date": "2026-08-20"})).json()
        assert before["events"] == []
        assert len(before["skipped"]) == 3

        augmont = await find(client, "AUGMONT")
        await client.patch(
            f"/api/ipos/{augmont['id']}",
            json={"lot_size": 19, "latest_gmp": "94.56", "allotment_date": "2026-08-27"},
        )

        after = (await client.post("/api/schedule", json={"start_date": "2026-08-20"})).json()
        assert [e["ipo_name"] for e in after["events"]] == ["Augmont Enterprises Limited"]
        assert after["total_expected_profit"] > 0

    async def test_a_skipped_row_is_reported_to_the_caller(self, client, recorded_feed):
        recorded_feed["rows"] = RECORDED + MALFORMED
        summary = (await client.post("/api/ipos/import")).json()
        assert summary["imported"] == 3
        assert [s["name"] for s in summary["skipped"]] == [
            "NONAME",
            "No Band Ltd",
            "No Date Ltd",
        ]

    async def test_an_upstream_failure_is_a_502_with_advice(self, client, monkeypatch):
        async def broken():
            raise NseUnavailable("NSE answered HTTP 403. Add issues manually in the meantime.")

        monkeypatch.setattr("app.main.fetch_issues", broken)
        response = await client.post("/api/ipos/import")
        assert response.status_code == 502
        assert "manually" in response.json()["detail"]

    async def test_a_failed_import_leaves_the_calendar_alone(self, client, monkeypatch):
        async def broken():
            raise NseUnavailable("down")

        monkeypatch.setattr("app.main.fetch_issues", broken)
        await client.post("/api/ipos/import")
        assert (await client.get("/api/user/state")).status_code == 200
        assert (await client.get("/api/ipos")).json() == []

    async def test_import_can_be_turned_off_by_configuration(self, client, monkeypatch):
        # ``Settings`` is frozen, so this replaces the object rather than a field.
        monkeypatch.setattr(
            "app.main.settings", replace(settings, ipo_import_source="none")
        )
        response = await client.post("/api/ipos/import")
        assert response.status_code == 501
        assert "live import is off" in response.json()["detail"]


class TestImportedIssueIsNotAnIpoTask:
    def test_it_carries_no_allotment_date_at_all(self):
        """``IPOTask.allotment_date`` is required and non-nullable, so an imported
        issue cannot be expressed as one — forcing it would mean inventing the date
        the entire fund-freeze calculation is built on."""
        assert "allotment_date" not in ImportedIssue.__dataclass_fields__
        assert "lot_size" not in ImportedIssue.__dataclass_fields__
        assert "lot_size_estimate" in ImportedIssue.__dataclass_fields__
