"""HTTP layer tests.

Two things are being checked that the engine tests cannot cover: that the API
does not offer a parameter which could be used to read someone else's portfolio,
and that the engine's invariants still hold after a full round trip through
SQLAlchemy storage and JSON serialisation.

The scheduling invariants are verified with the same independent verifiers the
engine tests use, rebuilt from the HTTP response rather than from Python objects.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import AllotmentAssumption, IPOTask, PanAccount, ScheduleResult
from app.main import app, get_session
from app.models import Base, Ipo
from app.repository import ensure_seeded, load_sample_data
from tests.conftest import assert_no_overdraft, assert_sebi_one_lot_per_pan

#: Anchor the seeded calendar just ahead of today so the default ``start_date``
#: (today) has something to plan. Only relative spacing is asserted on, so this
#: stays deterministic regardless of when the suite runs.
ANCHOR_OFFSET = timedelta(days=1)


@pytest.fixture
async def db():
    """A fresh in-memory database with the sample portfolio loaded.

    ``ensure_seeded`` creates only the user profile now — an empty portfolio is the
    real first-run state. These tests need capital and a calendar to plan against,
    so they ask for the sample data explicitly, which is also what the UI's
    "Load sample data" button does. ``tests/test_writes.py`` covers the empty case.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        user = await ensure_seeded(session)
        await load_sample_data(session, user, anchor=date.today() + ANCHOR_OFFSET)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db):
    """API client bound to the test database.

    ``ASGITransport`` does not fire lifespan events, so the app's real engine and
    seeding never run — the override is the only database the routes can see.
    """

    async def override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http
    app.dependency_overrides.clear()


def as_pans(state: dict) -> list[PanAccount]:
    return [
        PanAccount(
            id=p["id"],
            holder_name=p["holder_name"],
            relation=p["relation"],
            available_balance=Decimal(str(p["available_balance"])),
        )
        for p in state["pans"]
        if p["is_active"]
    ]


def as_tasks(ipos: list[dict]) -> list[IPOTask]:
    return [
        IPOTask(
            id=i["id"],
            name=i["name"],
            lot_size=i["lot_size"],
            gmp_percent=Decimal(str(i["gmp_percent"])),
            close_date=date.fromisoformat(i["close_date"]),
            allotment_date=date.fromisoformat(i["allotment_date"]),
            issue_type=i["issue_type"],
            min_price=Decimal(str(i["min_price"])),
            max_price=Decimal(str(i["max_price"])),
            allotment_probability=Decimal(str(i["allotment_probability"])),
        )
        for i in ipos
        if i["schedulable"]
    ]


class TestHealth:
    async def test_reports_ok_and_the_active_backend(self, client):
        body = (await client.get("/api/health")).json()
        assert body["status"] == "ok"
        assert body["database"] == "sqlite"
        assert body["gmp_provider"] == "seeded"

    async def test_surfaces_production_warnings_rather_than_hiding_them(self, client):
        body = (await client.get("/api/health")).json()
        joined = " ".join(body["production_warnings"])
        assert "SQLite" in joined
        assert "PAN_HASH_SALT" in joined


class TestUserState:
    async def test_liquid_capital_is_the_sum_of_active_pans(self, client):
        state = (await client.get("/api/user/state")).json()
        assert state["active_pan_count"] == 3
        assert state["liquid_capital"] == pytest.approx(335000.0)
        assert state["liquid_capital"] == pytest.approx(
            sum(p["available_balance"] for p in state["pans"])
        )

    async def test_pans_are_masked_and_the_number_never_leaves_the_server(self, client):
        raw = (await client.get("/api/user/state")).text
        state = json.loads(raw)
        for pan in state["pans"]:
            assert pan["pan_masked"].count("*") == 4
            assert len(pan["pan_masked"]) == 10
        # D11: not masked-in-one-field-and-leaked-in-another.
        for plaintext in ("ABCDE1234F", "BCDEF2345G", "CDEFG3456H"):
            assert plaintext not in raw
        assert "pan_hash" not in raw


class TestNoIdentityFromTheCaller:
    """SECURITY.md: the blueprint's ``/api/user/state?user_id=`` was an IDOR."""

    async def test_no_route_accepts_a_user_id_parameter(self, client):
        spec = (await client.get("/openapi.json")).json()
        offenders = []
        for path, methods in spec["paths"].items():
            for verb, operation in methods.items():
                for param in operation.get("parameters", []):
                    if "user" in param["name"].lower():
                        offenders.append(f"{verb.upper()} {path} ?{param['name']}")
        assert not offenders, f"identity must come from the session, not the request: {offenders}"

    async def test_no_request_body_accepts_a_user_id(self, client):
        """Every schema, not just those named ``*Request``.

        The write schemas added for the editable rail are called ``PanCreate``,
        ``IpoWrite`` and so on, and a suffix-based check would have skipped exactly
        the endpoints where a caller-supplied identity does the most damage.
        """
        spec = (await client.get("/openapi.json")).json()
        offenders = [
            name
            for name, schema in spec["components"]["schemas"].items()
            if "user_id" in schema.get("properties", {})
        ]
        assert not offenders, f"identity must come from the session, not the body: {offenders}"


class TestIpoUniverse:
    async def test_lists_the_seeded_calendar_with_priority_ranks(self, client):
        ipos = (await client.get("/api/ipos")).json()
        assert len(ipos) == 8
        ranks = sorted(i["priority_rank"] for i in ipos)
        assert ranks == list(range(1, 9))

    async def test_rank_one_is_the_highest_gmp(self, client):
        ipos = (await client.get("/api/ipos")).json()
        top = next(i for i in ipos if i["priority_rank"] == 1)
        assert top["gmp_percent"] == max(i["gmp_percent"] for i in ipos)

    async def test_lot_cost_uses_the_cutoff_price(self, client):
        """D7: blocked capital is the top of the band, not the floor."""
        for ipo in (await client.get("/api/ipos")).json():
            assert ipo["cutoff_price"] == ipo["max_price"]
            assert ipo["lot_cost"] == pytest.approx(ipo["max_price"] * ipo["lot_size"])

    async def test_unblock_date_is_one_day_after_allotment(self, client):
        """Rule 2, T+1."""
        for ipo in (await client.get("/api/ipos")).json():
            if ipo["allotment_date"]:
                allotment = date.fromisoformat(ipo["allotment_date"])
                assert date.fromisoformat(ipo["unblock_date"]) == allotment + timedelta(days=1)


class TestSchedule:
    async def test_default_run_produces_a_plan(self, client):
        result = (await client.post("/api/schedule", json={})).json()
        assert result["policy"] == "value_first"
        assert result["events"]
        assert result["total_expected_profit"] > 0

    async def test_every_ipo_is_scheduled_or_explained(self, client):
        ipos = (await client.get("/api/ipos")).json()
        result = (await client.post("/api/schedule", json={})).json()
        accounted = {e["ipo_id"] for e in result["events"]} | {
            s["ipo_id"] for s in result["skipped"]
        }
        assert accounted == {i["id"] for i in ipos}

    async def test_invariants_survive_the_database_and_json_round_trip(self, client):
        """The engine's core guarantees, re-verified from the HTTP response."""
        state = (await client.get("/api/user/state")).json()
        ipos = (await client.get("/api/ipos")).json()
        payload = (
            await client.post("/api/schedule", json={"assumption": "expected"})
        ).json()

        result = ScheduleResult(**payload)
        pans, tasks = as_pans(state), as_tasks(ipos)
        assert_no_overdraft(result, pans, tasks, AllotmentAssumption.EXPECTED)
        assert_sebi_one_lot_per_pan(result, pans)

    async def test_min_gmp_override_is_honoured(self, client):
        strict = (await client.post("/api/schedule", json={"min_gmp": "50"})).json()
        for event in strict["events"]:
            assert event["gmp_percent"] >= 50
        reasons = " ".join(s["reason"] for s in strict["skipped"])
        assert "below the 50" in reasons

    async def test_a_start_date_after_every_close_schedules_nothing(self, client):
        far = (date.today() + timedelta(days=365)).isoformat()
        result = (await client.post("/api/schedule", json={"start_date": far})).json()
        assert result["events"] == []
        assert len(result["skipped"]) == 8

    async def test_jit_greedy_is_still_available_as_a_baseline(self, client):
        result = (await client.post("/api/schedule", json={"policy": "jit_greedy"})).json()
        assert result["policy"] == "jit_greedy"


class TestUnschedulableIpo:
    """D10: ``allotment_date`` is nullable, and the API must say so out loud."""

    async def test_an_ipo_without_an_allotment_date_is_reported_not_dropped(self, client, db):
        async with db() as session:
            session.add(
                Ipo(
                    name="Unannounced Issue",
                    symbol="UNANNOUNCED",
                    min_price=Decimal("100.00"),
                    max_price=Decimal("110.00"),
                    lot_size=10,
                    gmp_percent=Decimal("90.00"),
                    open_date=date.today(),
                    close_date=date.today() + timedelta(days=3),
                )
            )
            await session.commit()

        listed = (await client.get("/api/ipos")).json()
        pending = next(i for i in listed if i["name"] == "Unannounced Issue")
        assert pending["schedulable"] is False
        assert pending["priority_rank"] is None
        assert "registrar" in pending["note"]

        result = (await client.post("/api/schedule", json={})).json()
        skipped = next(s for s in result["skipped"] if s["ipo_name"] == "Unannounced Issue")
        assert "allotment date" in skipped["reason"]
        # A 90% GMP issue is the most attractive on the board; it must not be bid
        # on regardless, because its freeze window is unknown.
        assert "Unannounced Issue" not in {e["ipo_name"] for e in result["events"]}


class TestPolicyComparison:
    async def test_returns_both_policies_and_a_delta(self, client):
        body = (await client.post("/api/schedule/compare", json={})).json()
        assert body["value_first"]["policy"] == "value_first"
        assert body["jit_greedy"]["policy"] == "jit_greedy"
        assert body["delta_expected_profit"] == pytest.approx(
            body["value_first"]["total_expected_profit"]
            - body["jit_greedy"]["total_expected_profit"],
            abs=0.01,
        )

    @staticmethod
    async def _leave_one_small_pan(db, balance: str) -> None:
        """Reduce the portfolio to a single PAN holding ``balance``."""
        from sqlalchemy import select

        from app.models import PanAccountRow

        async with db() as session:
            rows = list((await session.execute(select(PanAccountRow))).scalars())
            for row in rows:
                row.is_active = row.relation == "Self"
                if row.is_active:
                    row.available_balance = Decimal(balance)
            await session.commit()

    async def test_the_fix_is_visible_when_capital_is_scarce(self, client, db):
        """D1 end to end: one small PAN, so every bid has an opportunity cost."""
        await self._leave_one_small_pan(db, "15000.00")

        body = (
            await client.post(
                "/api/schedule/compare", json={"assumption": "none_allotted"}
            )
        ).json()

        assert body["capital_constrained"] is True
        assert body["delta_expected_profit"] > 0, (
            "with one small PAN the GMP ranking must beat close-date order"
        )
        # The ranking claims the two best issues; close-date order takes whatever
        # closes next once the first block has matured.
        won = [e["gmp_percent"] for e in body["value_first"]["events"]]
        baseline = [e["gmp_percent"] for e in body["jit_greedy"]["events"]]
        assert sorted(won, reverse=True)[:2] == [62.5, 55.2]
        assert 27.8 in baseline

    async def test_expected_allotment_can_leave_room_for_only_one_bid(self, client, db):
        """Why the case above must use ``none_allotted`` to show a difference.

        Under ``EXPECTED``, the 9% allotment probability on the top-ranked issue
        permanently consumes ₹1,310.40 of the ₹15,000, leaving ₹13,689.60 — less
        than the cheapest remaining lot. Only one bid fits at all, so both
        policies pick the same one and the delta is legitimately zero.
        """
        await self._leave_one_small_pan(db, "15000.00")

        body = (
            await client.post("/api/schedule/compare", json={"assumption": "expected"})
        ).json()

        assert len(body["value_first"]["events"]) == 1
        assert body["delta_expected_profit"] == 0.0
        assert body["value_first"]["events"][0]["gmp_percent"] == 62.5


class TestCommitAndHistory:
    async def test_committing_creates_one_application_per_pan_per_ipo(self, client):
        plan = (await client.post("/api/schedule", json={})).json()
        expected = sum(len(e["pans_used"]) for e in plan["events"])

        commit = (await client.post("/api/schedule/commit", json={})).json()
        assert commit["applications_created"] == expected

        history = (await client.get("/api/portfolio/history")).json()
        assert len(history) == expected
        assert all(row["lots_applied"] == 1 for row in history)

    async def test_recommitting_the_same_plan_is_a_no_op(self, client):
        await client.post("/api/schedule/commit", json={})
        again = (await client.post("/api/schedule/commit", json={})).json()
        assert again["applications_created"] == 0

    async def test_history_masks_the_pan(self, client):
        await client.post("/api/schedule/commit", json={})
        raw = (await client.get("/api/portfolio/history")).text
        assert "ABCDE1234F" not in raw
        assert "ABCDE****F" in raw

    async def test_user_state_counts_committed_applications(self, client):
        before = (await client.get("/api/user/state")).json()
        assert before["committed_application_count"] == 0
        await client.post("/api/schedule/commit", json={})
        after = (await client.get("/api/user/state")).json()
        assert after["committed_application_count"] > 0


class TestNoActiveCapital:
    async def test_returns_409_rather_than_an_empty_plan(self, client, db):
        from sqlalchemy import select

        from app.models import PanAccountRow

        async with db() as session:
            for row in (await session.execute(select(PanAccountRow))).scalars():
                row.is_active = False
            await session.commit()

        response = await client.post("/api/schedule", json={})
        assert response.status_code == 409
        assert "no active PAN" in response.json()["detail"]
