"""Request and response shapes for the HTTP layer.

Deliberately separate from :mod:`app.domain`: the domain types are the engine's
contract and should not change shape because an endpoint wants a different field.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain import AllotmentAssumption, ScheduleResult, SchedulingPolicy


class ScheduleRequest(BaseModel):
    """Knobs for a planning run. Every field has a sane default.

    Note there is no ``user_id`` and no ``pans`` — capital and identity come from
    the server's own state, never from the request body. See SECURITY.md.
    """

    policy: SchedulingPolicy = SchedulingPolicy.VALUE_FIRST
    assumption: AllotmentAssumption = AllotmentAssumption.EXPECTED
    min_gmp: Decimal | None = Field(
        default=None, description="Override the configured GMP% floor."
    )
    start_date: date | None = Field(
        default=None, description="Plan from this date. Defaults to today."
    )


class PanOut(BaseModel):
    id: str
    holder_name: str
    relation: str
    pan_masked: str
    upi_id: str
    available_balance: float
    is_active: bool


class UserStateOut(BaseModel):
    id: str
    name: str
    liquid_capital: float
    demat_balance: float
    #: ``pooled`` plans ``liquid_capital`` as one war-chest any PAN may draw on;
    #: ``per_pan`` ring-fences each holder's balance. See ``CapitalMode``.
    capital_mode: str
    pans: list[PanOut]
    active_pan_count: int
    committed_application_count: int


class IpoOut(BaseModel):
    id: str
    name: str
    symbol: str | None
    issue_type: str
    min_price: float
    max_price: float
    cutoff_price: float
    lot_size: int
    lot_cost: float
    latest_gmp: float
    gmp_percent: float
    expected_profit_per_lot: float
    open_date: date
    close_date: date
    allotment_date: date | None
    unblock_date: date | None
    listing_date: date | None
    allotment_probability: float
    priority_rank: int | None
    schedulable: bool
    source: str
    needs_review: bool
    #: Where ``latest_gmp`` came from: ``user`` typed it (authoritative) or ``live``
    #: pulled it from a grey-market aggregator (unofficial, unregulated).
    gmp_source: str
    #: True when allotment/listing were estimated under SEBI T+3 rather than published
    #: by the registrar. Both fields are server-derived and so are absent from
    #: ``IpoWrite``/``IpoPatch`` — exactly like ``gmp_percent``.
    dates_estimated: bool
    note: str | None = None
    #: What is still missing before this issue can be planned at all. Empty for a
    #: complete row. Sent as data rather than composed in the browser so the reason
    #: an issue is unplannable is decided in one place.
    missing: list[str] = Field(default_factory=list)


class ApplicationOut(BaseModel):
    id: str
    ipo_name: str
    pan_holder: str
    pan_masked: str
    lots_applied: int
    blocked_amount: float
    bid_date: date
    unblock_date: date | None
    allotment_status: str
    #: ``allotment_status`` as the tick-box the UI actually draws: ``True`` allotted,
    #: ``False`` not allotted, ``None`` not known yet. Derived server-side so the browser
    #: never has to know which of the four stored statuses count as "allotted".
    allotted: bool | None = None


class ComparisonOut(BaseModel):
    """The D1 finding as an API response."""

    value_first: ScheduleResult
    jit_greedy: ScheduleResult
    delta_expected_profit: float
    capital_constrained: bool


class HealthOut(BaseModel):
    status: str
    database: str
    gmp_provider: str
    scheduler_default_policy: str
    production_warnings: list[str]


# ─────────────────────────────────────────────────────────────── write requests
#
# None of these carry a ``user_id``. Identity comes from ``current_user`` on the
# server, so there is no parameter a caller can change to write into someone else's
# portfolio. ``tests/test_api.py::TestNoIdentityFromTheCaller`` enforces that by
# reading ``/openapi.json``, and it covers these endpoints too.
#
# Every constraint the database enforces is mirrored here on purpose. A CHECK
# violation surfaces as a driver ``IntegrityError`` and a 500; the same rule stated
# in pydantic produces a 422 naming the field, which is the difference between "the
# server broke" and "the allotment date cannot precede the close date".

#: A PAN is five letters, four digits, a letter. Validated so a typo is caught before
#: it becomes an unrecoverable hash — the number is not stored, so it cannot be
#: checked afterwards.
PAN_PATTERN = r"^[A-Z]{5}[0-9]{4}[A-Z]$"


class UserPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    demat_balance: Decimal | None = Field(default=None, ge=0)
    #: A ``Literal`` rather than a free string so an unknown mode is a 422 at the edge
    #: instead of a CHECK-constraint failure or, worse, a silently per-PAN plan.
    capital_mode: Literal["pooled", "per_pan"] | None = None


class PanCreate(BaseModel):
    """A new PAN account.

    ``pan_number`` is the only place a plaintext PAN enters the system. It is hashed
    and masked inside :func:`app.repository.add_pan` and never stored, logged or
    returned — the response is a :class:`PanOut`, which has no field for it. See D11.
    """

    holder_name: str = Field(min_length=1, max_length=100)
    relation: str = Field(default="Self", max_length=50)
    pan_number: str = Field(pattern=PAN_PATTERN, description="Not stored; hashed and masked.")
    upi_id: str = Field(min_length=3, max_length=100)
    linked_bank_name: str | None = Field(default=None, max_length=100)
    opening_balance: Decimal = Field(default=Decimal("0.00"), ge=0)

    @field_validator("pan_number", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class PanPatch(BaseModel):
    """Editable PAN fields. The number itself is not among them.

    Correcting a mistyped PAN means deleting the account and adding it again, because
    only a one-way hash was kept — there is nothing to compare a correction against.
    """

    holder_name: str | None = Field(default=None, min_length=1, max_length=100)
    relation: str | None = Field(default=None, max_length=50)
    upi_id: str | None = Field(default=None, min_length=3, max_length=100)
    linked_bank_name: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None
    #: Sets the balance to this figure by recording the difference as a movement, so
    #: the ledger still explains it. See :func:`app.repository.set_balance`.
    balance: Decimal | None = Field(default=None, ge=0)


class MovementCreate(BaseModel):
    kind: Literal["DEPOSIT", "WITHDRAWAL"]
    amount: Decimal = Field(gt=0)
    note: str | None = Field(default=None, max_length=140)
    occurred_on: date | None = None


class MovementOut(BaseModel):
    id: str
    kind: str
    amount: float
    #: The same figure with its direction applied, so the UI does not have to know
    #: which kinds are debits to render a running balance.
    signed_amount: float
    note: str | None
    occurred_on: date
    balance_after: float | None = None


class PanLedgerOut(BaseModel):
    pan_id: str
    holder_name: str
    pan_masked: str
    #: Cash in the account, **not** cash net of pending ASBA blocks. The engine
    #: measures peak load against this figure; the PAN ledger panel shows headroom.
    available_balance: float
    movements: list[MovementOut]


class IpoWrite(BaseModel):
    """Create or replace an issue.

    ``gmp_percent`` is absent by design: the caller supplies the rupee premium in
    ``latest_gmp`` and the server derives the percentage
    (:func:`app.repository.derive_gmp_percent`). Both are stored columns read by
    different callers — the UI shows rupees, Rule 3 ranks on the percentage — so
    accepting both is how they come to disagree.
    """

    name: str = Field(min_length=1, max_length=150)
    symbol: str | None = Field(default=None, max_length=50)
    issue_type: Literal["Mainboard", "SME"] = "Mainboard"
    min_price: Decimal = Field(gt=0)
    max_price: Decimal = Field(gt=0)
    lot_size: int = Field(gt=0)
    latest_gmp: Decimal = Field(default=Decimal("0.00"), ge=0)
    open_date: date
    close_date: date
    allotment_date: date | None = None
    listing_date: date | None = None
    registrar_name: str | None = Field(default=None, max_length=100)
    allotment_probability: Decimal = Field(default=Decimal("0.000"), ge=0, le=1)

    @model_validator(mode="after")
    def _check_ordering(self) -> IpoWrite:
        if self.min_price > self.max_price:
            raise ValueError("min_price cannot exceed max_price")
        if self.open_date > self.close_date:
            raise ValueError("open_date cannot be after close_date")
        if self.allotment_date is not None and self.allotment_date < self.close_date:
            raise ValueError("allotment_date cannot precede close_date")
        if (
            self.listing_date is not None
            and self.allotment_date is not None
            and self.listing_date < self.allotment_date
        ):
            raise ValueError("listing_date cannot precede allotment_date")
        return self


class IpoPatch(BaseModel):
    """Partial update. Same rules as :class:`IpoWrite`, checked against the merged row.

    Field-level ordering cannot be validated here — sending only ``close_date`` says
    nothing about the stored ``allotment_date`` — so :func:`app.main.update_ipo`
    re-validates the merged result through ``IpoWrite`` before saving. That way one
    set of rules covers both endpoints.
    """

    name: str | None = Field(default=None, min_length=1, max_length=150)
    symbol: str | None = Field(default=None, max_length=50)
    issue_type: Literal["Mainboard", "SME"] | None = None
    min_price: Decimal | None = Field(default=None, gt=0)
    max_price: Decimal | None = Field(default=None, gt=0)
    lot_size: int | None = Field(default=None, gt=0)
    latest_gmp: Decimal | None = Field(default=None, ge=0)
    open_date: date | None = None
    close_date: date | None = None
    allotment_date: date | None = None
    listing_date: date | None = None
    registrar_name: str | None = Field(default=None, max_length=100)
    allotment_probability: Decimal | None = Field(default=None, ge=0, le=1)


class ApplicationPatch(BaseModel):
    """Record what the registrar decided about one application.

    A three-state field rather than a plain boolean: "not allotted" and "not known yet"
    are different facts, and only the first one means the money is gone. Sending ``null``
    puts the row back to ``APPLIED``, so ticking the box by mistake is undoable.

    ``allotment_status`` itself is not accepted — ``UNBLOCKED`` is bookkeeping the server
    derives from the T+1 date, not something a user chooses.

    The field is required even though it is nullable, so an empty body is a 422 rather
    than a silent reset: with a default, ``{}`` and ``{"allotted": null}`` would be the
    same request, and only one of them is something a caller meant.
    """

    allotted: bool | None


class SkippedImportOut(BaseModel):
    name: str
    reason: str


class GmpRefreshOut(BaseModel):
    """What a live GMP refresh changed, and what it deliberately did not.

    Grey-market premium is unofficial and unregulated — ``disclaimer`` says so on every
    response rather than only in the UI, so a caller cannot present these numbers as
    exchange data by accident.
    """

    source: str
    #: Issues whose premium was written from the aggregator.
    updated: list[str]
    #: Issues left alone because the user had typed their own premium. Their edit wins.
    unchanged_because_edited: list[str]
    #: Stored issues the aggregator had no quote for.
    unmatched: list[str]
    #: How many premiums the page carried, matched or not — a sanity signal for the user.
    quotes_seen: int
    disclaimer: str


class ImportSummaryOut(BaseModel):
    """What an import did, including what it could not do.

    ``skipped`` and ``unchanged_because_edited`` are reported rather than swallowed:
    a count of successes alone would make a partially-understood feed look complete.
    """

    source: str
    imported: int
    updated: int
    skipped: list[SkippedImportOut]
    unchanged_because_edited: list[str]
    #: Every imported issue needs review, because NSE publishes no lot size, no
    #: allotment date and no GMP. Surfaced as a number so the UI can nag.
    needs_review: int
    note: str
