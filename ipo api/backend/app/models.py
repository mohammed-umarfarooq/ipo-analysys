"""SQLAlchemy 2.x async models — the single source of truth for the dev schema.

``migrations/001_init.sql`` is the production PostgreSQL migration (it also carries
the extensions, triggers and views that a portable model layer cannot express).
These models mirror it and are what ``create_all`` builds for local SQLite, so a
developer needs no database server. ``tests/test_models.py`` asserts the two agree.

Money never touches a binary float. See :class:`Money` and D3 in docs/DEVIATIONS.md.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings
from app.domain import money


class Money(TypeDecorator):
    """Exact decimal money on both PostgreSQL and SQLite.

    SQLite has no true decimal type: SQLAlchemy's ``Numeric`` round-trips through a
    C double there, which reintroduces exactly the drift D3 exists to prevent. So on
    SQLite the value is stored as an **integer number of paise** and scaled back on
    read; on PostgreSQL it uses native ``NUMERIC(14,2)``. Callers always see a
    ``Decimal``.

    Integer paise rather than text (D14). Text is equally exact for round-tripping
    but it is not *ordered*: SQLite would compare ``'985.00' > '1040.00'`` as
    strings, which silently inverts every money ``CHECK`` constraint and every
    ``ORDER BY`` on a money column. Integers keep SQL-level comparisons meaningful,
    which is the whole point of enforcing the constraints in the database (D10).
    """

    impl = Numeric(14, 2)
    cache_ok = True

    def load_dialect_impl(self, dialect):  # noqa: ANN001, ANN201
        if dialect.name == "sqlite":
            return dialect.type_descriptor(BigInteger())
        return dialect.type_descriptor(Numeric(14, 2, asdecimal=True))

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        exact = money(Decimal(str(value)))
        if dialect.name == "sqlite":
            return int(exact.scaleb(2).to_integral_value())
        return exact

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        if dialect.name == "sqlite":
            return money(Decimal(int(value)).scaleb(-2))
        return money(Decimal(str(value)))


def _uuid() -> str:
    return str(uuid.uuid4())


def hash_pan(pan_number: str, salt: str) -> str:
    """Salted digest of a PAN, for uniqueness without storing the number (D11)."""
    normalised = pan_number.strip().upper()
    return hashlib.sha256(f"{salt}:{normalised}".encode()).hexdigest()


def mask_pan(pan_number: str) -> str:
    """``ABCDE1234F`` -> ``ABCDE****F``. What the UI is allowed to render."""
    normalised = pan_number.strip().upper()
    if len(normalised) != 10:
        raise ValueError("a PAN is exactly 10 characters")
    return f"{normalised[:5]}****{normalised[9]}"


class Base(DeclarativeBase):
    pass


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # No total_bank_balance: cash is per-PAN. See D4 and the user_liquid_capital view.
    total_demat_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    #: How the scheduler treats capital. ``pooled`` (the default) plans one shared
    #: war-chest — the sum of active PAN balances is a single fund any PAN may draw,
    #: recycled as ASBA unblocks at T+1. ``per_pan`` ring-fences each holder's balance
    #: (ASBA-accurate). Storage stays per-PAN either way (D4); this selects only the
    #: capacity test in :mod:`app.scheduler`, so no pooled *balance* is ever stored.
    capital_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="pooled")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    pans: Mapped[list[PanAccountRow]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def liquid_capital(self) -> Decimal:
        """Total ASBA-freezable cash across active PANs."""
        return sum(
            (p.available_balance for p in self.pans if p.is_active), Decimal("0.00")
        )

    __table_args__ = (
        CheckConstraint(
            "capital_mode IN ('pooled', 'per_pan')", name="user_profiles_capital_mode"
        ),
    )


class PanAccountRow(Base):
    __tablename__ = "pan_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
    )
    holder_name: Mapped[str] = mapped_column(String(100), nullable=False)
    relation: Mapped[str] = mapped_column(String(50), default="Self")
    # The PAN itself is never persisted in the clear (D11).
    pan_masked: Mapped[str] = mapped_column(String(10), nullable=False)
    pan_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    upi_id: Mapped[str] = mapped_column(String(100), nullable=False)
    linked_bank_name: Mapped[str | None] = mapped_column(String(100))
    # The balance ASBA can actually freeze in THIS holder's account (D4).
    available_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[UserProfile] = relationship(back_populates="pans")
    applications: Mapped[list[IpoApplication]] = relationship(back_populates="pan")
    movements: Mapped[list[CashMovement]] = relationship(
        back_populates="pan", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("available_balance >= 0", name="pan_accounts_balance_non_negative"),
        Index("pan_accounts_user_idx", "user_id"),
    )


class CashMovement(Base):
    """One dated cash event in a PAN holder's bank account.

    ``pan_accounts.available_balance`` is the running total of these rows. It stays a
    stored column because the engine reads it on every planning pass and a ``CHECK``
    constraint depends on it, so the ledger is a materialised sum with exactly one
    writer — :func:`app.repository.apply_movement` — rather than two independent
    truths. ``tests/test_writes.py`` pins the two together (D18).

    ``amount`` is always positive and direction lives in ``kind``, so a row cannot
    contradict its own label: there is no way to store a ``DEPOSIT`` of -5000. There
    is deliberately no ``ADJUSTMENT`` kind — "set balance to X" resolves to whichever
    of ``DEPOSIT``/``WITHDRAWAL`` closes the gap, so every row still says which way
    the money moved.
    """

    __tablename__ = "cash_movements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    pan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pan_accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    note: Mapped[str | None] = mapped_column(String(140))
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    pan: Mapped[PanAccountRow] = relationship(back_populates="movements")

    #: Signed effect of each kind on the balance.
    DIRECTIONS: ClassVar[dict[str, int]] = {"OPENING": 1, "DEPOSIT": 1, "WITHDRAWAL": -1}

    @property
    def signed_amount(self) -> Decimal:
        return self.amount * self.DIRECTIONS[self.kind]

    __table_args__ = (
        CheckConstraint(
            "kind IN ('OPENING', 'DEPOSIT', 'WITHDRAWAL')",
            name="cash_movements_kind",
        ),
        CheckConstraint("amount > 0", name="cash_movements_amount_positive"),
        Index("cash_movements_pan_idx", "pan_id", "occurred_on"),
    )


class Ipo(Base):
    __tablename__ = "ipos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    symbol: Mapped[str | None] = mapped_column(String(50), unique=True)
    issue_type: Mapped[str] = mapped_column(String(20), default="Mainboard")
    min_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    max_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    latest_gmp: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    gmp_percent: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    open_date: Mapped[date] = mapped_column(Date, nullable=False)
    close_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Nullable: unknown until the registrar fixes it (D10).
    allotment_date: Mapped[date | None] = mapped_column(Date)
    listing_date: Mapped[date | None] = mapped_column(Date)
    registrar_name: Mapped[str | None] = mapped_column(String(100))
    allotment_probability: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), default=Decimal("0.000")
    )
    #: Where the row came from: ``user`` typed it, ``nse`` imported it, ``sample`` is
    #: opt-in demo data. Load-bearing, not metadata: a refresh from NSE may only
    #: overwrite rows still marked ``nse``, and editing a row promotes it to ``user``
    #: so the import can never undo an edit.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    #: An import is incomplete by construction — NSE publishes no lot size, no
    #: allotment date and no GMP — so imported rows are flagged until a human has
    #: confirmed the numbers that decide how much capital gets frozen.
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Where ``latest_gmp`` came from: ``user`` typed it (authoritative) or ``live``
    #: pulled it from a grey-market aggregator. GMP is unofficial and unregulated, so
    #: a live refresh never overwrites a row a human has edited.
    gmp_source: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    #: True when allotment/listing were estimated under SEBI's T+3 regime (close + 1
    #: and + 3 working days) because the registrar has not published them. Cleared the
    #: moment a human confirms a date, so the UI can badge an estimate as such.
    dates_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    applications: Mapped[list[IpoApplication]] = relationship(
        back_populates="ipo", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("min_price <= max_price", name="ipos_price_band"),
        CheckConstraint("lot_size > 0", name="ipos_lot_size_positive"),
        CheckConstraint("open_date <= close_date", name="ipos_calendar"),
        CheckConstraint(
            "allotment_date IS NULL OR allotment_date >= close_date", name="ipos_allotment"
        ),
        CheckConstraint("issue_type IN ('Mainboard', 'SME')", name="ipos_issue_type"),
        CheckConstraint("source IN ('user', 'nse', 'sample')", name="ipos_source"),
        CheckConstraint("gmp_source IN ('user', 'live')", name="ipos_gmp_source"),
        CheckConstraint(
            "allotment_probability >= 0 AND allotment_probability <= 1",
            name="ipos_allotment_probability",
        ),
        Index("ipos_close_date_idx", "close_date"),
    )


class IpoApplication(Base):
    __tablename__ = "ipo_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ipo_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ipos.id", ondelete="CASCADE"), nullable=False
    )
    pan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pan_accounts.id", ondelete="CASCADE"), nullable=False
    )
    lots_applied: Mapped[int] = mapped_column(Integer, default=1)
    blocked_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    bid_date: Mapped[date] = mapped_column(Date, nullable=False)
    unblock_date: Mapped[date | None] = mapped_column(Date)
    allotment_status: Mapped[str] = mapped_column(String(30), default="APPLIED")
    listing_profit_realized: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ipo: Mapped[Ipo] = relationship(back_populates="applications")
    pan: Mapped[PanAccountRow] = relationship(back_populates="applications")

    __table_args__ = (
        # Rule 1, enforced by the database and not only by the scheduler (D10).
        UniqueConstraint("ipo_id", "pan_id", name="ipo_applications_one_per_pan"),
        CheckConstraint("lots_applied = 1", name="ipo_applications_single_lot"),
        CheckConstraint("blocked_amount > 0", name="ipo_applications_amount_positive"),
        CheckConstraint(
            "allotment_status IN ('APPLIED', 'ALLOTTED', 'NOT_ALLOTTED', 'UNBLOCKED')",
            name="ipo_applications_status",
        ),
        Index("ipo_applications_pan_idx", "pan_id"),
    )


class ConversationMemory(Base):
    """Copilot transcript. The ``embedding`` column is PostgreSQL/pgvector only.

    On SQLite it is stored as text and semantic recall is simply unavailable, which
    is why the local build has no vector search rather than a fake one.
    """

    __tablename__ = "conversation_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    insight_extracted: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'tool', 'system')", name="conversation_memories_role"
        ),
        Index("conversation_memories_session_idx", "user_id", "session_id"),
    )


# ─────────────────────────────────────────────────────────────────── engine wiring

engine = create_async_engine(settings.database_url, echo=settings.sql_echo)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def create_schema() -> None:
    """Build the dev schema. PostgreSQL uses migrations/001_init.sql instead."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
