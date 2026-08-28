# Deviations from the Master Blueprint

Every place this implementation differs from the supplied blueprint, and why. IDs are
referenced from code comments and from test names, so a reader who hits `# D4` in a
docstring can find the reasoning here.

Each item was verified by running code, not by inspection alone. The blueprint's
original scheduler is transcribed verbatim in `tests/test_regressions.py`
(`blueprint_original`) and several tests assert that it *is* broken — so if someone
later reverts the engine toward the original shape, the suite names the rule that broke.

---

## D1 — CRITICAL: the priority queue did not prioritise

**Blueprint:** Rule 3 ranks IPOs by `(GMP% DESC, allotment_date ASC)` via a max-heap.
Rule 4 pins every bid to the IPO's close date.

**Problem:** these two rules contradict each other, and the close date wins. The heap
was re-sorted inside the per-date loop and only ever compared IPOs closing on the *same*
day, so across days the GMP ranking had no effect at all. Money committed to a weak
issue closing early stays frozen through allotment, starving a strong issue closing days
later.

Reproduced with ₹20,000, one PAN, and two issues — `JunkCo` at 5% GMP closing Mar 2,
`StarCo` at **80%** closing Mar 4:

```
(2026-03-02, 'Junk Co', 5.0, 1 lot, 15000, balance 5000.0)
-> Star Co received nothing.
```

**Fix:** commit capital in **global priority order** rather than date order
(`SchedulingPolicy.VALUE_FIRST`), and test each prospective bid against the **peak
concurrent load** on that PAN's account across the whole footprint of the new fund
block. Ranking becomes binding while bids stay just-in-time, and the schedule still
cannot overdraw.

A naive "process highest GMP first" would double-commit the same rupees, because two
bids placed on different dates can have overlapping freeze windows. The interval-peak
check is what prevents that; `test_fix_does_not_double_commit_the_same_rupees` pins it.

**Measured effect** (`uv run python -m app.demo`, seeded calendar, ₹15,000 in one PAN):

| Policy | Expected listing gain | Issues won |
|---|---|---|
| `VALUE_FIRST` | **₹17,291.68** | Vertex 62.50%, Aether 55.20% |
| `JIT_GREEDY` (blueprint) | ₹13,253.32 | Vertex 62.50%, Northline 27.80% |

30% more profit from the same capital and the same calendar. The blueprint's algorithm
is kept as `SchedulingPolicy.JIT_GREEDY` so the UI can show this comparison.

## D2 — the max-heap was decorative

`for _, _, _, ipo in sorted(pq)` re-sorted the entire list on every date and never
popped, so it was an O(n log n)-per-day full sort, not a heap.

The `idx` element in `(-gmp_percent, allotment_date, idx, ipo)` was also load-bearing
rather than cosmetic: it stopped tuple comparison before reaching the `IPOTask`, which
is not orderable. Drop it and any tie on both GMP and allotment date raises
`TypeError: '<' not supported between instances of 'IPOTask' and 'IPOTask'`.

**Fix:** `IPOTask.priority_key()` returns a fully-ordered tuple of scalars ending in the
name, so ties are deterministic and no model ever enters a sort key. Pinned by
`test_ipotask_is_not_orderable_so_it_must_never_enter_a_sort_key`.

## D3 — money was held as `float`

The schema stores `NUMERIC(14,2)`; the engine used `float`, and computed lot counts with
`int(current_capital // ipo.lot_cost)`.

```
int(44999.09999999999 // 14999.70) == 2    # should be 3 — a lot silently vanishes
Decimal("44999.10") // Decimal("14999.70") == 3
```

**Fix:** `Decimal` end to end, quantised to paise at every boundary
(`app.domain.money`, `floor_lots`). Because SQLAlchemy's `Numeric` round-trips through
a C double on SQLite, `app.models.Money` is a `TypeDecorator` that stores an integer
number of **paise** on SQLite and native `NUMERIC(14,2)` on PostgreSQL — otherwise the
fix would be undone at the storage layer. See [D14](#d14--exact-is-not-enough-money-must-also-be-ordered)
for why paise and not text. `tests/test_models.py::TestMoneyExactness` covers the round
trip.

## D4 — ASBA capital is not one shared pool

**Blueprint:** a single `user_profiles.total_bank_balance`, and
`IPOJobScheduler(liquid_capital, active_pans)` treats it as available to every PAN.

**Problem:** an ASBA mandate freezes money in the **applicant's own** bank account. A
bid under your mother's PAN cannot be funded from your balance. The blueprint would
plan three lots across three PANs out of a pool that physically sits in one account —
a schedule no bank would execute.

**Fix:** `pan_accounts.available_balance` per account; the engine tracks capital per PAN
silo and `total_bank_balance` is dropped in favour of the derived `user_liquid_capital`
view, so there is one source of truth for cash. `IPOJobScheduler.from_shared_pool()`
keeps the original call signature working by splitting evenly, and documents why.

## D5 — capital lost to allotment was never debited

The blueprint returned 100% of blocked funds at `allotment_date + 1`. But Rule 2
unblocks funds only for **unallotted** applications; allotted money is debited and
becomes shares. Every `remaining_liquid_balance` was therefore optimistic, and a
guaranteed allotment would recycle cash that no longer existed
(`test_original_engine_returns_capital_even_when_certain_to_be_allotted`).

**Fix:** `AllotmentAssumption`. `NONE_ALLOTTED` reproduces the blueprint;
`EXPECTED` treats `lot_cost x allotment_probability` as permanently spent from the
allotment date, modelled as a second fund block that never unblocks.

## D6 — `min_gmp` was advertised but never applied

`recalculate_ipo_schedule(..., min_gmp: float = 10.0)` exposed a threshold the scheduler
never read. Now wired, and a filtered issue appears in `skipped` with the reason.

## D7 — the bid price was ambiguous

`ipos` stores `min_price` and `max_price`; `IPOTask` had a single `price`. Retail
applications are placed at **cut-off**, i.e. the top of the band, so `lot_cost` must
derive from `max_price`. Using `min_price` understates blocked capital by the width of
the band and produces schedules that overdraw in reality.

## D8 — two competing AI stacks, one unreachable

The LangGraph agent (blueprint §5) and the Next.js `streamText` route (§7) implement the
same copilot with the same two tools. The frontend's `useChat` posts to `/api/chat`,
which never touches LangGraph — so the graph and its `AsyncPostgresSaver` were dead
code. Separately, `conversation_memories` is written by nothing, while LangGraph
persists to its own checkpoint tables: two rival memory systems.

**Resolved in Step 2: one entry point, and LangGraph is dropped.** The copilot is the
Next.js route handler at `frontend/src/app/api/chat/route.ts`, because that is the URL
`useChat` actually posts to — keeping LangGraph as well would have meant maintaining a
second copy of every tool, and a tool's authorisation rules are the last thing that
should exist in two places. The graph, `AgentState` and `AsyncPostgresSaver` are gone,
which also retires all of [D9](#d9--langgraph-api-misuse).

Memory is the honest half of this: **`conversation_memories` is still written by
nothing.** The chat is stateless per request — the client sends the message list, the
server keeps none of it. Semantic recall needs pgvector, which needs a PostgreSQL
server this machine does not have (see [D12](#d12--vector1536-hardcodes-one-embedding-provider)
and the environment table), so it is absent rather than faked. When it lands,
`conversation_memories` is the one store; the LangGraph checkpoint tables do not come
back.

The four tools are read-only, and `POST /api/schedule/commit` is deliberately not among
them — [D15](#d15--the-copilot-cannot-spend-money-and-the-browser-cannot-reach-fastapi).

## D9 — LangGraph API misuse

- `AsyncPostgresSaver.from_conn_string(uri)` returns an **async context manager**;
  `await checkpointer.setup()` on the returned object is wrong. It needs `async with`
  bound to the FastAPI lifespan.
- `lambda state: "tools" if state["messages"][-1].tool_calls else END` raises
  `AttributeError` whenever the last message is not an AI message carrying that
  attribute. The built-in `tools_condition` handles the cases.
- `chatbot_node` never passes a system prompt, so the persona and the `capital` / `pans`
  fields in `AgentState` are unused.

**Resolved by removal.** All three are LangGraph bugs, and there is no LangGraph left
(D8). The analysis stays here because it is the evidence for that decision: three
defects in a 40-line graph that the frontend could never reach.

## D10 — schema hardening

The original DDL left these to chance; all are now enforced in
`migrations/001_init.sql` and mirrored in `app/models.py`:

- **`UNIQUE (ipo_id, pan_id)`** on `ipo_applications`. Rule 1 was enforced only in
  Python, so a retry or a second client could create a second application for the same
  PAN on the same issue. Also `CHECK (lots_applied = 1)` — extra lots on one PAN cannot
  raise allotment odds, so anything above 1 is wasted capital rather than a bigger bet.
- `CHECK (min_price <= max_price)`, `CHECK (open_date <= close_date)`,
  `CHECK (allotment_date >= close_date)`, `CHECK (listing_date >= allotment_date)`.
- `allotment_date` made **nullable**: the registrar has not fixed one for a freshly
  announced issue, and `NOT NULL` forced callers to invent a date the scheduler would
  then plan against.
- `updated_at` had `DEFAULT NOW()` and no trigger, so it recorded the insert time
  forever. Added a `set_updated_at()` trigger on every table that has the column.
- Indexes on the foreign keys and hot paths (`ipos.close_date`, the GMP priority
  ordering, `ipo_applications.pan_id`, `conversation_memories(user_id, session_id)`),
  plus an **HNSW** index on `embedding` — chosen over IVFFlat because it needs no
  training pass, which matters when the table starts empty.

`tests/test_models.py::TestNoDriftFromPostgresMigration` asserts the models and the
hand-written PostgreSQL DDL still describe the same schema.

## D11 — PAN plaintext and no authentication

The blueprint stores `pan_number VARCHAR(10)` in the clear and exposes
`/api/user/state` keyed by `user_id` with no authentication of any kind. A PAN is
sensitive personal data under India's DPDP Act.

**Partially fixed:** the raw PAN is never persisted. `pan_hash` (salted SHA-256) gives
uniqueness and `pan_masked` (`ABCDE****F`) is what the UI renders;
`test_no_model_has_a_plaintext_pan_column` enforces it, and two API tests assert the
number never appears in a response body. The endpoints take no `user_id` at all —
`TestNoIdentityFromTheCaller` reads `/openapi.json` and fails if one is ever added as a
parameter, so the blueprint's "trust the caller's UUID" shape cannot come back by
accident.

**Authentication is still absent** and must be added before this is exposed to a network
— see `SECURITY.md`. [D15](#d15--the-copilot-cannot-spend-money-and-the-browser-cannot-reach-fastapi)
reduces the blast radius meanwhile (the browser cannot address FastAPI at all) but it is
containment, not authentication.

## D12 — `vector(1536)` hardcodes one embedding provider

1536 is OpenAI `text-embedding-3-small`. The column width must match whichever
embedding model is configured, so it is now paired with `EMBEDDING_DIM` in
`app/config.py` and commented in the migration. Moot under the SQLite decision below,
since pgvector needs a PostgreSQL server.

## D13 — Rule 3 is a heuristic, not an optimum

Found while testing, and **not** a defect in the implementation — a limit of the
specified rule that is worth knowing before trusting the output.

Ranking by GMP% is greedy on a *percentage* while the scarce resource is *rupees x days
locked*. Two issues can tie on GMP%, at which point Rule 3 breaks the tie on the earlier
allotment date — but the issue with the larger lot cost earns more absolute rupees at
the same percentage. A seeded sweep found `VALUE_FIRST` losing to the greedy baseline in
1 case out of 200 for exactly this reason (worst shortfall under 2%).

The suite therefore asserts aggregate dominance rather than universal dominance, and
`test_rule3_is_a_heuristic_not_an_optimum` pins the counterexample. A true optimum needs
a solver over the capital-lockup profile, not a greedy sort. Left as specified because
Rule 3 is a stated hard requirement.

## D14 — exact is not enough: money must also be ordered

Found while building the API, and a defect in *my own* Step 1 code rather than the
blueprint's. The `Money` `TypeDecorator` from D3 originally stored a **string** on
SQLite. That is exact — a decimal survives the round trip unchanged — which is why the
round-trip test passed and the problem stayed invisible.

But a `TEXT` column compares as text, so SQL comparisons on money inverted:

```
'985.00' > '1040.00'    -- true as strings, false as money
```

Every money `CHECK` constraint from D10 is a comparison. `CHECK (min_price <= max_price)`
would have **rejected a valid price band** of ₹985–₹1,040, and `ORDER BY blocked_amount
DESC` would have ranked ₹9,000 above ₹44,520. D10's whole argument is that these
constraints belong in the database and not only in Python; text storage quietly took
that back.

**Fix:** store an integer number of **paise** on SQLite (`BigInteger`), scaled back to a
`Decimal` on read. Integers are exact *and* ordered, so the constraints mean in SQLite
what they mean in PostgreSQL. `tests/test_models.py::TestMoneyIsOrderedInSql` pins it at
the SQL level: the ₹985–₹1,040 band is accepted, the inverted band is rejected, and
`ORDER BY` on a money column comes back numerically sorted.

The general lesson is worth keeping: a round-trip test proves storage is *lossless*, not
that it is *comparable*, and half the value of a money column is in the comparisons.

## D15 — the copilot cannot spend money, and the browser cannot reach FastAPI

Not a defect in the blueprint so much as a hole in it: it specifies an unauthenticated
FastAPI service (D11) and a browser-side `useChat`, and never says which of the two the
browser is allowed to talk to.

Two decisions, both enforced in code rather than by convention:

**The browser never addresses FastAPI.** All traffic goes browser → this app's own route
handlers (`/api/plan`, `/api/commit`, `/api/chat`) → FastAPI. `frontend/src/lib/api.ts`
starts with `import "server-only"`, so importing the backend client into a client
component is a **build error**, not a review comment. The backend's address lives in
`API_BASE_URL` — deliberately not `NEXT_PUBLIC_API_BASE_URL`, so it never enters the
bundle. Until authentication exists, an address the browser does not know is one an
attacker's page cannot post to.

**The copilot has no write tools.** It gets four, all read-only: `read_portfolio`,
`list_ipos`, `plan_schedule`, `compare_policies`. `POST /api/schedule/commit` writes
`ipo_applications` and is reachable only from a two-step confirmation in the UI. Tool
arguments from a model are attacker-controlled in the presence of prompt injection
(`SECURITY.md`), and "explain my schedule" should not be one injected sentence away from
placing bids. The tools carry no user identity either — they cannot be steered at another
account by a model-produced `user_id`.

Both proxy handlers validate their body with zod before forwarding, so the route is not a
way to relay arbitrary JSON to an unauthenticated service.

## D16 — the chat route targets an AI SDK that no longer exists

The blueprint's `route.ts` (§7) defines tools with `parameters:` and returns
`result.toDataStreamResponse()`. Neither survives in AI SDK 7, which is what
`npm install ai` resolves to today (7.0.74). Verified against the current official docs
rather than from memory, because this is exactly the kind of API that moves:

| Blueprint | AI SDK 7 |
|---|---|
| `tool({ parameters: z.object({...}) })` | `tool({ inputSchema: z.object({...}) })` |
| `result.toDataStreamResponse()` | `createUIMessageStreamResponse({ stream: toUIMessageStream({ stream: result.stream }) })` |
| `maxSteps` | `stopWhen: isStepCount(6)` |
| `messages` passed through | `messages: await convertToModelMessages(messages)` |
| `message.content` on the client | `message.parts`, with tool parts named `tool-{name}` |

`gpt-4o` also becomes `claude-sonnet-5` (overridable via `COPILOT_MODEL`), per the
provider decision below. The transcribed blueprint route would not compile, so there was
no version of this step that consisted of pasting it in.

---

## Environment-driven decisions

Measured on this machine, not assumed:

| Tool | Found | Consequence |
|---|---|---|
| Python | 3.14.6 | All pinned deps have cp314 wheels; verified by a clean `uv sync`. |
| Node / npm | 24.18.1 / 11.16.0 | Frontend runs on Next 16.3.2 with Turbopack. |
| Docker | **absent** | No containerised PostgreSQL. |
| `psql` | **absent** | No local PostgreSQL, therefore **no pgvector**. |

1. **SQLite for development, PostgreSQL for production.** One set of SQLAlchemy models;
   `DATABASE_URL` is the only switch. `migrations/001_init.sql` stays the production
   migration because extensions, triggers and views cannot be expressed portably. The
   dev schema is built with `create_all` rather than a second hand-maintained SQL file,
   so the two cannot drift silently; a test asserts they agree.
2. **`psycopg3` over `asyncpg`** as the production driver — better Python 3.14 wheel
   coverage. (This was also LangGraph's driver, which mattered before D8 dropped
   LangGraph; the wheel argument stands on its own.)
3. **Claude over `gpt-4o`** for the copilot: `claude-sonnet-5` via
   `@ai-sdk/anthropic`, overridable with `COPILOT_MODEL`. The copilot degrades to a
   readable "not configured" panel with no `ANTHROPIC_API_KEY` rather than failing at
   request time.
4. **No scraping.** GMP exists only on third-party grey-market aggregators, as
   unlabelled HTML, under terms that generally prohibit scraping, and it changes shape
   without notice. `app/providers/gmp.py` defines a `GmpProvider` interface and ships a
   deterministic `SeededProvider`; a real source can be dropped in behind the interface.
5. **No Redis.** The blueprint caches GMP for 15 minutes. `SeededProvider` is a
   dictionary lookup, so caching it would be pure ceremony; the TTL belongs with the real
   provider that needs it.

Items 1–5 were put to the user as questions and left unanswered, so they are defaults
chosen to be **easy to reverse** — each is one environment variable or one class behind
an existing interface — not settled choices. The D8 chat-architecture question was also
unanswered, but Step 2 could not be built around an open question, so it was decided and
the reasoning is recorded under D8 in full for reversal.
