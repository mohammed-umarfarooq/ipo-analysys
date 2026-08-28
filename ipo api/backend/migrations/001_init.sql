-- 001_init.sql — PostgreSQL / Supabase schema.
--
-- This is the blueprint's original schema plus the constraints and indexes it was
-- missing (D10 in docs/DEVIATIONS.md). Every deviation is commented inline.
--
-- Apply with:  psql "$DATABASE_URL" -f migrations/001_init.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- Keeps updated_at honest. The original schema had DEFAULT NOW() with no trigger,
-- so the column recorded the insert time forever and silently never updated.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ─── Users ────────────────────────────────────────────────────────────────────
-- DEVIATION (D4): the original `total_bank_balance` column is gone. ASBA freezes
-- money in the applicant's own account, so the authoritative balance lives per-PAN
-- on pan_accounts.available_balance. A single pooled figure invited schedules that
-- fund a family member's bid from your account, which no bank would honour.
-- Read the pooled figure from the user_liquid_capital view below instead.
CREATE TABLE user_profiles (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                VARCHAR(100) NOT NULL,
    total_demat_balance NUMERIC(14, 2) NOT NULL DEFAULT 0.00
                            CHECK (total_demat_balance >= 0),
    -- How the scheduler treats capital (D4 addendum): 'pooled' plans one shared
    -- war-chest across PANs, 'per_pan' ring-fences each holder's balance. Cash still
    -- lives per-PAN on pan_accounts.available_balance either way — this only selects
    -- the capacity test, so no single pooled cash column is reintroduced.
    capital_mode        VARCHAR(16) NOT NULL DEFAULT 'pooled'
                            CHECK (capital_mode IN ('pooled', 'per_pan')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER user_profiles_updated_at BEFORE UPDATE ON user_profiles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ─── PAN accounts ─────────────────────────────────────────────────────────────
-- DEVIATION (D11): the PAN itself is NOT stored in the clear. `pan_hash` is a
-- salted digest used for uniqueness, and `pan_masked` (e.g. 'ABCDE****F') is what
-- the UI renders. A PAN is sensitive personal data under India's DPDP Act, and the
-- original plaintext column sat behind an API with no authentication at all.
CREATE TABLE pan_accounts (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id           UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    holder_name       VARCHAR(100) NOT NULL,
    relation          VARCHAR(50) NOT NULL DEFAULT 'Self',
    pan_masked        VARCHAR(10) NOT NULL,
    pan_hash          CHAR(64) NOT NULL UNIQUE,
    upi_id            VARCHAR(100) NOT NULL,
    linked_bank_name  VARCHAR(100),
    -- DEVIATION (D4): the balance ASBA can actually freeze in THIS account.
    available_balance NUMERIC(14, 2) NOT NULL DEFAULT 0.00
                          CHECK (available_balance >= 0),
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX pan_accounts_user_idx ON pan_accounts (user_id) WHERE is_active;

CREATE TRIGGER pan_accounts_updated_at BEFORE UPDATE ON pan_accounts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- Replaces the dropped user_profiles.total_bank_balance with a derived figure, so
-- there is exactly one source of truth for cash.
CREATE VIEW user_liquid_capital AS
SELECT u.id AS user_id,
       u.name,
       COALESCE(SUM(p.available_balance) FILTER (WHERE p.is_active), 0.00) AS liquid_capital,
       COUNT(p.id) FILTER (WHERE p.is_active) AS active_pan_count
FROM user_profiles u
LEFT JOIN pan_accounts p ON p.user_id = u.id
GROUP BY u.id, u.name;


-- ─── Cash movements ───────────────────────────────────────────────────────────
-- The fund ledger behind pan_accounts.available_balance: every deposit and
-- withdrawal is a dated row, so a balance has a provenance and a mistyped entry is
-- reversible rather than destructive.
--
-- available_balance remains a stored column because the scheduler reads it on every
-- planning pass and a CHECK constraint depends on it. That makes it a materialised
-- sum, and a materialised sum can drift — so it has exactly one writer
-- (app.repository.apply_movement) and a test asserts it equals SUM(signed amount)
-- for every PAN after a randomised sequence of operations. See D18.
--
-- amount is always positive and the direction lives in kind, so a row cannot
-- contradict its own label. There is no ADJUSTMENT kind: "set the balance to X"
-- resolves to whichever of DEPOSIT/WITHDRAWAL closes the gap, so every row still
-- records which way the money moved.
CREATE TABLE cash_movements (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    pan_id      UUID NOT NULL REFERENCES pan_accounts(id) ON DELETE CASCADE,
    kind        VARCHAR(20) NOT NULL
                    CHECK (kind IN ('OPENING', 'DEPOSIT', 'WITHDRAWAL')),
    amount      NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
    note        VARCHAR(140),
    occurred_on DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX cash_movements_pan_idx ON cash_movements (pan_id, occurred_on DESC);

-- The invariant the application maintains, written down where a DBA will see it:
--   pan_accounts.available_balance
--     = SUM(CASE kind WHEN 'WITHDRAWAL' THEN -amount ELSE amount END)
-- Not a constraint because PostgreSQL cannot express a cross-table CHECK, and a
-- trigger owning the balance would compete with the application for the same write.


-- ─── IPO master ───────────────────────────────────────────────────────────────
CREATE TABLE ipos (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          VARCHAR(150) NOT NULL UNIQUE,
    symbol        VARCHAR(50) UNIQUE,
    issue_type    VARCHAR(20) NOT NULL DEFAULT 'Mainboard'
                      CHECK (issue_type IN ('Mainboard', 'SME')),
    min_price     NUMERIC(10, 2) NOT NULL CHECK (min_price > 0),
    max_price     NUMERIC(10, 2) NOT NULL CHECK (max_price > 0),
    lot_size      INT NOT NULL CHECK (lot_size > 0),
    latest_gmp    NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
    gmp_percent   NUMERIC(6, 2) NOT NULL DEFAULT 0.00,
    open_date     DATE NOT NULL,
    close_date    DATE NOT NULL,
    -- DEVIATION (D10): nullable. The registrar has not fixed an allotment date for
    -- a freshly announced issue, and NOT NULL forced callers to invent one.
    -- The scheduler skips IPOs with no allotment date rather than guessing.
    allotment_date  DATE,
    listing_date    DATE,
    registrar_name  VARCHAR(100),
    -- Probability used by the EXPECTED allotment assumption (D5).
    allotment_probability NUMERIC(4, 3) NOT NULL DEFAULT 0.000
                              CHECK (allotment_probability BETWEEN 0 AND 1),
    -- Provenance, and it is load-bearing rather than informational: a refresh from
    -- NSE may only overwrite rows still marked 'nse', and editing any row promotes
    -- it to 'user'. That is what stops an import from undoing a human's correction.
    source        VARCHAR(16) NOT NULL DEFAULT 'user'
                      CHECK (source IN ('user', 'nse', 'sample')),
    -- An import is incomplete by construction: NSE publishes no lot size, no
    -- allotment date and no GMP, and the first of those decides how much capital a
    -- bid freezes. Flagged until a human confirms it.
    needs_review  BOOLEAN NOT NULL DEFAULT FALSE,
    -- Provenance of latest_gmp: 'user' typed it (authoritative), 'live' pulled it
    -- from a grey-market aggregator. GMP is unofficial and unregulated, so a live
    -- refresh never overwrites a row a human has edited.
    gmp_source    VARCHAR(16) NOT NULL DEFAULT 'user'
                      CHECK (gmp_source IN ('user', 'live')),
    -- TRUE when allotment/listing were estimated under SEBI T+3 (close + 1 and + 3
    -- working days) because the registrar has not published them. Cleared when a
    -- human confirms a date.
    dates_estimated BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- DEVIATION (D10): the original schema let a bid band and a calendar be
    -- inverted, which the scheduler would then happily plan against.
    CONSTRAINT ipos_price_band  CHECK (min_price <= max_price),
    CONSTRAINT ipos_calendar    CHECK (open_date <= close_date),
    CONSTRAINT ipos_allotment   CHECK (allotment_date IS NULL OR allotment_date >= close_date),
    CONSTRAINT ipos_listing     CHECK (listing_date IS NULL OR allotment_date IS NULL
                                       OR listing_date >= allotment_date)
);

-- The scheduler's hot path is "IPOs still open as of a date", ranked by GMP.
CREATE INDEX ipos_close_date_idx  ON ipos (close_date);
CREATE INDEX ipos_priority_idx    ON ipos (gmp_percent DESC, allotment_date ASC);

CREATE TRIGGER ipos_updated_at BEFORE UPDATE ON ipos
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ─── Applications ─────────────────────────────────────────────────────────────
CREATE TABLE ipo_applications (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ipo_id                   UUID NOT NULL REFERENCES ipos(id) ON DELETE CASCADE,
    pan_id                   UUID NOT NULL REFERENCES pan_accounts(id) ON DELETE CASCADE,
    -- DEVIATION (D10 / Rule 1): capped at 1. Under the SEBI retail lottery extra
    -- lots on one PAN cannot raise the allotment probability, so anything above 1
    -- is wasted capital rather than a bigger bet.
    lots_applied             INT NOT NULL DEFAULT 1 CHECK (lots_applied = 1),
    blocked_amount           NUMERIC(14, 2) NOT NULL CHECK (blocked_amount > 0),
    bid_date                 DATE NOT NULL,
    unblock_date             DATE,
    allotment_status         VARCHAR(30) NOT NULL DEFAULT 'APPLIED'
                                 CHECK (allotment_status IN
                                     ('APPLIED', 'ALLOTTED', 'NOT_ALLOTTED', 'UNBLOCKED')),
    listing_profit_realized  NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- DEVIATION (D10): Rule 1 was enforced only in Python. The database is the
    -- last line of defence — a retry or a second client must not create a second
    -- application for the same PAN on the same issue.
    CONSTRAINT ipo_applications_one_per_pan UNIQUE (ipo_id, pan_id)
);

CREATE INDEX ipo_applications_pan_idx    ON ipo_applications (pan_id);
CREATE INDEX ipo_applications_status_idx ON ipo_applications (allotment_status);

CREATE TRIGGER ipo_applications_updated_at BEFORE UPDATE ON ipo_applications
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ─── Copilot memory ───────────────────────────────────────────────────────────
-- DEVIATION (D12): the original vector(1536) hardcoded OpenAI's embedding width.
-- The dimension must match whichever embedding model is configured; change it here
-- and in EMBEDDING_DIM together. Left at 1536 as a neutral default.
CREATE TABLE conversation_memories (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id           UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    session_id        VARCHAR(100) NOT NULL,
    role              VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content           TEXT NOT NULL,
    insight_extracted TEXT,
    embedding         vector(1536),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX conversation_memories_session_idx
    ON conversation_memories (user_id, session_id, created_at DESC);

-- Semantic recall index. HNSW gives better recall/latency than IVFFlat and needs
-- no training pass, which matters when the table starts empty.
CREATE INDEX conversation_memories_embedding_idx
    ON conversation_memories USING hnsw (embedding vector_cosine_ops);
