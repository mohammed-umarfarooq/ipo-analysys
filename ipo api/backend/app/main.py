"""FastAPI application.

    uv run uvicorn app.main:app --reload --port 8000

No route takes an identity from the caller. There is no authentication in this
build, so the one mitigation available is to give the API no parameter that
*could* be abused to reach someone else's portfolio: the user is resolved from
server state, never from the request. That now covers writes as well as reads —
see the ``writes`` section below and SECURITY.md.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain import AllotmentAssumption, ScheduleResult, SchedulingPolicy
from app.models import (
    CashMovement,
    Ipo,
    IpoApplication,
    PanAccountRow,
    SessionLocal,
    UserProfile,
    create_schema,
    engine,
)
from app.providers.gmp_live import GmpUnavailable, fetch_live_gmp
from app.providers.nse import NseUnavailable, fetch_issues
from app.repository import (
    InsufficientFunds,
    add_pan,
    apply_live_gmp,
    apply_movement,
    count_applications_for_ipo,
    count_applications_for_pan,
    derive_gmp_percent,
    ensure_seeded,
    fill_estimated_dates,
    load_application,
    load_applications,
    load_ipo,
    load_ipos,
    load_movements,
    load_pan,
    load_sample_data,
    load_user,
    reverse_movement,
    set_balance,
    upsert_imported_issues,
)
from app.schemas import (
    ApplicationOut,
    ApplicationPatch,
    ComparisonOut,
    GmpRefreshOut,
    HealthOut,
    ImportSummaryOut,
    IpoOut,
    IpoPatch,
    IpoWrite,
    MovementCreate,
    MovementOut,
    PanCreate,
    PanLedgerOut,
    PanOut,
    PanPatch,
    ScheduleRequest,
    SkippedImportOut,
    UserPatch,
    UserStateOut,
)
from app.service import (
    NoCapitalError,
    build_schedule,
    commit_schedule,
    compare_policies,
    pan_summary,
    rank_ipos,
)

ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Build the dev schema and make sure a user profile exists, then hand over.

    It deliberately does **not** create PANs or IPOs. The first run is an empty
    portfolio you fill in; fixtures live behind ``POST /api/demo/sample-data``.
    On PostgreSQL ``migrations/001_init.sql`` owns the schema; ``create_schema``
    is a no-op against tables that already exist.
    """
    await create_schema()
    async with SessionLocal() as session:
        await ensure_seeded(session)
    yield
    await engine.dispose()


app = FastAPI(
    title="IPO Copilot & Cashflow Scheduler",
    version="0.2.0",
    summary="Plans ASBA bids across PAN accounts to capture the most grey-market premium.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Explicit list, not "*": the responses carry balances and masked PANs.
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    # PATCH and DELETE are needed by the editable rail: a browser preflights both,
    # and omitting them fails the write at the CORS layer rather than the endpoint.
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


# ──────────────────────────────────────────────────────────────── dependencies


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def current_user(session: AsyncSession = Depends(get_session)) -> UserProfile:
    """Resolve the acting user from server state.

    This is a placeholder for real authentication, not a substitute for it. The
    important property is the shape: no endpoint accepts a ``user_id``, so no
    endpoint can be made to read another user's data by changing a parameter.
    Replacing this with a session/JWT lookup is the whole migration.
    """
    user = await load_user(session)
    if user is None:  # pragma: no cover - lifespan seeds one
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "no user profile has been provisioned"
        )
    return user


def _resolve(req: ScheduleRequest) -> tuple[AllotmentAssumption, date, Decimal]:
    min_gmp = settings.min_gmp if req.min_gmp is None else req.min_gmp
    return req.assumption, req.start_date or date.today(), min_gmp


# ──────────────────────────────────────────────────────────────────── routes


@app.get("/api/health", response_model=HealthOut, tags=["meta"])
async def health() -> HealthOut:
    return HealthOut(
        status="ok",
        database="sqlite" if settings.is_sqlite else "postgresql",
        gmp_provider=settings.gmp_provider,
        scheduler_default_policy=SchedulingPolicy.VALUE_FIRST.value,
        production_warnings=settings.validate_for_production(),
    )


@app.get("/api/user/state", response_model=UserStateOut, tags=["portfolio"])
async def user_state(
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> UserStateOut:
    """Capital position across PAN accounts.

    ``liquid_capital`` is derived by summing active PANs rather than stored, so
    it cannot disagree with the per-account balances (D4).
    """
    applications = await load_applications(session, user.id)
    return UserStateOut(
        id=user.id,
        name=user.name,
        liquid_capital=float(user.liquid_capital),
        demat_balance=float(user.total_demat_balance),
        capital_mode=user.capital_mode,
        pans=[pan_summary(row) for row in user.pans],
        active_pan_count=sum(1 for row in user.pans if row.is_active),
        committed_application_count=len(applications),
    )


@app.get("/api/ipos", response_model=list[IpoOut], tags=["ipos"])
async def list_ipos(session: AsyncSession = Depends(get_session)) -> list[IpoOut]:
    """The IPO universe with Rule 3 rank and the capital each lot would freeze."""
    rows = await load_ipos(session)
    ranks = rank_ipos(rows)
    return [_ipo_out(row, ranks.get(row.id)) for row in rows]


def _ipo_out(row: Ipo, rank: int | None) -> IpoOut:
    """Serialise one issue, including why it may not be plannable.

    Shared by the list and the write endpoints so a row never looks different
    depending on which call returned it.
    """
    schedulable = row.allotment_date is not None
    missing: list[str] = []
    if row.allotment_date is None:
        missing.append("allotment date")
    elif row.dates_estimated:
        # Not "missing" in the sense that blocks planning, but the UI should say so:
        # the plan is built on close + 1 working day, not on the registrar's word.
        missing.append("allotment date (estimated)")
    if row.latest_gmp <= 0:
        missing.append("GMP")
    if row.needs_review:
        missing.append("lot size (estimated)")

    if not schedulable:
        note = "allotment date not fixed by the registrar yet"
    elif row.dates_estimated:
        note = "allotment and listing dates estimated from the close date (SEBI T+3)"
    else:
        note = None

    return IpoOut(
        id=row.id,
        name=row.name,
        symbol=row.symbol,
        issue_type=row.issue_type,
        min_price=float(row.min_price),
        max_price=float(row.max_price),
        # Retail bids at cut-off, so this is what actually gets blocked (D7).
        cutoff_price=float(row.max_price),
        lot_size=row.lot_size,
        lot_cost=float(row.max_price * row.lot_size),
        latest_gmp=float(row.latest_gmp),
        gmp_percent=float(row.gmp_percent),
        expected_profit_per_lot=float(row.max_price * row.lot_size * row.gmp_percent / 100),
        open_date=row.open_date,
        close_date=row.close_date,
        allotment_date=row.allotment_date,
        unblock_date=(row.allotment_date + timedelta(days=1) if row.allotment_date else None),
        listing_date=row.listing_date,
        allotment_probability=float(row.allotment_probability),
        priority_rank=rank,
        schedulable=schedulable,
        source=row.source,
        needs_review=row.needs_review,
        gmp_source=row.gmp_source,
        dates_estimated=row.dates_estimated,
        note=note,
        missing=missing,
    )


@app.post("/api/schedule", response_model=ScheduleResult, tags=["schedule"])
async def schedule(
    req: ScheduleRequest,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> ScheduleResult:
    """Plan bids. Every IPO appears in ``events`` or in ``skipped``, never neither."""
    assumption, start_date, min_gmp = _resolve(req)
    try:
        return await build_schedule(
            session,
            user,
            policy=req.policy,
            assumption=assumption,
            min_gmp=min_gmp,
            start_date=start_date,
        )
    except NoCapitalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@app.post("/api/schedule/compare", response_model=ComparisonOut, tags=["schedule"])
async def compare(
    req: ScheduleRequest,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> ComparisonOut:
    """Both policies on identical inputs. ``policy`` in the body is ignored here."""
    assumption, start_date, min_gmp = _resolve(req)
    try:
        payload = await compare_policies(
            session, user, assumption=assumption, min_gmp=min_gmp, start_date=start_date
        )
    except NoCapitalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return ComparisonOut(**payload)


@app.post("/api/schedule/commit", tags=["schedule"])
async def commit(
    req: ScheduleRequest,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> dict[str, int]:
    """Record a plan as applications. Re-committing the same plan is a no-op."""
    assumption, start_date, min_gmp = _resolve(req)
    try:
        result = await build_schedule(
            session,
            user,
            policy=req.policy,
            assumption=assumption,
            min_gmp=min_gmp,
            start_date=start_date,
        )
    except NoCapitalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await commit_schedule(session, user, result)


@app.get("/api/portfolio/history", response_model=list[ApplicationOut], tags=["portfolio"])
async def portfolio_history(
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> list[ApplicationOut]:
    """Committed applications. This is what the copilot's history tool reads."""
    return [
        _application_out(app_row) for app_row in await load_applications(session, user.id)
    ]


# ──────────────────────────────────────────────────────────────────────── writes
#
# Everything below mutates. Three rules hold across all of it:
#
# 1. No endpoint takes a ``user_id``. Identity is ``Depends(current_user)``, exactly
#    as on the read side, so there is no parameter to change in order to write into
#    another portfolio. ``TestNoIdentityFromTheCaller`` covers these too.
# 2. A PAN id that is not this user's is **404, not 403** — a 403 would confirm the
#    row exists, turning the endpoint into an existence oracle.
# 3. None of these are exposed as copilot tools. Model input is attacker-controlled
#    wherever prompt injection is possible, so writes stay behind a human. See
#    SECURITY.md; that property is load-bearing and survives this change.


async def _pan_or_404(session: AsyncSession, user: UserProfile, pan_id: str) -> PanAccountRow:
    pan = await load_pan(session, user.id, pan_id)
    if pan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such PAN account")
    return pan


async def _ipo_or_404(session: AsyncSession, ipo_id: str) -> Ipo:
    ipo = await load_ipo(session, ipo_id)
    if ipo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such IPO")
    return ipo


def _apply_gmp(ipo: Ipo, latest_gmp: Decimal) -> None:
    """Store the rupee premium and re-derive the percentage from it.

    Never accept ``gmp_percent`` from a caller: the UI shows rupees while Rule 3 ranks
    on the percentage, so two independent inputs would let the ranking disagree with
    the number on screen.
    """
    ipo.latest_gmp = latest_gmp
    ipo.gmp_percent = derive_gmp_percent(latest_gmp, ipo.max_price)


@app.patch("/api/user", response_model=UserStateOut, tags=["portfolio"])
async def update_user(
    patch: UserPatch,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> UserStateOut:
    if patch.name is not None:
        user.name = patch.name
    if patch.demat_balance is not None:
        user.total_demat_balance = patch.demat_balance
    if patch.capital_mode is not None:
        user.capital_mode = patch.capital_mode
    await session.commit()
    return await user_state(session, user)


@app.post("/api/pans", response_model=PanOut, status_code=201, tags=["portfolio"])
async def create_pan(
    body: PanCreate,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> PanOut:
    """Add a PAN account.

    The submitted PAN is hashed and masked in :func:`app.repository.add_pan` and is
    absent from the response model, so it cannot leak through this endpoint even by
    accident. A duplicate is a 409: ``pan_hash`` is unique, and the salted hash is the
    only way to notice the same PAN twice without keeping the number.
    """
    try:
        pan = await add_pan(
            session,
            user,
            holder_name=body.holder_name,
            relation=body.relation,
            pan_number=body.pan_number,
            upi_id=body.upi_id,
            opening_balance=body.opening_balance,
            linked_bank_name=body.linked_bank_name,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "that PAN is already linked to an account"
        ) from exc
    return PanOut(**pan_summary(pan))


@app.patch("/api/pans/{pan_id}", response_model=PanOut, tags=["portfolio"])
async def update_pan(
    pan_id: str,
    patch: PanPatch,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> PanOut:
    pan = await _pan_or_404(session, user, pan_id)

    for field in ("holder_name", "relation", "upi_id", "linked_bank_name", "is_active"):
        value = getattr(patch, field)
        if value is not None:
            setattr(pan, field, value)

    if patch.balance is not None:
        try:
            await set_balance(session, pan, target=patch.balance)
        except InsufficientFunds as exc:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await session.commit()
    return PanOut(**pan_summary(pan))


@app.delete("/api/pans/{pan_id}", tags=["portfolio"])
async def delete_pan(
    pan_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> dict[str, str]:
    """Remove a PAN account, unless bids were placed from it.

    ``ipo_applications.pan_id`` cascades, so deleting a PAN with history would take
    the record of those bids with it silently. Refusing is the safe answer, and
    deactivating achieves what the user actually wants — the scheduler ignores
    inactive PANs, so the capital leaves the plan while the history survives.
    """
    pan = await _pan_or_404(session, user, pan_id)
    committed = await count_applications_for_pan(session, pan_id)
    if committed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{pan.holder_name} has {committed} committed application(s); deleting the "
            f"account would erase them. Deactivate it instead to take it out of planning.",
        )
    await session.delete(pan)
    await session.commit()
    return {"deleted": pan_id}


@app.get("/api/pans/{pan_id}/movements", response_model=PanLedgerOut, tags=["portfolio"])
async def pan_ledger(
    pan_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> PanLedgerOut:
    """The fund ledger for one account, with a running balance per row.

    The running balance is computed here rather than in the browser because it must
    agree with ``available_balance``, and that agreement is the invariant this whole
    ledger exists to keep (D18).
    """
    pan = await _pan_or_404(session, user, pan_id)
    movements = await load_movements(session, pan_id)

    # Rows arrive newest-first; the running balance has to be accumulated oldest-first
    # and then flipped back, or every figure would be off by the rest of the ledger.
    running = Decimal("0.00")
    balances: dict[str, Decimal] = {}
    for movement in reversed(movements):
        running += movement.signed_amount
        balances[movement.id] = running

    return PanLedgerOut(
        pan_id=pan.id,
        holder_name=pan.holder_name,
        pan_masked=pan.pan_masked,
        available_balance=float(pan.available_balance),
        movements=[
            MovementOut(
                id=m.id,
                kind=m.kind,
                amount=float(m.amount),
                signed_amount=float(m.signed_amount),
                note=m.note,
                occurred_on=m.occurred_on,
                balance_after=float(balances[m.id]),
            )
            for m in movements
        ],
    )


@app.post(
    "/api/pans/{pan_id}/movements",
    response_model=PanLedgerOut,
    status_code=201,
    tags=["portfolio"],
)
async def create_movement(
    pan_id: str,
    body: MovementCreate,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> PanLedgerOut:
    """Add money or take it out. Returns the whole ledger so the UI needs one call."""
    pan = await _pan_or_404(session, user, pan_id)
    try:
        await apply_movement(
            session,
            pan,
            kind=body.kind,
            amount=body.amount,
            note=body.note,
            occurred_on=body.occurred_on,
        )
    except InsufficientFunds as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    return await pan_ledger(pan_id, session, user)


@app.delete("/api/movements/{movement_id}", response_model=PanLedgerOut, tags=["portfolio"])
async def delete_movement(
    movement_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> PanLedgerOut:
    """Reverse one ledger entry, unwinding the balance with it."""
    movement = await session.get(CashMovement, movement_id)
    if movement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such movement")
    # Ownership is checked through the PAN, so a movement id belonging to another
    # user's account is indistinguishable from one that does not exist.
    pan = await _pan_or_404(session, user, movement.pan_id)
    try:
        await reverse_movement(session, movement)
    except InsufficientFunds as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    return await pan_ledger(pan.id, session, user)


@app.post("/api/ipos", response_model=IpoOut, status_code=201, tags=["ipos"])
async def create_ipo(
    body: IpoWrite,
    session: AsyncSession = Depends(get_session),
    _: UserProfile = Depends(current_user),
) -> IpoOut:
    ipo = Ipo(
        name=body.name,
        symbol=body.symbol,
        issue_type=body.issue_type,
        min_price=body.min_price,
        max_price=body.max_price,
        lot_size=body.lot_size,
        open_date=body.open_date,
        close_date=body.close_date,
        allotment_date=body.allotment_date,
        listing_date=body.listing_date,
        registrar_name=body.registrar_name,
        allotment_probability=body.allotment_probability,
        source="user",
        needs_review=False,
    )
    _apply_gmp(ipo, body.latest_gmp)
    # An issue added by hand before the registrar has published gets the same T+3
    # estimate an imported one does, so it is plannable from the moment it exists.
    fill_estimated_dates(ipo)
    session.add(ipo)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "an issue with that name or symbol already exists"
        ) from exc
    return _ipo_out(ipo, None)


@app.patch("/api/ipos/{ipo_id}", response_model=IpoOut, tags=["ipos"])
async def update_ipo(
    ipo_id: str,
    patch: IpoPatch,
    session: AsyncSession = Depends(get_session),
    _: UserProfile = Depends(current_user),
) -> IpoOut:
    """Edit an issue, and mark it as the user's from then on.

    Two things happen beyond the field assignment:

    * The merged row is re-validated through :class:`IpoWrite`, because a partial
      patch cannot be checked on its own — sending only ``close_date`` says nothing
      about the stored ``allotment_date``, and the pair is what has to be consistent.
    * ``source`` becomes ``'user'`` and ``needs_review`` clears, which is what stops a
      later refresh from NSE overwriting this edit. That is the whole mechanism.
    """
    ipo = await _ipo_or_404(session, ipo_id)
    fields = patch.model_dump(exclude_unset=True)

    merged = {
        "name": ipo.name,
        "symbol": ipo.symbol,
        "issue_type": ipo.issue_type,
        "min_price": ipo.min_price,
        "max_price": ipo.max_price,
        "lot_size": ipo.lot_size,
        "latest_gmp": ipo.latest_gmp,
        "open_date": ipo.open_date,
        "close_date": ipo.close_date,
        "allotment_date": ipo.allotment_date,
        "listing_date": ipo.listing_date,
        "registrar_name": ipo.registrar_name,
        "allotment_probability": ipo.allotment_probability,
        **fields,
    }
    try:
        validated = IpoWrite(**merged)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc

    for field, value in validated.model_dump(exclude={"latest_gmp"}).items():
        setattr(ipo, field, value)
    _apply_gmp(ipo, validated.latest_gmp)

    # An edited row is the user's, whatever it was before.
    ipo.source = "user"
    ipo.needs_review = False

    # Provenance is per-field, so it is cleared per-field. Touching a date confirms
    # both dates; touching the premium makes it a typed figure rather than a scraped
    # one. Relabelling either on an unrelated edit — a lot-size fix, say — would be a
    # claim about where the number came from that is simply untrue.
    if "allotment_date" in fields or "listing_date" in fields:
        ipo.dates_estimated = False
    if "latest_gmp" in fields:
        ipo.gmp_source = "user"

    # If the edit left the row without an allotment date, estimate one rather than
    # dropping the issue out of every plan. Requirement 4: no confirmed date is not a
    # reason to be unschedulable.
    fill_estimated_dates(ipo)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "an issue with that name or symbol already exists"
        ) from exc

    ranks = rank_ipos(await load_ipos(session))
    return _ipo_out(ipo, ranks.get(ipo.id))


@app.delete("/api/ipos/{ipo_id}", tags=["ipos"])
async def delete_ipo(
    ipo_id: str,
    session: AsyncSession = Depends(get_session),
    _: UserProfile = Depends(current_user),
) -> dict[str, str]:
    """Remove an issue, unless bids were committed against it.

    ``ipo_applications.ipo_id`` is ``ON DELETE CASCADE`` with ``delete-orphan`` on the
    relationship, so a plain delete here would silently destroy the record of money
    already committed. The constraint is right for tearing down test data and wrong as
    a UI affordance, so this endpoint checks first.
    """
    ipo = await _ipo_or_404(session, ipo_id)
    committed = await count_applications_for_ipo(session, ipo_id)
    if committed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{ipo.name} has {committed} committed application(s); deleting it would "
            f"erase them. Remove those bids first if that is really what you want.",
        )
    await session.delete(ipo)
    await session.commit()
    return {"deleted": ipo_id}


@app.post("/api/ipos/import", response_model=ImportSummaryOut, tags=["ipos"])
async def import_ipos(
    session: AsyncSession = Depends(get_session),
    _: UserProfile = Depends(current_user),
) -> ImportSummaryOut:
    """Pull the live issue calendar from NSE.

    Partial by construction, and the response says so. NSE publishes the name, symbol,
    series, price band and dates; it publishes no lot size, no allotment date and no
    GMP. So each imported row arrives with an estimated lot size, no allotment date —
    which the scheduler already reports as unplannable with a reason — and a zero
    premium until someone types the real one. See D17.
    """
    if settings.ipo_import_source != "nse":
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            f"IPO_IMPORT_SOURCE is {settings.ipo_import_source!r}; live import is off",
        )
    try:
        issues, skipped = await fetch_issues()
    except NseUnavailable as exc:
        # 502, not 500: the fault is upstream and the message says what to do instead.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    result = await upsert_imported_issues(session, issues)
    return ImportSummaryOut(
        source="nse",
        imported=int(result["imported"]),
        updated=int(result["updated"]),
        skipped=[SkippedImportOut(name=s.name, reason=s.reason) for s in skipped],
        unchanged_because_edited=list(result["unchanged_because_edited"]),
        needs_review=int(result["imported"]),
        note=(
            "NSE does not publish lot size, allotment date or GMP. Lot sizes are "
            "estimated from the SEBI minimum application value and every imported "
            "issue needs those three fields confirmed before it can be planned."
        ),
    )


@app.post("/api/ipos/refresh-gmp", response_model=GmpRefreshOut, tags=["ipos"])
async def refresh_gmp(
    session: AsyncSession = Depends(get_session),
    _: UserProfile = Depends(current_user),
) -> GmpRefreshOut:
    """Pull grey-market premiums from a public aggregator.

    User-triggered, exactly like the NSE import, and for the same reason: it reaches a
    third party whose page can change or vanish, so it must be something a person asks
    for and can see the result of — not a background job that silently rewrites the
    numbers a plan was built on.

    Two rules this must keep:

    * **A premium the user typed is never overwritten.** GMP has no official source;
      the human's figure is the authoritative one and the scrape is a convenience.
    * **This is not a copilot tool.** It is a write endpoint reaching an untrusted
      third party, and the copilot's tools are read-only (see SECURITY.md).
    """
    try:
        premiums = await fetch_live_gmp()
    except GmpUnavailable as exc:
        # 502, not 500: the fault is upstream, and manual entry still works.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    result = await apply_live_gmp(session, premiums)
    return GmpRefreshOut(
        source=settings.gmp_live_url,
        updated=list(result["updated"]),
        unchanged_because_edited=list(result["unchanged_because_edited"]),
        unmatched=list(result["unmatched"]),
        quotes_seen=int(result["quotes_seen"]),
        disclaimer=(
            "Grey-market premium is an unofficial, unregulated indication collected "
            "from dealer quotes. No exchange or regulator publishes it. Treat it as "
            "sentiment, not a price, and override it whenever you know better."
        ),
    )


#: ``allotment_status`` on the wire is a tri-state tick box. ``UNBLOCKED`` is a bid whose
#: money came back without an allotment, so it reads as "not allotted" rather than as
#: "not known" — the outcome is settled even though the label is about the cash.
_ALLOTTED_BY_STATUS = {"ALLOTTED": True, "NOT_ALLOTTED": False, "UNBLOCKED": False}
_STATUS_BY_ALLOTTED = {True: "ALLOTTED", False: "NOT_ALLOTTED", None: "APPLIED"}


def _application_out(row: IpoApplication) -> ApplicationOut:
    return ApplicationOut(
        id=row.id,
        ipo_name=row.ipo.name,
        pan_holder=row.pan.holder_name,
        pan_masked=row.pan.pan_masked,
        lots_applied=row.lots_applied,
        blocked_amount=float(row.blocked_amount),
        bid_date=row.bid_date,
        unblock_date=row.unblock_date,
        allotment_status=row.allotment_status,
        allotted=_ALLOTTED_BY_STATUS.get(row.allotment_status),
    )


@app.patch(
    "/api/applications/{application_id}",
    response_model=ApplicationOut,
    tags=["portfolio"],
)
async def update_application(
    application_id: str,
    patch: ApplicationPatch,
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> ApplicationOut:
    """Tick whether an application was allotted.

    The registrar's result arrives days after the bid and is published nowhere this
    program can read, so it is typed in. Recording it is what turns the plan into a
    history: an allotted bid consumed its capital and will list, a rejected one gave the
    money back at T+1.

    A missing application id is a 404 even when the row exists under another user, for
    the reason given on :func:`app.repository.load_application`.
    """
    row = await load_application(session, user.id, application_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "application not found")

    row.allotment_status = _STATUS_BY_ALLOTTED[patch.allotted]
    await session.commit()
    return _application_out(row)


@app.post("/api/demo/sample-data", tags=["meta"])
async def sample_data(
    session: AsyncSession = Depends(get_session),
    user: UserProfile = Depends(current_user),
) -> dict[str, int]:
    """Insert the illustrative PANs and fixture calendar, on request.

    This used to run at every startup, which is what made the app look like a fixed
    demo. It is now something asked for once: rows are marked ``source='sample'``, and
    deleting them makes them stay deleted.
    """
    return await load_sample_data(session, user)
