# IPO Copilot & Cashflow Scheduler

Decides **which IPOs to bid on, from which PAN account, on which day**, so that a limited
pool of cash is recycled through ASBA fund-freeze cycles to capture the most grey-market
premium.

Built from a supplied Master Blueprint. The blueprint's architecture is sound, but its
core algorithm did not satisfy its own stated rules — **every deviation is documented
and test-pinned in [docs/DEVIATIONS.md](docs/DEVIATIONS.md)**. Read that before trusting
the output.

## Status

| Step | Scope | State |
|---|---|---|
| 1 | Schema, domain model, scheduling engine, test suite | **Done** |
| 2 | FastAPI routes (`/api/schedule`, `/api/ipos`, `/api/user/state`), copilot | **Done** |
| 3 | Next.js frontend, Gantt matrix, KPI ribbon | **Done** |
| 4 | Chat drawer, streaming tool calls | **Done** |

112 backend tests pass; `npm run lint`, `npm run typecheck` and `next build` are clean.
Five decisions were put to the user and left unanswered — see
[Open decisions](#open-decisions) — and each is a default chosen to be reversible with one
environment variable.

## Quick start

Two processes. No database server, no Docker. An API key is optional: the dashboard is
fully functional without one, and only the chat drawer needs it.

```bash
cd backend && uv sync --group dev && uv run uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend && npm install && npm run dev
```

Then open <http://localhost:3000>. The database seeds itself on first boot with three
PANs, ₹3,35,000 of capital and a six-issue IPO calendar.

To enable the copilot, put a key in `frontend/.env.local`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

Without it the drawer explains that it is disabled instead of failing at request time.

Run the test suite (112 tests):

```bash
cd backend && uv run pytest
```

See the same plan without a browser:

```bash
cd backend && uv run python -m app.demo
```

```
BID DATE    IPO                             GMP  LOTS         BLOCKED    UNBLOCKS      CASH LEFT
2026-03-04  Vertex Semiconductors        62.50%     3      ₹43,680.00  2026-03-08   ₹2,91,320.00   SEL+MOT+FAT
2026-03-05  Meridian Logistics           18.75%     3      ₹44,640.00  2026-03-09   ₹2,46,680.00   SEL+MOT+FAT
2026-03-06  Sunhaven Renewables          41.00%     3      ₹44,370.00  2026-03-11   ₹2,02,310.00   SEL+MOT+FAT
2026-03-09  Northline Speciality Chem    27.80%     3      ₹44,820.00  2026-03-13   ₹2,32,058.00   SEL+MOT+FAT
2026-03-12  Aether Data Centres          55.20%     3      ₹44,520.00  2026-03-16   ₹2,25,696.20   SEL+MOT+FAT
2026-03-13  Cobalt Tools SME             12.10%     1    ₹1,15,200.00  2026-03-17   ₹1,50,386.00   SEL
```

## The headline finding

The blueprint's Rule 3 ranks IPOs by GMP% descending. Rule 4 pins every bid to the
issue's close date. **These contradict each other, and the close date wins** — so the
GMP ranking was dead code. Capital committed to a weak issue closing early stays frozen
through allotment and starves a strong issue closing days later.

Same capital, same calendar, ₹15,000 in one PAN:

| Policy | Expected gain | Issues won |
|---|---|---|
| `VALUE_FIRST` (fixed) | **₹17,291.68** | Vertex 62.50%, Aether 55.20% |
| `JIT_GREEDY` (blueprint) | ₹13,253.32 | Vertex 62.50%, Northline 27.80% |

30% more profit. The blueprint's algorithm is retained as `JIT_GREEDY` so the UI can
show this comparison, and so the regression suite can prove the fix still holds.

Full reasoning: [D1](docs/DEVIATIONS.md#d1--critical-the-priority-queue-did-not-prioritise).
Note also [D13](docs/DEVIATIONS.md#d13--rule-3-is-a-heuristic-not-an-optimum): Rule 3 is
a greedy heuristic, not an optimiser, and it is not always right.

## How the engine works

`app/scheduler.py`. Capital is committed in **global priority order** (Rule 3), not date
order. Each prospective bid is tested against the **peak concurrent load** on that PAN's
account across the whole footprint of the new fund block:

- A bid freezes `lot_cost` over `[close_date, allotment_date + 1)` — Rule 2, T+1 unblock.
- Each PAN's frozen capital over time is a step function that only rises at a block's
  start date, so the peak is found by evaluating the load at every block start. No
  calendar walk, and exact.
- A bid is placed only if that peak stays within the PAN's **own** bank balance ([D4](docs/DEVIATIONS.md#d4--asba-capital-is-not-one-shared-pool)) —
  this is what stops the schedule from overdrawing, and what makes out-of-date-order
  commitment safe.
- One lot per PAN per IPO, structurally (Rule 1 — extra lots cannot improve lottery odds).

Two knobs the blueprint did not have:

- `SchedulingPolicy` — `VALUE_FIRST` (default) or `JIT_GREEDY` (blueprint baseline).
- `AllotmentAssumption` — `NONE_ALLOTTED` (blueprint: all capital returns) or `EXPECTED`
  (capital consumed by a successful allotment never comes back — [D5](docs/DEVIATIONS.md#d5--capital-lost-to-allotment-was-never-debited)).

Nothing is ever silently dropped: an IPO not bid on appears in `result.skipped` with a
reason.

## What the dashboard shows

One page, because the whole point is seeing the trade-off at once.

- **KPI ribbon** — liquid capital, planned bids, expected gain, peak capital frozen (with
  the share of available cash that represents at the busiest moment), and *value of
  ranking*: the rupee difference between the two policies. When capital is not scarce that
  last figure reads `—` with the reason, because a delta of zero and a delta that cannot
  exist are different facts.
- **Gantt timeline** — one bar per bid, spanning `[bid date, allotment date + 1)`, over a
  dated axis with a today marker. Underneath, a per-day strip of money frozen by those
  blocks, with the peak highlighted and total liquid capital drawn as a dashed line. The
  strip is the engine's reasoning made visible: frozen capital only rises when a block
  *starts*, which is why checking every bid date finds the true peak.
- **Priority matrix** — every issue ranked, including the ones that were skipped. A
  skipped issue keeps its row and carries its reason inline, so "why is this not in my
  plan" never requires reading logs.
- **PAN ledger** — per-account balance, peak load and headroom, computed the same way the
  engine computes it. This is where D4 becomes obvious: a relative's balance is not yours.
- **Policy and assumption toggles** — switching policy is instant because both plans
  arrive in one response; changing the allotment assumption re-plans. Prose next to the
  toggle explains what the current delta means rather than leaving two numbers on screen.
- **Commit** — two steps (*Commit plan* → *Confirm N bids*), then the page revalidates.
  This is the only state-changing path in the app.

## The copilot

A slide-over drawer, streaming, with four **read-only** tools: `read_portfolio`,
`list_ipos`, `plan_schedule`, `compare_policies`. Tool calls render as chips as they run,
so an answer's provenance is visible rather than asserted.

Its system prompt forbids stating any figure that did not come from a tool call, requires
GMP to be described as unofficial and unregulated, and declines the financial-adviser
role. Asked which policy is better, it must call `compare_policies` and quote the actual
difference.

`POST /api/schedule/commit` is deliberately **not** a tool. Model input is
attacker-controlled where prompt injection is possible, so spending money stays behind a
human click ([D15](docs/DEVIATIONS.md#d15--the-copilot-cannot-spend-money-and-the-browser-cannot-reach-fastapi)).

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness, plus `production_warnings` the UI surfaces as a banner. |
| `GET` | `/api/user/state` | PANs (masked), per-account balances, liquid capital. |
| `GET` | `/api/ipos` | The calendar with GMP, lot cost, priority rank, `schedulable`. |
| `POST` | `/api/schedule` | Plan under one policy and assumption. |
| `POST` | `/api/schedule/compare` | Both policies plus the delta, in one call. |
| `POST` | `/api/schedule/commit` | Write `ipo_applications`. The only mutation. |
| `GET` | `/api/portfolio/history` | Committed applications, PANs masked. |

**No endpoint takes a `user_id`.** The acting user is resolved from server state, so no
parameter can be changed to read another portfolio; a test reads `/openapi.json` and fails
the build if one appears. That is not authentication — see [SECURITY.md](SECURITY.md).

The browser never calls these directly. It talks to the Next.js route handlers
(`/api/plan`, `/api/commit`, `/api/chat`), which validate with zod and forward.
`src/lib/api.ts` is `import "server-only"`, so crossing that boundary is a build error,
and `API_BASE_URL` is deliberately not `NEXT_PUBLIC_` — the backend's address never enters
the bundle.

## Layout

```
backend/
  app/
    domain.py        Decimal money, IPOTask, PanAccount, fund blocks, policies
    scheduler.py     the engine
    models.py        SQLAlchemy 2.x async; Money TypeDecorator keeps Decimal exact
    schemas.py       pydantic request/response contracts
    repository.py    queries and the seed data
    service.py       plan / compare / commit, the transaction boundary
    main.py          FastAPI app, CORS, current_user, the seven routes
    config.py        DATABASE_URL and friends
    providers/gmp.py GmpProvider interface + deterministic SeededProvider
    demo.py          CLI smoke run
  migrations/
    001_init.sql     PostgreSQL/pgvector production schema
  tests/             112 tests
frontend/
  src/
    app/
      page.tsx       server component: fetches in parallel, renders failure states
      api/plan/      zod-validated proxy to /api/schedule/compare
      api/commit/    zod-validated proxy to the one mutation
      api/chat/      the copilot — streaming, four read-only tools
    components/      Dashboard, KpiRibbon, GanttSchedule, PriorityMatrix,
                     PanLedger, PolicyToggle, ChatDrawer
    lib/
      api.ts         server-only FastAPI client
      types.ts       hand-written mirrors of the pydantic schemas
      format.ts      ₹ grouping (en-IN), dates, GMP tones
docs/DEVIATIONS.md   every difference from the blueprint, with evidence
SECURITY.md          PAN handling, and the auth gap that must be closed
```

`lib/types.ts` is written by hand rather than generated from `/openapi.json`. Generated
types would carry field *names*; these carry field *meaning* — that balances are per PAN
and never pooled, that `lot_cost` derives from the cut-off price, that `unblock_date` is
exclusive. That is the part a reader gets wrong.

## Testing approach

The suite is organised around what could actually go wrong:

- **`test_scheduler_rules.py`** (16) — each of the four domain rules, including the T+1
  boundary from both sides (capital *is* reusable on the unblock date, and is *not* one
  day earlier).
- **`test_scheduler_invariants.py`** (22) — a seeded random sweep over hundreds of
  generated IPO calendars, asserting on every one that no PAN is ever overdrawn, that
  every IPO is either scheduled or explained, that the reported cash balance is
  reconstructible from the published blocks, and that results are deterministic. Seeds are
  fixed, so any failure is reproducible.
- **`test_regressions.py`** (20) — one test per defect. The blueprint's original scheduler
  is transcribed verbatim and several tests assert that it *is* broken, so a future
  "simplification" back toward it names the rule it broke.
- **`test_models.py`** (29) — money survives a database round trip exactly *and compares
  numerically in SQL* ([D14](docs/DEVIATIONS.md#d14--exact-is-not-enough-money-must-also-be-ordered)),
  the SEBI cap is enforced by the database and not only by Python, no model has a
  plaintext PAN column, and the models have not drifted from the hand-written PostgreSQL
  migration.
- **`test_api.py`** (25) — the HTTP contract: no endpoint accepts an identity parameter,
  PANs are masked in every response body, a commit is idempotent per `(ipo, pan)`, an
  empty portfolio returns 409 rather than a plausible-looking empty plan, and the D1 delta
  is visible end to end when capital is scarce.

Verifiers in `conftest.py` deliberately reconstruct the capital timeline from the
scheduler's **public output** rather than its internal ledger, so a bug in the ledger
surfaces as a failing invariant instead of being masked.

## Environment notes

Python 3.14.6 with no Docker and no local PostgreSQL, so development runs on SQLite via
`aiosqlite`; `DATABASE_URL` is the only switch to Postgres. `psycopg3` is the production
driver rather than `asyncpg` (better 3.14 wheel coverage). pgvector needs a real server,
so semantic recall is unavailable locally — absent rather than faked.

Frontend on Node 24.18.1 / npm 11.16.0. Versions resolved at install time and verified
with `npm ls`, because two of these APIs moved recently enough to matter
([D16](docs/DEVIATIONS.md#d16--the-chat-route-targets-an-ai-sdk-that-no-longer-exists)):

| Package | Resolved |
|---|---|
| `next` | 16.3.2 (Turbopack) |
| `react` / `react-dom` | 19.2.8 |
| `ai` | 7.0.74 |
| `@ai-sdk/anthropic` | 4.0.40 |
| `@ai-sdk/react` | 4.0.77 |
| `tailwindcss` | 4.3.3 (CSS-first `@theme`, no config file) |
| `zod` | 4.4.3 |
| `typescript` | 5.9.3 |
| `eslint` / `eslint-config-next` | 9.39.5 / 16.3.2 |

ESLint runs as `eslint .` from a flat config, not `next lint` — that command was removed in
Next 16, and the script it left behind failed with a directory error rather than linting
anything. It caught a real defect on its first run: `page.tsx` returned its JSX from inside
the `try` that guards the fetches, which reads as if render errors were handled when React
renders too late for that to be true.

## Open decisions

Put to the user and unanswered, so these are defaults chosen to be easy to reverse:

1. **Database** — SQLite now, Postgres-ready. Switch by setting `DATABASE_URL` and
   applying `migrations/001_init.sql`.
2. **LLM provider** — Claude (`claude-sonnet-5`) rather than the blueprint's `gpt-4o`.
   Override with `COPILOT_MODEL`.
3. **GMP source** — no scraping. Interface plus seeded fixtures; a real source drops in
   behind `GmpProvider`.
4. **No Redis.** The blueprint caches GMP for 15 minutes; `SeededProvider` is a dictionary
   lookup, so the TTL belongs with the real provider that needs it.
5. **Chat architecture** — this one could not stay open, because Step 2 had to be built on
   an answer. The blueprint specified two copilots doing the same job with two rival memory
   stores, and the LangGraph half was unreachable from the frontend. Decided: one entry
   point at Next.js `/api/chat`, LangGraph dropped, and `conversation_memories` left
   unwritten rather than half-wired. Full reasoning, and what reversing it would take:
   [D8](docs/DEVIATIONS.md#d8--two-competing-ai-stacks-one-unreachable).

## Not financial advice

Grey-market premium is an unregulated, unofficial indicator with no settlement guarantee.
The seeded calendar is illustrative fixtures, not market data. Expected-profit figures are
arithmetic on an input GMP, not a forecast. See [SECURITY.md](SECURITY.md).
