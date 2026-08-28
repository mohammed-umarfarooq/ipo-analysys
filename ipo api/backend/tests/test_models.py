"""Persistence-layer tests.

Two things are worth guarding here: that money survives a database round trip
exactly (D3 is easy to reintroduce at the storage boundary), and that the models
have not drifted from the hand-written PostgreSQL migration.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import BACKEND_ROOT
from app.models import (
    Base,
    ConversationMemory,
    Ipo,
    IpoApplication,
    PanAccountRow,
    UserProfile,
    hash_pan,
    mask_pan,
)

MIGRATION = BACKEND_ROOT / "migrations" / "001_init.sql"


@pytest.fixture
async def session():
    """A fresh in-memory database per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed_user(session) -> tuple[UserProfile, PanAccountRow, Ipo]:
    user = UserProfile(name="Test User")
    session.add(user)
    await session.flush()

    pan = PanAccountRow(
        user_id=user.id,
        holder_name="Test User",
        pan_masked=mask_pan("ABCDE1234F"),
        pan_hash=hash_pan("ABCDE1234F", "salt"),
        upi_id="test@upi",
        available_balance=Decimal("150000.00"),
    )
    ipo = Ipo(
        name="Test Issue",
        symbol="TESTISS",
        min_price=Decimal("228.00"),
        max_price=Decimal("240.00"),
        lot_size=62,
        gmp_percent=Decimal("18.75"),
        open_date=date(2026, 3, 1),
        close_date=date(2026, 3, 3),
        allotment_date=date(2026, 3, 6),
    )
    session.add_all([pan, ipo])
    await session.flush()
    return user, pan, ipo


class TestSchemaBuilds:
    async def test_create_all_succeeds_on_sqlite(self, session):
        user, pan, ipo = await _seed_user(session)
        await session.commit()
        assert user.id and pan.id and ipo.id

    async def test_all_expected_tables_exist(self):
        assert set(Base.metadata.tables) == {
            "user_profiles",
            "pan_accounts",
            "cash_movements",
            "ipos",
            "ipo_applications",
            "conversation_memories",
        }


class TestMoneyExactness:
    """The float-hostile values from D3 must survive storage unchanged."""

    @pytest.mark.parametrize(
        "amount",
        ["14999.70", "44999.10", "0.01", "99999999999.99", "1499.97", "0.00"],
    )
    async def test_decimal_round_trips_exactly(self, session, amount):
        user, pan, _ = await _seed_user(session)
        pan.available_balance = Decimal(amount)
        await session.commit()
        session.expunge_all()

        reloaded = await session.get(PanAccountRow, pan.id)
        assert reloaded.available_balance == Decimal(amount)
        assert isinstance(reloaded.available_balance, Decimal)

    async def test_three_stored_lots_still_sum_exactly(self, session):
        """The exact case that lost a lot under float arithmetic."""
        user, _, _ = await _seed_user(session)
        for i in range(3):
            session.add(
                PanAccountRow(
                    user_id=user.id,
                    holder_name=f"Holder {i}",
                    pan_masked=mask_pan(f"ABCDE123{i}F"),
                    pan_hash=hash_pan(f"ABCDE123{i}F", "salt"),
                    upi_id=f"h{i}@upi",
                    available_balance=Decimal("14999.70"),
                )
            )
        await session.commit()
        session.expunge_all()

        reloaded = await session.get(UserProfile, user.id)
        await session.refresh(reloaded, ["pans"])
        total = sum(
            (p.available_balance for p in reloaded.pans if p.available_balance == Decimal("14999.70")),
            Decimal("0.00"),
        )
        assert total == Decimal("44999.10")


class TestMoneyIsOrderedInSql:
    """D14: money must compare numerically in the database, not lexicographically.

    An earlier version of :class:`Money` stored text on SQLite. Text round-trips
    exactly, so the exactness tests passed — but SQLite then compared ``'985.00'``
    against ``'1040.00'`` as strings, inverting every money CHECK constraint. The
    band below is the exact case that surfaced it: a perfectly valid price band
    that the database rejected.
    """

    async def test_a_valid_band_whose_digits_sort_backwards_is_accepted(self, session):
        session.add(
            Ipo(
                name="Vertex", min_price=Decimal("985.00"), max_price=Decimal("1040.00"),
                lot_size=14, open_date=date(2026, 3, 1), close_date=date(2026, 3, 3),
            )
        )
        await session.commit()

    async def test_an_inverted_band_whose_digits_sort_forwards_is_rejected(self, session):
        session.add(
            Ipo(
                name="Backwards", min_price=Decimal("1040.00"), max_price=Decimal("985.00"),
                lot_size=14, open_date=date(2026, 3, 1), close_date=date(2026, 3, 3),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_order_by_on_a_money_column_is_numeric(self, session):
        user, _, _ = await _seed_user(session)
        for i, amount in enumerate(["985.00", "1040.00", "99.00", "10000.00"]):
            session.add(
                PanAccountRow(
                    user_id=user.id, holder_name=f"H{i}",
                    pan_masked=mask_pan(f"ABCDE900{i}F"), pan_hash=hash_pan(f"ABCDE900{i}F", "s"),
                    upi_id=f"h{i}@upi", available_balance=Decimal(amount),
                )
            )
        await session.commit()

        rows = await session.execute(
            select(PanAccountRow.available_balance)
            .where(PanAccountRow.holder_name.like("H%"))
            .order_by(PanAccountRow.available_balance)
        )
        amounts = [r[0] for r in rows]
        assert amounts == sorted(amounts), "money sorted as text, not as a number"
        assert amounts[0] == Decimal("99.00")
        assert amounts[-1] == Decimal("10000.00")


class TestRule1EnforcedByTheDatabase:
    """D10: the SEBI cap must not depend on application code being correct."""

    async def test_duplicate_pan_for_one_ipo_is_rejected(self, session):
        _, pan, ipo = await _seed_user(session)
        session.add(
            IpoApplication(
                ipo_id=ipo.id, pan_id=pan.id, blocked_amount=Decimal("14880.00"),
                bid_date=date(2026, 3, 3),
            )
        )
        await session.commit()

        session.add(
            IpoApplication(
                ipo_id=ipo.id, pan_id=pan.id, blocked_amount=Decimal("14880.00"),
                bid_date=date(2026, 3, 3),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_multiple_lots_on_one_application_is_rejected(self, session):
        _, pan, ipo = await _seed_user(session)
        session.add(
            IpoApplication(
                ipo_id=ipo.id, pan_id=pan.id, lots_applied=3,
                blocked_amount=Decimal("44640.00"), bid_date=date(2026, 3, 3),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


class TestConstraints:
    async def test_negative_pan_balance_is_rejected(self, session):
        user, _, _ = await _seed_user(session)
        session.add(
            PanAccountRow(
                user_id=user.id, holder_name="Debtor",
                pan_masked=mask_pan("ZZZZZ9999Z"), pan_hash=hash_pan("ZZZZZ9999Z", "s"),
                upi_id="z@upi", available_balance=Decimal("-1.00"),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_inverted_price_band_is_rejected(self, session):
        session.add(
            Ipo(
                name="Inverted", min_price=Decimal("500.00"), max_price=Decimal("100.00"),
                lot_size=10, open_date=date(2026, 3, 1), close_date=date(2026, 3, 3),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_allotment_before_close_is_rejected(self, session):
        session.add(
            Ipo(
                name="Backwards", min_price=Decimal("100.00"), max_price=Decimal("100.00"),
                lot_size=10, open_date=date(2026, 3, 1), close_date=date(2026, 3, 10),
                allotment_date=date(2026, 3, 4),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_allotment_date_may_be_unknown(self, session):
        """D10: a freshly announced issue has no allotment date yet."""
        session.add(
            Ipo(
                name="Unannounced", min_price=Decimal("100.00"), max_price=Decimal("110.00"),
                lot_size=10, open_date=date(2026, 3, 1), close_date=date(2026, 3, 3),
            )
        )
        await session.commit()

    async def test_bad_conversation_role_is_rejected(self, session):
        user, _, _ = await _seed_user(session)
        session.add(
            ConversationMemory(
                user_id=user.id, session_id="s1", role="wizard", content="hi"
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


class TestPanPrivacy:
    """D11: the PAN number itself must never be persisted."""

    def test_mask_hides_the_middle_digits(self):
        assert mask_pan("ABCDE1234F") == "ABCDE****F"

    def test_mask_normalises_case_and_whitespace(self):
        assert mask_pan("  abcde1234f ") == "ABCDE****F"

    def test_mask_rejects_a_malformed_pan(self):
        with pytest.raises(ValueError, match="exactly 10 characters"):
            mask_pan("TOOSHORT")

    def test_hash_is_stable_and_salt_dependent(self):
        assert hash_pan("ABCDE1234F", "s1") == hash_pan("abcde1234f", "s1")
        assert hash_pan("ABCDE1234F", "s1") != hash_pan("ABCDE1234F", "s2")
        assert len(hash_pan("ABCDE1234F", "s1")) == 64

    def test_no_model_has_a_plaintext_pan_column(self):
        columns = {c.name for t in Base.metadata.tables.values() for c in t.columns}
        assert "pan_number" not in columns, "the raw PAN must not be stored"
        assert {"pan_masked", "pan_hash"} <= columns


class TestNoDriftFromPostgresMigration:
    """The models and the hand-written PostgreSQL DDL must describe one schema."""

    @staticmethod
    def _migration_tables() -> dict[str, str]:
        sql = MIGRATION.read_text(encoding="utf-8")
        blocks: dict[str, str] = {}
        for match in re.finditer(
            r"CREATE TABLE (\w+)\s*\((.*?)\n\);", sql, re.DOTALL
        ):
            blocks[match.group(1)] = match.group(2)
        return blocks

    def test_migration_file_is_parseable(self):
        assert MIGRATION.exists()
        assert self._migration_tables(), "no CREATE TABLE blocks found"

    def test_table_names_match(self):
        assert set(self._migration_tables()) == set(Base.metadata.tables)

    def test_every_model_column_exists_in_the_migration(self):
        blocks = self._migration_tables()
        missing = []
        for name, table in Base.metadata.tables.items():
            body = blocks[name]
            for column in table.columns:
                if not re.search(rf"^\s*{re.escape(column.name)}\s", body, re.MULTILINE):
                    missing.append(f"{name}.{column.name}")
        assert not missing, f"columns present in the models but not the migration: {missing}"

    @staticmethod
    def _migration_sql(strip_comments: bool = False) -> str:
        sql = MIGRATION.read_text(encoding="utf-8")
        if strip_comments:
            sql = re.sub(r"--[^\n]*", "", sql)
        return sql

    def test_migration_keeps_the_sebi_unique_constraint(self):
        assert "UNIQUE (ipo_id, pan_id)" in self._migration_sql()

    def test_migration_does_not_reintroduce_a_pooled_bank_balance(self):
        """D4: one shared balance is what allowed unexecutable schedules."""
        # Comments are stripped: the migration *documents* why the column is gone.
        assert "total_bank_balance" not in self._migration_sql(strip_comments=True)
