"""Database access and demo seeding.

The seam between persistence and the pure engine. Everything here converts
SQLAlchemy rows to the frozen domain types in :mod:`app.domain`, so the scheduler
never sees an ORM object and stays independently testable.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.dates import estimated_dates
from app.domain import IPOTask, IssueType, PanAccount, money
from app.models import (
    CashMovement,
    Ipo,
    IpoApplication,
    PanAccountRow,
    UserProfile,
    hash_pan,
    mask_pan,
)
from app.providers.gmp import SeededProvider
from app.providers.gmp_live import normalise_name
from app.providers.nse import ImportedIssue

#: Sample PANs for ``POST /api/demo/sample-data``. Fabricated, opt-in, and never
#: created automatically — see :func:`ensure_seeded`. Only the salted hash and the
#: ``ABCDE****F`` mask reach the database (D11).
_DEMO_PANS: tuple[tuple[str, str, str, str, str], ...] = (
    ("Mohammed", "Self", "ABCDE1234F", "mohammed@okhdfc", "180000"),
    ("Aisha", "Mother", "BCDEF2345G", "aisha@okicici", "95000"),
    ("Rashid", "Father", "CDEFG3456H", "rashid@oksbi", "60000"),
)


# ─────────────────────────────────────────────────────────────────────── reads


async def load_user(session: AsyncSession) -> UserProfile | None:
    """The single local user, with PANs eagerly loaded.

    There is exactly one because there is no authentication yet. See
    :func:`app.main.current_user` and SECURITY.md — the identity must never come
    from the request.
    """
    result = await session.execute(
        select(UserProfile).options(selectinload(UserProfile.pans)).limit(1)
    )
    return result.scalar_one_or_none()


async def load_ipos(session: AsyncSession) -> list[Ipo]:
    result = await session.execute(select(Ipo).order_by(Ipo.close_date, Ipo.name))
    return list(result.scalars())


async def load_applications(session: AsyncSession, user_id: str) -> list[IpoApplication]:
    """Committed applications for one user, newest bid first."""
    result = await session.execute(
        select(IpoApplication)
        .join(PanAccountRow, IpoApplication.pan_id == PanAccountRow.id)
        .where(PanAccountRow.user_id == user_id)
        .options(selectinload(IpoApplication.ipo), selectinload(IpoApplication.pan))
        .order_by(IpoApplication.bid_date.desc())
    )
    return list(result.scalars())


async def load_application(
    session: AsyncSession, user_id: str, application_id: str
) -> IpoApplication | None:
    """One committed application, scoped to its owner.

    Scoped through ``pan_accounts.user_id`` for the same reason as :func:`load_pan`: an
    id belonging to someone else must read as "not found", not as "forbidden", which
    would confirm the row exists.
    """
    result = await session.execute(
        select(IpoApplication)
        .join(PanAccountRow, IpoApplication.pan_id == PanAccountRow.id)
        .where(IpoApplication.id == application_id, PanAccountRow.user_id == user_id)
        .options(selectinload(IpoApplication.ipo), selectinload(IpoApplication.pan))
    )
    return result.scalar_one_or_none()


async def load_pan(session: AsyncSession, user_id: str, pan_id: str) -> PanAccountRow | None:

    """One PAN, scoped to its owner.

    The ``user_id`` filter is the whole point: a caller supplying someone else's PAN
    id must get "not found", not that PAN. It reads as belt-and-braces while there is
    a single local user, and it is the line that has to already be there on the day
    authentication arrives.
    """
    result = await session.execute(
        select(PanAccountRow).where(
            PanAccountRow.id == pan_id, PanAccountRow.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def load_movements(session: AsyncSession, pan_id: str) -> list[CashMovement]:
    """The fund ledger for one PAN, newest first."""
    result = await session.execute(
        select(CashMovement)
        .where(CashMovement.pan_id == pan_id)
        .order_by(CashMovement.occurred_on.desc(), CashMovement.created_at.desc())
    )
    return list(result.scalars())


async def load_ipo(session: AsyncSession, ipo_id: str) -> Ipo | None:
    result = await session.execute(
        select(Ipo).where(Ipo.id == ipo_id).options(selectinload(Ipo.applications))
    )
    return result.scalar_one_or_none()


async def count_applications_for_ipo(session: AsyncSession, ipo_id: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(IpoApplication).where(IpoApplication.ipo_id == ipo_id)
    )
    return int(result.scalar_one())


async def count_applications_for_pan(session: AsyncSession, pan_id: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(IpoApplication).where(IpoApplication.pan_id == pan_id)
    )
    return int(result.scalar_one())


# ─────────────────────────────────────────────────────────────── money movements


class InsufficientFunds(ValueError):
    """A withdrawal larger than the balance it would come out of."""


async def apply_movement(
    session: AsyncSession,
    pan: PanAccountRow,
    *,
    kind: str,
    amount: Decimal,
    note: str | None = None,
    occurred_on: date | None = None,
) -> CashMovement:
    """Record a cash movement and move the balance with it. **The only writer.**

    ``available_balance`` is a materialised sum of ``cash_movements`` (D18), and the
    way a materialised sum goes wrong is a second writer. So every path that changes
    a balance — deposit, withdrawal, opening balance, "set balance to X" — comes
    through here, and both rows are written in one transaction.

    Raises :class:`InsufficientFunds` rather than letting the
    ``available_balance >= 0`` CHECK fail, so an overdrawn withdrawal is a 409 the
    UI can explain instead of a 500 from the driver.
    """
    exact = money(amount)
    if exact <= 0:
        raise ValueError("a movement amount must be positive")
    if kind not in CashMovement.DIRECTIONS:
        raise ValueError(f"unknown movement kind {kind!r}")

    delta = exact * CashMovement.DIRECTIONS[kind]
    new_balance = money(pan.available_balance + delta)
    if new_balance < 0:
        raise InsufficientFunds(
            f"{pan.holder_name} has {pan.available_balance} available; "
            f"withdrawing {exact} would overdraw the account"
        )

    movement = CashMovement(
        pan_id=pan.id,
        kind=kind,
        amount=exact,
        note=note,
        occurred_on=occurred_on or date.today(),
    )
    session.add(movement)
    pan.available_balance = new_balance
    return movement


async def set_balance(
    session: AsyncSession,
    pan: PanAccountRow,
    *,
    target: Decimal,
    occurred_on: date | None = None,
) -> CashMovement | None:
    """Move the balance to ``target`` by recording the difference.

    "Set balance" is not a third kind of movement — it resolves to whichever of
    deposit/withdrawal closes the gap, so the ledger still says which way the money
    went. Returns ``None`` when the balance already matches, because a zero-amount
    row would violate ``amount > 0`` and would say nothing anyway.
    """
    wanted = money(target)
    if wanted < 0:
        raise ValueError("a balance cannot be negative")
    delta = money(wanted - pan.available_balance)
    if delta == 0:
        return None
    kind = "DEPOSIT" if delta > 0 else "WITHDRAWAL"
    return await apply_movement(
        session,
        pan,
        kind=kind,
        amount=abs(delta),
        note=f"balance set to {wanted}",
        occurred_on=occurred_on,
    )


async def reverse_movement(session: AsyncSession, movement: CashMovement) -> None:
    """Undo one ledger entry, balance included.

    A mistyped deposit must be removable, and removing the row without unwinding the
    balance would break the invariant this module exists to hold.
    """
    pan = await session.get(PanAccountRow, movement.pan_id)
    if pan is not None:
        unwound = money(pan.available_balance - movement.signed_amount)
        if unwound < 0:
            raise InsufficientFunds(
                f"removing this entry would leave {pan.holder_name} with a negative "
                f"balance; adjust the balance first"
            )
        pan.available_balance = unwound
    await session.delete(movement)


# ──────────────────────────────────────────────────────────────────── mapping


def to_pan_account(row: PanAccountRow) -> PanAccount:
    return PanAccount(
        id=row.id,
        holder_name=row.holder_name,
        relation=row.relation,
        available_balance=row.available_balance,
    )


def to_ipo_task(row: Ipo) -> IPOTask | None:
    """Convert a stored IPO to a schedulable task.

    Returns ``None`` only when the row has no allotment date at all. That used to be
    the common case for anything imported from NSE, which meant an imported issue
    could never be planned; writes now fill an estimate under SEBI T+3 and flag it
    (``dates_estimated``), so this branch is a defensive floor rather than the norm.
    A row that still reaches here with no date cannot be planned — the engine has no
    freeze window — and the caller surfaces it instead of inventing one.
    """
    if row.allotment_date is None:
        return None
    return IPOTask(
        id=row.id,
        name=row.name,
        lot_size=row.lot_size,
        gmp_percent=row.gmp_percent,
        close_date=row.close_date,
        allotment_date=row.allotment_date,
        listing_date=row.listing_date,
        issue_type=IssueType(row.issue_type),
        min_price=row.min_price,
        max_price=row.max_price,
        allotment_probability=row.allotment_probability,
    )


# ─────────────────────────────────────────────────────────────────── seeding


async def ensure_seeded(session: AsyncSession) -> UserProfile:
    """Create the one user profile row if the database is empty. Nothing else.

    It used to seed three PANs and an eight-issue fixture calendar, and both had to
    go — not because fixtures are untidy, but because the way this function decided
    whether to seed made a user-editable calendar impossible:

    * It inserted the whole calendar whenever ``ipos`` was empty, so **deleting every
      issue and restarting brought them all back**.
    * It re-anchored dates on any row whose *name* matched a fixture, so **editing a
      seeded issue's dates was silently undone** the next time the calendar went
      stale.

    Both are gone. Sample data now lives behind ``POST /api/demo/sample-data``, where
    it is something the user asks for once rather than something that reappears.
    ``current_user`` still needs a profile row to resolve, so that much is created.
    """
    user = await load_user(session)
    if user is None:
        user = UserProfile(name="You")
        session.add(user)
        await session.commit()
        await session.refresh(user, ["pans"])
    return user


async def add_pan(
    session: AsyncSession,
    user: UserProfile,
    *,
    holder_name: str,
    relation: str,
    pan_number: str,
    upi_id: str,
    opening_balance: Decimal = Decimal("0.00"),
    linked_bank_name: str | None = None,
) -> PanAccountRow:
    """Create a PAN account, hashing and masking the number on the way in.

    ``pan_number`` exists only inside this call. It is converted immediately and
    never stored, logged or returned — see D11 and SECURITY.md. Any opening balance
    becomes an ``OPENING`` ledger row so the balance has a provenance from the start.
    """
    pan = PanAccountRow(
        user_id=user.id,
        holder_name=holder_name,
        relation=relation,
        pan_masked=mask_pan(pan_number),
        pan_hash=hash_pan(pan_number, settings.pan_hash_salt),
        upi_id=upi_id,
        linked_bank_name=linked_bank_name,
        available_balance=Decimal("0.00"),
    )
    session.add(pan)
    await session.flush()

    if money(opening_balance) > 0:
        await apply_movement(
            session, pan, kind="OPENING", amount=opening_balance, note="opening balance"
        )
    return pan


async def load_sample_data(
    session: AsyncSession, user: UserProfile, *, anchor: date | None = None
) -> dict[str, int]:
    """Insert the demo PANs and fixture calendar, on explicit request only.

    This is what the old startup seeding did, moved behind a button. Rows are marked
    ``source='sample'`` so they are distinguishable from anything real, and existing
    PANs and issues are left alone.

    ``anchor`` is the date the fixture calendar is built around; it defaults to
    tomorrow so today's plan has something in it. Tests pass it explicitly so the
    spacing they assert on does not depend on a default here.
    """
    anchor = anchor or date.today() + timedelta(days=1)
    pans_created = 0
    if not user.pans:
        for holder, relation, pan_number, upi, balance in _DEMO_PANS:
            await add_pan(
                session,
                user,
                holder_name=holder,
                relation=relation,
                pan_number=pan_number,
                upi_id=upi,
                opening_balance=Decimal(balance),
            )
            pans_created += 1

    existing = {ipo.name for ipo in await load_ipos(session)}
    ipos_created = 0
    for task in await SeededProvider(anchor=anchor).fetch_open_ipos(anchor):
        if task.name in existing:
            continue
        session.add(
            Ipo(
                name=task.name,
                symbol=task.id,
                issue_type=task.issue_type.value,
                min_price=task.min_price,
                max_price=task.max_price,
                lot_size=task.lot_size,
                latest_gmp=task.expected_profit_per_lot / task.lot_size,
                gmp_percent=task.gmp_percent,
                # The provider does not model an open date; bidding opens two days
                # before it closes, which is the usual mainboard window.
                open_date=task.close_date - timedelta(days=2),
                close_date=task.close_date,
                allotment_date=task.allotment_date,
                listing_date=task.allotment_date + timedelta(days=3),
                allotment_probability=task.allotment_probability,
                source="sample",
            )
        )
        ipos_created += 1

    await session.commit()
    await session.refresh(user, ["pans"])
    return {"pans_created": pans_created, "ipos_created": ipos_created}


# ───────────────────────────────────────────────────────────────── ipo calendar


def fill_estimated_dates(row: Ipo) -> bool:
    """Give a row a plannable calendar when the registrar has not published one.

    Sets allotment to close + 1 working day and listing to close + 3 (SEBI T+3, see
    :mod:`app.dates`) and marks ``dates_estimated``. Returns whether anything changed.

    Only ever fills a *missing* allotment date, or refreshes dates already flagged as
    estimates. A date a human confirmed is never recomputed — that is the same rule
    ``source`` enforces for the rest of the row, and it is why the flag exists instead
    of the estimate being applied at read time.

    Why estimate at all: an issue with no allotment date was dropped from every plan,
    so importing the real NSE calendar produced a schedule with nothing in it. An
    approximate freeze window that is visibly labelled is worth more than an invisible
    issue, and the user can overwrite it the moment the registrar publishes.
    """
    if row.allotment_date is not None and not row.dates_estimated:
        return False
    allotment, listing = estimated_dates(row.close_date)
    if row.allotment_date == allotment and row.listing_date == listing:
        return False
    row.allotment_date = allotment
    row.listing_date = listing
    row.dates_estimated = True
    return True


async def apply_live_gmp(
    session: AsyncSession, premiums: dict[str, Decimal]
) -> dict[str, object]:
    """Write scraped premiums onto matching issues, and never onto a user's own figure.

    Provenance decides who wins, per field. ``gmp_source == 'user'`` with a premium
    already typed means a human has an opinion about this number, and a scrape from an
    unregulated grey market does not get to overrule it — the row is reported as
    protected instead. A row still at zero has no opinion to overrule, so it is filled.

    ``gmp_percent`` is re-derived through :func:`derive_gmp_percent` rather than read
    from the page, which is the same rule every other GMP write follows.
    """
    updated: list[str] = []
    protected: list[str] = []
    unmatched_names: list[str] = []

    for row in await load_ipos(session):
        premium = premiums.get(normalise_name(row.name))
        if premium is None and row.symbol:
            premium = premiums.get(normalise_name(row.symbol))
        if premium is None:
            unmatched_names.append(row.name)
            continue
        if row.gmp_source == "user" and row.latest_gmp > 0:
            protected.append(row.name)
            continue
        if money(premium) == money(row.latest_gmp) and row.gmp_source == "live":
            continue
        row.latest_gmp = money(premium)
        row.gmp_percent = derive_gmp_percent(row.latest_gmp, row.max_price)
        row.gmp_source = "live"
        updated.append(row.name)

    await session.commit()
    return {
        "updated": updated,
        "unchanged_because_edited": protected,
        "unmatched": unmatched_names,
        "quotes_seen": len(premiums),
    }


def derive_gmp_percent(latest_gmp: Decimal, max_price: Decimal) -> Decimal:
    """GMP as a percentage of the cut-off price.

    ``ipos`` stores the rupee premium *and* the percentage, and the two are read by
    different callers: the UI shows rupees, while Rule 3 ranks on the percentage. If
    both were accepted from a request they would drift, and the symptom would be a
    ranking that quietly disagrees with the premium on screen. So the percentage is
    never an input — it is derived here, on every write, from the two numbers that
    define it.
    """
    cutoff = money(max_price)
    if cutoff <= 0:
        return Decimal("0.00")
    return money(money(latest_gmp) / cutoff * Decimal("100"))


async def upsert_imported_issues(
    session: AsyncSession, issues: list[ImportedIssue]
) -> dict[str, object]:
    """Merge a live import into the calendar without ever undoing a human edit.

    The rule is one line and the UI can state it: **a row the user has edited is
    never overwritten.** Editing any issue promotes ``source`` to ``'user'``
    (see :func:`app.main.update_ipo`), and this function only touches rows still
    marked ``'nse'``. New rows arrive with ``needs_review`` set, because NSE gives no
    lot size, no allotment date and no GMP — and the lot size is what decides how
    much capital a bid freezes, so it must not pass for confirmed.
    """
    by_symbol = {i.symbol: i for i in await load_ipos(session) if i.symbol}
    by_name = {i.name: i for i in await load_ipos(session)}

    imported = 0
    updated = 0
    protected: list[str] = []

    for issue in issues:
        existing = by_symbol.get(issue.symbol) or by_name.get(issue.name)

        if existing is None:
            # NSE publishes no allotment or listing date — the registrar fixes those
            # after the book closes. Estimate them under T+3 and flag it, so the issue
            # enters the plan instead of vanishing into `skipped`.
            allotment, listing = estimated_dates(issue.close_date)
            session.add(
                Ipo(
                    name=issue.name,
                    symbol=issue.symbol,
                    issue_type=issue.issue_type.value,
                    min_price=issue.min_price,
                    max_price=issue.max_price,
                    lot_size=issue.lot_size_estimate,
                    # No GMP anywhere in the feed, so zero rather than a guess. It
                    # will be skipped by the min-GMP floor until the user types one
                    # or a live refresh fills it, which is the honest state for
                    # "premium unknown".
                    latest_gmp=Decimal("0.00"),
                    gmp_percent=Decimal("0.00"),
                    open_date=issue.open_date,
                    close_date=issue.close_date,
                    allotment_date=allotment,
                    listing_date=listing,
                    dates_estimated=True,
                    allotment_probability=Decimal("0.000"),
                    source="nse",
                    needs_review=True,
                )
            )
            imported += 1
            continue

        if existing.source != "nse":
            protected.append(existing.name)
            continue

        existing.name = issue.name
        existing.symbol = issue.symbol
        existing.issue_type = issue.issue_type.value
        existing.min_price = issue.min_price
        existing.max_price = issue.max_price
        existing.open_date = issue.open_date
        existing.close_date = issue.close_date
        # The lot size is still an estimate, so re-deriving it from a moved band is
        # right; the user's own lot size lives on a row that is no longer 'nse'.
        existing.lot_size = issue.lot_size_estimate
        existing.gmp_percent = derive_gmp_percent(existing.latest_gmp, existing.max_price)
        # A moved close date moves the estimates with it. Confirmed dates are left
        # alone by `fill_estimated_dates` itself.
        fill_estimated_dates(existing)
        updated += 1

    await session.commit()
    return {"imported": imported, "updated": updated, "unchanged_because_edited": protected}
