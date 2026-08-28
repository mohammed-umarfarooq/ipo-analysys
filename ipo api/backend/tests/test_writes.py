"""The write path: funds, PAN accounts and the editable calendar.

These tests exist because the app's inputs used to be fixtures. What is being
checked is not that the endpoints return 200 — it is the four properties that make
a user-editable portfolio trustworthy:

* a plaintext PAN entered here never comes back out (D11),
* ``available_balance`` never disagrees with the ledger that explains it (D18),
* a delete cannot silently destroy committed bids (D19),
* ``gmp_percent`` is always derived from the rupee premium, so the ranking cannot
  disagree with the number on screen.

The database starts **empty**, as it now does on a real first run.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dates import estimated_dates
from app.main import app, get_session
from app.models import Base, CashMovement, Ipo, PanAccountRow
from app.repository import ensure_seeded, load_ipos

TOMORROW = date.today() + timedelta(days=1)

#: A syntactically valid PAN that exists nowhere. It must never appear in a response.
SECRET_PAN = "ZYXWV9876Q"


@pytest.fixture
async def db():
    """An empty database with only the user profile row — the real first-run state."""
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


# ----------------------------------------------------------------------- helpers


async def add_pan(client, *, holder="Mohammed", pan=SECRET_PAN, balance="15000"):
    response = await client.post(
        "/api/pans",
        json={
            "holder_name": holder,
            "relation": "Self",
            "pan_number": pan,
            "upi_id": f"{holder.lower()}@okhdfc",
            "opening_balance": balance,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def issue_payload(**overrides) -> dict:
    """A complete, plannable mainboard issue. Overridden per test."""
    return {
        "name": "Augmont Enterprises",
        "symbol": "AUGMONT",
        "issue_type": "Mainboard",
        "min_price": "750",
        "max_price": "788",
        "lot_size": 19,
        "latest_gmp": "94.56",
        "open_date": TOMORROW.isoformat(),
        "close_date": (TOMORROW + timedelta(days=2)).isoformat(),
        "allotment_date": (TOMORROW + timedelta(days=4)).isoformat(),
        "listing_date": (TOMORROW + timedelta(days=6)).isoformat(),
        "allotment_probability": "0.25",
    } | overrides


async def add_issue(client, **overrides) -> dict:
    response = await client.post("/api/ipos", json=issue_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------- first run


class TestFirstRunIsEmpty:
    """No PANs, no issues, no invented names. This is the bug the rewrite fixes."""

    async def test_no_pans_and_no_capital(self, client):
        state = (await client.get("/api/user/state")).json()
        assert state["pans"] == []
        assert state["liquid_capital"] == 0.0
        assert state["active_pan_count"] == 0

    async def test_no_ipos(self, client):
        assert (await client.get("/api/ipos")).json() == []

    async def test_planning_without_capital_is_a_409_not_a_crash(self, client):
        response = await client.post("/api/schedule", json={})
        assert response.status_code == 409
        assert "PAN" in response.json()["detail"]

    async def test_deleted_issues_stay_deleted_across_a_restart(self, client, db):
        """``ensure_seeded`` used to re-insert the whole calendar whenever ``ipos``
        was empty, so deleting every issue and restarting brought them all back."""
        created = await add_issue(client)
        assert (await client.delete(f"/api/ipos/{created['id']}")).status_code == 200

        async with db() as session:  # a second "startup" against the same database
            await ensure_seeded(session)
            assert await load_ipos(session) == []

    async def test_sample_data_is_opt_in_and_idempotent(self, client):
        first = (await client.post("/api/demo/sample-data")).json()
        assert first["pans_created"] == 3
        assert first["ipos_created"] > 0

        second = (await client.post("/api/demo/sample-data")).json()
        assert second == {"pans_created": 0, "ipos_created": 0}
        assert len((await client.get("/api/ipos")).json()) == first["ipos_created"]


# ------------------------------------------------------------------ PAN entry


class TestPanEntry:
    async def test_the_plaintext_pan_never_appears_in_any_response(self, client):
        created = await add_pan(client)
        assert created["pan_masked"] == "ZYXWV****Q"
        assert SECRET_PAN not in str(created)

        for path in ("/api/user/state", f"/api/pans/{created['id']}/movements"):
            body = (await client.get(path)).text
            assert SECRET_PAN not in body, f"{path} leaked the PAN"

    async def test_the_plaintext_pan_is_not_stored(self, client, db):
        await add_pan(client)
        async with db() as session:
            row = (await session.execute(select(PanAccountRow))).scalar_one()
            stored = " ".join(str(getattr(row, c.key)) for c in row.__table__.columns)
        assert SECRET_PAN not in stored
        assert len(row.pan_hash) == 64

    async def test_the_same_pan_twice_is_a_409(self, client):
        await add_pan(client)
        again = await client.post(
            "/api/pans",
            json={
                "holder_name": "Someone Else",
                "relation": "Father",
                "pan_number": SECRET_PAN,
                "upi_id": "else@oksbi",
            },
        )
        assert again.status_code == 409
        assert "already linked" in again.json()["detail"]

    async def test_a_malformed_pan_is_a_422_before_it_becomes_a_hash(self, client):
        response = await client.post(
            "/api/pans",
            json={"holder_name": "X", "pan_number": "NOTAPAN", "upi_id": "x@okhdfc"},
        )
        assert response.status_code == 422

    async def test_a_lowercase_pan_is_accepted_and_normalised(self, client):
        created = await add_pan(client, pan=SECRET_PAN.lower())
        assert created["pan_masked"] == "ZYXWV****Q"

    async def test_an_opening_balance_becomes_a_ledger_row(self, client):
        pan = await add_pan(client, balance="15000")
        ledger = (await client.get(f"/api/pans/{pan['id']}/movements")).json()
        assert [m["kind"] for m in ledger["movements"]] == ["OPENING"]
        assert ledger["available_balance"] == 15000.0

    async def test_an_unknown_pan_id_is_404_not_403(self, client):
        """A 403 would confirm the row exists, making the endpoint an existence oracle."""
        assert (await client.get("/api/pans/not-a-real-id/movements")).status_code == 404
        assert (await client.patch("/api/pans/not-a-real-id", json={})).status_code == 404
        assert (await client.delete("/api/pans/not-a-real-id")).status_code == 404


# ------------------------------------------------------------------ fund ledger


class TestFundLedger:
    async def test_add_and_withdraw_move_the_balance(self, client):
        pan = await add_pan(client, balance="15000")
        deposit = await client.post(
            f"/api/pans/{pan['id']}/movements",
            json={"kind": "DEPOSIT", "amount": "10000", "note": "salary"},
        )
        assert deposit.status_code == 201
        assert deposit.json()["available_balance"] == 25000.0

        withdrawal = await client.post(
            f"/api/pans/{pan['id']}/movements",
            json={"kind": "WITHDRAWAL", "amount": "5000", "note": "rent"},
        )
        assert withdrawal.json()["available_balance"] == 20000.0

        ledger = withdrawal.json()["movements"]
        assert [m["kind"] for m in ledger] == ["WITHDRAWAL", "DEPOSIT", "OPENING"]
        # Newest first on the way out, running balance accumulated oldest first.
        assert [m["balance_after"] for m in ledger] == [20000.0, 25000.0, 15000.0]

    async def test_overdrawing_is_a_409_and_changes_nothing(self, client):
        pan = await add_pan(client, balance="15000")
        response = await client.post(
            f"/api/pans/{pan['id']}/movements",
            json={"kind": "WITHDRAWAL", "amount": "15000.01"},
        )
        assert response.status_code == 409
        assert "overdraw" in response.json()["detail"]

        ledger = (await client.get(f"/api/pans/{pan['id']}/movements")).json()
        assert ledger["available_balance"] == 15000.0
        assert len(ledger["movements"]) == 1

    async def test_a_zero_or_negative_movement_is_a_422(self, client):
        pan = await add_pan(client)
        for amount in ("0", "-100"):
            response = await client.post(
                f"/api/pans/{pan['id']}/movements",
                json={"kind": "DEPOSIT", "amount": amount},
            )
            assert response.status_code == 422, amount

    async def test_set_balance_records_the_difference_rather_than_overwriting(self, client):
        pan = await add_pan(client, balance="15000")
        response = await client.patch(f"/api/pans/{pan['id']}", json={"balance": "9000"})
        assert response.status_code == 200
        assert response.json()["available_balance"] == 9000.0

        ledger = (await client.get(f"/api/pans/{pan['id']}/movements")).json()
        newest = ledger["movements"][0]
        assert newest["kind"] == "WITHDRAWAL"
        assert newest["amount"] == 6000.0
        assert "balance set to 9000.00" == newest["note"]

    async def test_setting_the_balance_it_already_has_writes_nothing(self, client):
        pan = await add_pan(client, balance="15000")
        await client.patch(f"/api/pans/{pan['id']}", json={"balance": "15000"})
        ledger = (await client.get(f"/api/pans/{pan['id']}/movements")).json()
        assert len(ledger["movements"]) == 1  # just the opening row

    async def test_a_mistyped_entry_can_be_reversed(self, client):
        pan = await add_pan(client, balance="15000")
        created = await client.post(
            f"/api/pans/{pan['id']}/movements",
            json={"kind": "DEPOSIT", "amount": "100000", "note": "typo"},
        )
        mistake = created.json()["movements"][0]
        assert created.json()["available_balance"] == 115000.0

        after = await client.delete(f"/api/movements/{mistake['id']}")
        assert after.status_code == 200
        assert after.json()["available_balance"] == 15000.0
        assert len(after.json()["movements"]) == 1

    async def test_reversing_an_entry_that_would_overdraw_is_refused(self, client):
        """Removing a deposit already spent would leave a negative balance, which the
        CHECK forbids — so it is a 409 with advice, not a 500 from the driver."""
        pan = await add_pan(client, balance="0")
        deposit = await client.post(
            f"/api/pans/{pan['id']}/movements", json={"kind": "DEPOSIT", "amount": "10000"}
        )
        movement_id = deposit.json()["movements"][0]["id"]
        await client.post(
            f"/api/pans/{pan['id']}/movements", json={"kind": "WITHDRAWAL", "amount": "8000"}
        )

        response = await client.delete(f"/api/movements/{movement_id}")
        assert response.status_code == 409
        assert "negative" in response.json()["detail"]
        assert (await client.get(f"/api/pans/{pan['id']}/movements")).json()[
            "available_balance"
        ] == 2000.0

    @pytest.mark.parametrize("seed", [1, 7, 42, 1729])
    async def test_the_balance_always_equals_the_sum_of_its_movements(self, client, db, seed):
        """D18's invariant, the one a materialised total exists to risk.

        A randomised sequence of every operation that can move money, then the
        column compared against the ledger that is supposed to explain it. Drift
        here is the same class of bug as D14: two truths, silently disagreeing.
        """
        rng = random.Random(seed)
        pan = await add_pan(client, balance="20000")
        pan_id = pan["id"]

        for _ in range(40):
            action = rng.choice(["deposit", "withdraw", "set", "reverse"])
            if action == "reverse":
                ledger = (await client.get(f"/api/pans/{pan_id}/movements")).json()
                if len(ledger["movements"]) > 1:
                    victim = rng.choice(ledger["movements"])
                    await client.delete(f"/api/movements/{victim['id']}")
                continue
            if action == "set":
                await client.patch(
                    f"/api/pans/{pan_id}", json={"balance": str(rng.randrange(0, 50000))}
                )
                continue
            kind = "DEPOSIT" if action == "deposit" else "WITHDRAWAL"
            await client.post(
                f"/api/pans/{pan_id}/movements",
                json={"kind": kind, "amount": str(rng.randrange(1, 30000))},
            )

        async with db() as session:
            row = await session.get(PanAccountRow, pan_id)
            movements = (
                (await session.execute(select(CashMovement).where(CashMovement.pan_id == pan_id)))
                .scalars()
                .all()
            )
        expected = sum((m.signed_amount for m in movements), Decimal("0.00"))
        assert row.available_balance == expected
        assert row.available_balance >= 0

        # And the figure the API publishes agrees with both.
        ledger = (await client.get(f"/api/pans/{pan_id}/movements")).json()
        assert Decimal(str(ledger["available_balance"])) == expected
        if ledger["movements"]:
            assert Decimal(str(ledger["movements"][0]["balance_after"])) == expected


# --------------------------------------------------------- funds change the plan


class TestEditingCapitalChangesThePlan:
    """The whole point of the feature: the plan follows the money."""

    async def test_more_capital_funds_more_bids(self, client):
        pan = await add_pan(client, holder="Mohammed", balance="15000")
        await add_issue(client, name="Cheap Issue", symbol="CHEAP", lot_size=19)
        await add_issue(
            client,
            name="Second Issue",
            symbol="SECOND",
            min_price="500",
            max_price="500",
            lot_size=30,
            latest_gmp="100",
            close_date=(TOMORROW + timedelta(days=2)).isoformat(),
            allotment_date=(TOMORROW + timedelta(days=4)).isoformat(),
            listing_date=(TOMORROW + timedelta(days=6)).isoformat(),
        )

        lean = (await client.post("/api/schedule", json={})).json()
        assert len(lean["events"]) == 1  # ₹15,000 funds exactly one of the two

        await client.post(
            f"/api/pans/{pan['id']}/movements", json={"kind": "DEPOSIT", "amount": "100000"}
        )
        flush = (await client.post("/api/schedule", json={})).json()
        assert len(flush["events"]) == 2
        assert flush["total_expected_profit"] > lean["total_expected_profit"]

    async def test_withdrawing_removes_a_bid_again(self, client):
        pan = await add_pan(client, balance="200000")
        await add_issue(client, name="A", symbol="A")
        await add_issue(
            client,
            name="B",
            symbol="B",
            min_price="500",
            max_price="500",
            lot_size=30,
            latest_gmp="100",
        )
        before = (await client.post("/api/schedule", json={})).json()
        assert len(before["events"]) == 2

        await client.patch(f"/api/pans/{pan['id']}", json={"balance": "15000"})
        after = (await client.post("/api/schedule", json={})).json()
        assert len(after["events"]) == 1

    async def test_deactivating_a_pan_takes_its_capital_out_of_the_plan(self, client):
        pan = await add_pan(client, balance="200000")
        await add_pan(client, holder="Aisha", pan="ABCDE1234F", balance="200000")
        await add_issue(client)

        both = (await client.post("/api/schedule", json={})).json()
        assert both["events"][0]["lots_applied"] == 2

        await client.patch(f"/api/pans/{pan['id']}", json={"is_active": False})
        one = (await client.post("/api/schedule", json={})).json()
        assert one["events"][0]["lots_applied"] == 1

    async def test_the_demat_balance_is_editable(self, client):
        response = await client.patch("/api/user", json={"demat_balance": "50000"})
        assert response.status_code == 200
        assert response.json()["demat_balance"] == 50000.0
        assert (await client.get("/api/user/state")).json()["demat_balance"] == 50000.0


# -------------------------------------------------------------- calendar entry


class TestCalendarEntry:
    async def test_a_created_issue_derives_its_gmp_percent(self, client):
        created = await add_issue(client, max_price="788", latest_gmp="94.56")
        # 94.56 / 788 * 100 = 12.0
        assert created["gmp_percent"] == pytest.approx(12.0)
        assert created["latest_gmp"] == 94.56
        assert created["source"] == "user"
        assert created["needs_review"] is False
        assert created["missing"] == []

    async def test_gmp_percent_cannot_be_supplied_and_is_re_derived_on_patch(self, client):
        created = await add_issue(client)
        patched = await client.patch(
            f"/api/ipos/{created['id']}",
            json={"latest_gmp": "39.40", "gmp_percent": "999"},
        )
        assert patched.status_code == 200
        # The bogus percentage is ignored; the real one follows the rupee premium.
        assert patched.json()["gmp_percent"] == pytest.approx(5.0)
        assert patched.json()["latest_gmp"] == 39.4

    async def test_an_incomplete_issue_says_what_it_is_missing(self, client):
        created = await add_issue(client, allotment_date=None, listing_date=None, latest_gmp="0")
        # No registrar date, so one is estimated — the issue is plannable, and labelled.
        assert created["schedulable"] is True
        assert created["dates_estimated"] is True
        assert created["missing"] == ["allotment date (estimated)", "GMP"]
        assert created["unblock_date"] is not None

    async def test_an_issue_with_no_allotment_date_is_planned_on_an_estimate(self, client):
        """Requirement 4: a missing registrar date must not delete the issue from the plan.

        This used to assert the opposite — ``events == []`` and a "registrar" reason in
        ``skipped``. That was the honest behaviour while the engine had no way to guess a
        freeze window, but the effect was that importing the real NSE calendar produced an
        empty plan, since NSE publishes no allotment date for anything.
        """
        await add_pan(client, balance="200000")
        created = await add_issue(client, allotment_date=None, listing_date=None)
        plan = (await client.post("/api/schedule", json={})).json()

        assert [e["ipo_id"] for e in plan["events"]] == [created["id"]]
        assert plan["skipped"] == []
        # The plan freezes capital over the estimated window, not an invented one.
        allotment, _ = estimated_dates(date.fromisoformat(created["close_date"]))
        assert plan["events"][0]["allotment_date"] == allotment.isoformat()

    async def test_confirming_a_date_clears_the_estimate_flag(self, client):
        created = await add_issue(client, allotment_date=None, listing_date=None)
        assert created["dates_estimated"] is True

        confirmed = (
            await client.patch(
                f"/api/ipos/{created['id']}",
                json={"allotment_date": created["allotment_date"]},
            )
        ).json()
        # Same date, but now a human has vouched for it, so the badge goes away and a
        # later close-date shift will not silently move it.
        assert confirmed["dates_estimated"] is False
        assert confirmed["missing"] == []
        assert confirmed["note"] is None

    @pytest.mark.parametrize(
        "override,why",
        [
            ({"min_price": "900"}, "band inverted"),
            ({"lot_size": 0}, "lot size zero"),
            ({"allotment_date": TOMORROW.isoformat()}, "allotment before close"),
            ({"open_date": (TOMORROW + timedelta(days=9)).isoformat()}, "opens after closing"),
            ({"allotment_probability": "1.5"}, "probability above one"),
            ({"latest_gmp": "-5"}, "negative premium"),
        ],
    )
    async def test_an_impossible_issue_is_a_422_not_a_500(self, client, override, why):
        """Every DB CHECK is mirrored in pydantic, so the caller gets the field name
        rather than an IntegrityError surfacing as a server fault."""
        response = await client.post("/api/ipos", json=issue_payload(**override))
        assert response.status_code == 422, f"{why}: got {response.status_code}"

    async def test_a_partial_patch_is_validated_against_the_stored_row(self, client):
        """``close_date`` alone says nothing about the stored ``allotment_date``; the
        pair is what has to stay consistent, so the merged row is re-validated."""
        created = await add_issue(client)
        response = await client.patch(
            f"/api/ipos/{created['id']}",
            json={"close_date": (TOMORROW + timedelta(days=8)).isoformat()},
        )
        assert response.status_code == 422

    async def test_a_duplicate_name_is_a_409(self, client):
        await add_issue(client)
        again = await client.post("/api/ipos", json=issue_payload(symbol="OTHER"))
        assert again.status_code == 409

    async def test_an_unknown_issue_id_is_404(self, client):
        assert (await client.patch("/api/ipos/nope", json={})).status_code == 404
        assert (await client.delete("/api/ipos/nope")).status_code == 404

    async def test_an_edited_issue_is_promoted_to_the_users_own(self, client, db):
        await client.post("/api/demo/sample-data")
        sample = next(i for i in (await client.get("/api/ipos")).json() if i["source"] == "sample")
        patched = await client.patch(f"/api/ipos/{sample['id']}", json={"latest_gmp": "10"})
        assert patched.json()["source"] == "user"

        async with db() as session:
            row = await session.get(Ipo, sample["id"])
        assert row.source == "user"
        assert row.needs_review is False


# ------------------------------------------------------------ deletion guards


class TestDeletionCannotDestroyCommittedBids:
    """``ipo_applications`` cascades from both parents, so a plain delete would take
    the record of committed money with it. D19."""

    @pytest.fixture
    async def committed(self, client):
        pan = await add_pan(client, balance="200000")
        ipo = await add_issue(client)
        result = await client.post("/api/schedule/commit", json={})
        assert result.status_code == 200
        assert sum(result.json().values()) > 0
        return pan, ipo

    async def test_deleting_a_used_pan_is_refused_with_advice(self, client, committed):
        pan, _ = committed
        response = await client.delete(f"/api/pans/{pan['id']}")
        assert response.status_code == 409
        assert "Deactivate" in response.json()["detail"]
        assert len((await client.get("/api/portfolio/history")).json()) == 1

    async def test_deleting_a_used_issue_is_refused(self, client, committed):
        _, ipo = committed
        response = await client.delete(f"/api/ipos/{ipo['id']}")
        assert response.status_code == 409
        assert len((await client.get("/api/portfolio/history")).json()) == 1
        assert len((await client.get("/api/ipos")).json()) == 1

    async def test_an_unused_pan_and_issue_delete_cleanly(self, client):
        pan = await add_pan(client)
        ipo = await add_issue(client)
        assert (await client.delete(f"/api/pans/{pan['id']}")).status_code == 200
        assert (await client.delete(f"/api/ipos/{ipo['id']}")).status_code == 200
        assert (await client.get("/api/user/state")).json()["pans"] == []
        assert (await client.get("/api/ipos")).json() == []

    async def test_deleting_a_pan_takes_its_ledger_with_it(self, client, db):
        pan = await add_pan(client, balance="15000")
        await client.delete(f"/api/pans/{pan['id']}")
        async with db() as session:
            remaining = (await session.execute(select(CashMovement))).scalars().all()
        assert remaining == []


# ------------------------------------------------------------- API shape guard


class TestWriteEndpointsTakeNoIdentity:
    async def test_no_write_route_or_body_accepts_a_user_id(self, client):
        """The read side was audited for this; the write side matters more, because
        here the consequence of a caller-supplied identity is a modified portfolio,
        not just a disclosed one."""
        spec = (await client.get("/openapi.json")).json()
        offenders = []
        for path, methods in spec["paths"].items():
            for verb, operation in methods.items():
                if verb.upper() not in {"POST", "PATCH", "PUT", "DELETE"}:
                    continue
                for param in operation.get("parameters", []):
                    if "user" in param["name"].lower():
                        offenders.append(f"{verb.upper()} {path} ?{param['name']}")
        for name, schema in spec["components"]["schemas"].items():
            if "user_id" in schema.get("properties", {}):
                offenders.append(name)
        assert not offenders, f"identity must come from the session: {offenders}"

    async def test_nothing_mutates_on_a_get(self, client):
        """Every mutation sits behind POST/PATCH/DELETE, so it cannot be triggered by
        a link, an image tag, or a browser prefetch. The copilot reaches this API by
        HTTP too, and its tools only ever issue GETs — see SECURITY.md."""
        spec = (await client.get("/openapi.json")).json()
        gets = {path for path, methods in spec["paths"].items() if "get" in methods}
        assert gets == {
            "/api/health",
            "/api/user/state",
            "/api/ipos",
            "/api/portfolio/history",
            "/api/pans/{pan_id}/movements",
        }
