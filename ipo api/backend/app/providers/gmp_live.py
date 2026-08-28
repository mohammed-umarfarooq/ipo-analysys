"""Live grey-market premium from a public aggregator.

**There is no official GMP feed.** The grey market is an unregulated, off-exchange
forward market; NSE, BSE and SEBI publish nothing about it. Every number here comes
from an aggregator that collects dealer quotes, so it is an indication of sentiment
and nothing more. It is never authoritative and the user's own figure always wins.

Why this source
---------------
The obvious candidates were checked live while writing this:

* ``investorgain.com`` report 331 — the data is real but sits behind
  ``alphanodejs.investorgain.com/cloud/v2/report/data-read/331/...``, which answers
  ``{"msg":-1,"error":"Invalid request!"}`` to any request that is not signed by their
  own client. Gated, so not usable.
* ``chittorgarh.com`` — its GMP report redirects to the investorgain one.
* ``ipowatch.in`` — serves the whole premium table **server-rendered in the HTML**,
  no gate, no JSON API to guess at. That is what this parses.

A server-rendered table is a better scrape target than a private JSON API: nothing is
signed, so the request cannot be rejected for lacking a token, and when the markup does
change the parser returns nothing rather than something wrong.

The table's columns, verbatim::

    IPO Name | IPO GMP* | Trend | Price Band | Est. Listing | Date
    Augmont Enterprises | ₹310 | 🟢 | ₹786 | ₹1096 (39.34%) | 21-25 August

Only the first two are read. The premium is rupees per share, which is exactly what
``ipos.latest_gmp`` stores, so ``gmp_percent`` still comes from
:func:`app.repository.derive_gmp_percent` and never from the page.

This will break. It is a scrape of someone else's HTML, and it is treated as such:
failure raises :class:`GmpUnavailable`, the endpoint answers 502, and typing a premium
by hand keeps working exactly as before.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from html import unescape

import httpx

from app.config import settings

#: Public GMP table, server-rendered. Overridable via ``GMP_LIVE_URL`` so a broken
#: source can be repointed without a code change.
DEFAULT_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_AMOUNT = re.compile(r"-?\d+(?:\.\d+)?")

#: Company-form noise that differs between the aggregator and NSE for the same issue:
#: "Augmont Enterprises" vs "Augmont Enterprises Limited". Stripped from both sides.
_SUFFIXES = (
    "limited",
    "ltd",
    "private",
    "pvt",
    "india",
    "ipo",
    "corporation",
    "company",
)


class GmpUnavailable(RuntimeError):
    """The aggregator could not be reached, or served something that was not the table."""


def normalise_name(name: str) -> str:
    """A comparison key that survives the two sites naming one issue differently.

    Lowercased, company-form words dropped, everything non-alphanumeric removed. So
    "Augmont Enterprises Limited" and "Augmont Enterprises" both become
    ``augmontenterprises`` and match, while two genuinely different issues do not
    collide — the remaining string is still the distinctive part of the name.
    """
    words = re.split(r"[^a-z0-9]+", name.lower())
    return "".join(w for w in words if w and w not in _SUFFIXES)


def parse_premium(raw: str) -> Decimal | None:
    """``"₹310"`` -> ``310``. ``None`` when there is no number to read.

    ``₹0`` is a real answer meaning "no premium right now", so it parses to zero rather
    than to ``None``; ``₹-`` (premium not yet quoted) has no number and returns
    ``None``, which the caller skips instead of writing a zero over a real figure.
    A negative premium is possible in the grey market and is kept.
    """
    found = _AMOUNT.search(raw or "")
    if not found:
        return None
    try:
        return Decimal(found.group(0))
    except InvalidOperation:  # pragma: no cover - the regex cannot produce this
        return None


def _text(cell: str) -> str:
    """A table cell as plain text: tags removed, entities decoded, spaces collapsed.

    Entity decoding is not cosmetic. The page is WordPress-generated and writes the
    rupee sign as ``&#8377;``, so a cell read literally is ``&#8377;310`` — from which
    the first number matched is **8377**, a plausible-looking premium that is entirely
    fictional. Tags are stripped before decoding so that an escaped ``&lt;b&gt;`` in the
    content cannot turn into markup the stripper has already walked past.
    """
    return re.sub(r"\s+", " ", unescape(_TAG.sub("", cell))).strip()


def parse_gmp_table(html: str) -> dict[str, Decimal]:
    """Extract ``{normalised name: rupee premium}`` from the aggregator's page.

    Rows whose first cell is a header, or whose premium cell holds no number, are
    dropped silently — the page carries several unrelated tables and a legend, and the
    absence of a premium is not an error. An empty result *is* treated as an error by
    the caller, because "the table moved" and "no IPO has a premium today" must not
    look the same.
    """
    premiums: dict[str, Decimal] = {}
    for row in _ROW.findall(html):
        cells = [_text(c) for c in _CELL.findall(row)]
        if len(cells) < 2:
            continue
        key = normalise_name(cells[0])
        if not key or key == "iponame":
            continue
        premium = parse_premium(cells[1])
        if premium is None:
            continue
        # First occurrence wins: the live table is at the top of the page and older
        # month-by-month tables repeat names further down.
        premiums.setdefault(key, premium)
    return premiums


async def fetch_live_gmp() -> dict[str, Decimal]:
    """Fetch and parse the public GMP table.

    Raises :class:`GmpUnavailable` on any failure, including an empty parse, so the
    endpoint can answer 502 with something a user can act on. It never returns a
    partial-looking success that would quietly zero out premiums.
    """
    url = settings.gmp_live_url
    timeout = httpx.Timeout(settings.gmp_live_timeout_seconds)
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"}

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as c:
            response = await c.get(url)
    except httpx.HTTPError as exc:
        raise GmpUnavailable(
            f"could not reach the GMP aggregator ({exc.__class__.__name__}). "
            f"Grey-market premium has no official feed — type it in by hand instead."
        ) from exc

    if response.status_code != 200:
        raise GmpUnavailable(
            f"the GMP aggregator answered HTTP {response.status_code}. It is a public "
            f"page with no support commitment — type the premium in by hand instead."
        )

    premiums = parse_gmp_table(response.text)
    if not premiums:
        raise GmpUnavailable(
            "the GMP aggregator's page no longer contains a readable premium table, "
            "which usually means its markup changed. Type the premium in by hand."
        )
    return premiums
