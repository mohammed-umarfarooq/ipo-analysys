"""Runtime configuration, read from the environment.

Deliberately plain: one dataclass, no framework. The only switch that matters is
``DATABASE_URL`` — SQLite for local work, PostgreSQL in production, same models.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / ".env")

DEFAULT_SQLITE_URL = f"sqlite+aiosqlite:///{BACKEND_ROOT / 'ipo_copilot.db'}"

#: Kept out of ``app.providers.gmp_live`` to avoid importing a provider here.
DEFAULT_GMP_LIVE_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    #: ``sqlite+aiosqlite:///...`` for dev, ``postgresql+psycopg://...`` for prod.
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL))

    #: Which GMP source to use. Only "seeded" ships; see app/providers/gmp.py.
    gmp_provider: str = field(default_factory=lambda: os.getenv("GMP_PROVIDER", "seeded"))

    #: Where ``POST /api/ipos/import`` pulls the issue calendar from. ``nse`` hits
    #: NSE's public endpoints; ``none`` disables the button. NSE publishes no lot
    #: size, no allotment date and no GMP, so an import is always partial — see D17.
    ipo_import_source: str = field(
        default_factory=lambda: os.getenv("IPO_IMPORT_SOURCE", "nse")
    )

    #: NSE's endpoints are undocumented and occasionally slow. A short timeout keeps
    #: a hung upstream from holding a request open; the import is retryable by hand.
    nse_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("NSE_TIMEOUT_SECONDS", "15"))
    )

    #: IPOs below this GMP% are never bid on.
    min_gmp: Decimal = field(default_factory=lambda: _decimal("MIN_GMP", "10.0"))

    #: Public grey-market-premium table scraped by ``POST /api/ipos/refresh-gmp``.
    #: There is no official GMP feed anywhere, so this is an unregulated third-party
    #: page; overridable so a broken source can be repointed without a code change.
    gmp_live_url: str = field(
        default_factory=lambda: os.getenv("GMP_LIVE_URL", DEFAULT_GMP_LIVE_URL)
    )

    gmp_live_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("GMP_LIVE_TIMEOUT_SECONDS", "15"))
    )

    #: Salt for PAN hashing. MUST be set to a random value in production (D11).
    pan_hash_salt: str = field(default_factory=lambda: os.getenv("PAN_HASH_SALT", ""))

    #: Must match the vector(N) width in migrations/001_init.sql (D12).
    embedding_dim: int = field(default_factory=lambda: int(os.getenv("EMBEDDING_DIM", "1536")))

    sql_echo: bool = field(default_factory=lambda: os.getenv("SQL_ECHO", "").lower() == "true")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def validate_for_production(self) -> list[str]:
        """Problems that are tolerable locally but must not reach production."""
        problems = []
        if self.is_sqlite:
            problems.append("DATABASE_URL still points at SQLite; pgvector is unavailable")
        if not self.pan_hash_salt:
            problems.append("PAN_HASH_SALT is unset, so PAN hashes are unsalted")
        return problems


settings = Settings()
