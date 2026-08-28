# Security notes

Read this before exposing the application to a network. The blueprint this was built
from had no security model, and the gaps below are real rather than theoretical.

## Handled

**PAN numbers are never stored in the clear.** The original schema had
`pan_number VARCHAR(10) UNIQUE NOT NULL`. A Permanent Account Number is sensitive
personal data under India's Digital Personal Data Protection Act, 2023, and it is also
a direct identifier for tax and KYC records. The schema now stores:

- `pan_hash` — salted SHA-256, used only for the uniqueness constraint.
- `pan_masked` — `ABCDE****F`, the only form the UI ever renders.

`PAN_HASH_SALT` must be set to a random secret in production. Unsalted hashes of a
10-character alphanumeric identifier are brute-forceable. Rotating the salt invalidates
every stored hash, so treat it as permanent per deployment.
`tests/test_models.py::TestPanPrivacy::test_no_model_has_a_plaintext_pan_column` fails
the build if a raw PAN column is reintroduced. Two API tests go further and assert the
number never appears in a response body — `TestUserState::test_pans_are_masked_and_the_number_never_leaves_the_server`
and `TestCommitAndHistory::test_history_masks_the_pan` — because a column can be private
while a serialiser leaks it.

**Secrets are not committed.** `.env` is gitignored; `.env.example` carries no values.
The local SQLite file is gitignored too — it holds balances and PAN hashes.

**Money cannot silently drift.** Exact decimal arithmetic end to end, so a rounding
error cannot quietly move rupees. See D3 and D14 in `docs/DEVIATIONS.md`.

**No endpoint accepts a `user_id`.** The blueprint's `/api/user/state?user_id=...` is
gone. FastAPI resolves the acting user from its own state (`current_user`), so there is
no parameter an attacker — or a model — can change to read someone else's portfolio.
`tests/test_api.py::TestNoIdentityFromTheCaller` parses `/openapi.json` and fails the
build if an identity parameter is ever added back. This is not authentication; it removes
one specific way of getting it wrong.

**The browser cannot reach FastAPI.** Traffic goes browser → this app's own Next.js route
handlers → FastAPI. `frontend/src/lib/api.ts` begins with `import "server-only"`, so
importing the backend client into a client component is a build error rather than a code
review comment, and the backend's address lives in `API_BASE_URL` — deliberately *not*
`NEXT_PUBLIC_API_BASE_URL`, so it never enters the browser bundle. While the backend is
unauthenticated, an address the page does not know is one a malicious page cannot post to.
`/api/plan` and `/api/commit` validate their body with zod before forwarding, so neither
is a way to relay arbitrary JSON onward.

**CORS is an explicit origin list**, `http://localhost:3000` and `http://127.0.0.1:3000`,
never `*`. Responses carry balances and masked PANs.

**The copilot has no write tools.** Its four tools — `read_portfolio`, `list_ipos`,
`plan_schedule`, `compare_policies` — only read. `POST /api/schedule/commit` writes
`ipo_applications` and is reachable only from a two-step confirmation in the UI. Tool
arguments from a model are attacker-controlled in the presence of prompt injection, so
"explain my schedule" must not be one injected sentence away from placing bids.

## Not handled — do not deploy without these

**There is no authentication or authorisation.** The API is single-tenant and binds to
localhost; anyone who can reach the port reads the whole portfolio. Before exposing it:

- Authenticate every request; never accept `user_id` from the client as identity.
- Scope every query by the authenticated user — a row-level check, not a filter the
  caller supplies.
- If Supabase is used, enable Row Level Security on all five tables. RLS is off by
  default and the anon key is public by design.
- Replace `current_user`'s "the one seeded profile" resolution with the session's user.
  It is one function, and it is the intended seam.

**No rate limiting or audit log.** A portfolio endpoint is worth enumerating, and a
committed bid schedule is worth having a record of.

**The copilot sends financial data to a third party.** This is now live, not
hypothetical: `read_portfolio` puts holder names, relationships, masked PANs and
per-account balances into the prompt, and the prompt goes to Anthropic. That is a
deliberate egress decision and it should be made deliberately per deployment — the tool
already strips the PAN to its masked form and returns aggregates where it can, and
tightening it further (holder initials, balance bands) is a one-function change. Nothing
is persisted on the model provider's side by this code, but nothing here audits what was
sent either.

**Prompt injection reaches tools that read the portfolio.** If untrusted text ever enters
the context — a scraped IPO name, a pasted document, a registrar's PDF — treat every tool
argument as attacker-controlled. Two properties limit the damage today: the tools cannot
write, and they carry no user identity, so the worst case is disclosure to the person
already holding the session rather than a state change or a cross-account read. Both
properties are load-bearing; adding a write tool removes the first, and adding a
`user_id` parameter removes the second.

**Chat history is not persisted, and that is not a privacy feature.** `conversation_memories`
is written by nothing (D8). When it starts being written it will hold balances and
holdings in plain text, and it will need the same treatment as the rest of the schema.

## Not financial advice

Grey-market premium is an unregulated, unofficial indicator with no settlement guarantee.
The `SeededProvider` ships illustrative fixtures, not market data. Expected-profit
figures are arithmetic on an input GMP, not a forecast, and `allotment_probability` is an
assumption the user supplies. Nothing here should be read as a recommendation to apply
for any issue.
