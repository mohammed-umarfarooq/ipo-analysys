/**
 * TypeScript mirrors of the FastAPI response models in `backend/app/schemas.py`
 * and `backend/app/domain.py`.
 *
 * Hand-written rather than generated. The API is small and stable, and a
 * hand-written type is a place to record what a field *means* — `blockedAmount`
 * being per-PAN rather than pooled, for instance, is the whole point of D4 and is
 * invisible in a generated `number`.
 *
 * Money crosses the wire as a JSON number. That is safe here because the backend
 * has already quantised every value to paise with `Decimal`; these numbers are
 * for display and are never used to re-derive an allocation. All arithmetic that
 * decides anything happens in Python.
 */

export type SchedulingPolicy = "value_first" | "jit_greedy";
export type AllotmentAssumption = "none_allotted" | "expected";
/**
 * `pooled` plans one war-chest that any PAN may draw on; `per_pan` ring-fences each
 * holder's own balance, which is what ASBA actually does. Pooled is the default
 * because it matches how a household thinks about its money — see D4.
 */
export type CapitalMode = "pooled" | "per_pan";

export interface Pan {
  id: string;
  holder_name: string;
  relation: string;
  /** `ABCDE****F`. The number itself never leaves the backend (D11). */
  pan_masked: string;
  upi_id: string;
  /** Cash ASBA can freeze in *this* holder's account (D4). Not a shared pool. */
  available_balance: number;
  is_active: boolean;
}

export interface UserState {
  id: string;
  name: string;
  liquid_capital: number;
  demat_balance: number;
  capital_mode: CapitalMode;
  pans: Pan[];
  active_pan_count: number;
  committed_application_count: number;
}

export interface Ipo {
  id: string;
  name: string;
  symbol: string | null;
  issue_type: string;
  min_price: number;
  max_price: number;
  /** Retail bids at the top of the band, so this is what gets blocked (D7). */
  cutoff_price: number;
  lot_size: number;
  lot_cost: number;
  /** The rupee premium the user typed. `gmp_percent` is derived from it server-side. */
  latest_gmp: number;
  gmp_percent: number;
  expected_profit_per_lot: number;
  open_date: string;
  close_date: string;
  /** Null until the registrar fixes it (D10) — such an issue is unschedulable. */
  allotment_date: string | null;
  /** Allotment date + 1 (Rule 2, T+1). */
  unblock_date: string | null;
  listing_date: string | null;
  allotment_probability: number;
  priority_rank: number | null;
  schedulable: boolean;
  /** `user` typed it, `nse` imported it, `sample` is opt-in demo data. */
  source: string;
  /** Set on import: the lot size is an estimate, not the issuer's number. */
  needs_review: boolean;
  /** `user` typed the premium (authoritative) or `live` scraped it (unofficial). */
  gmp_source: string;
  /** Allotment and listing are T+3 guesses from the close date, not the registrar's. */
  dates_estimated: boolean;
  note: string | null;
  /**
   * What the issue still needs before it can be planned, decided by the server so
   * the reason lives in one place. Empty for a complete row.
   */
  missing: string[];
}

export interface ScheduleEvent {
  action_date: string;
  ipo_id: string;
  ipo_name: string;
  gmp_percent: number;
  lots_applied: number;
  /** One entry per PAN. Rule 1: one lot each, never two lots on one PAN. */
  pans_used: string[];
  blocked_amount: number;
  allotment_date: string;
  unblock_date: string;
  remaining_liquid_balance: number;
  expected_profit: number;
}

export interface SkippedIpo {
  ipo_id: string;
  ipo_name: string;
  gmp_percent: number;
  reason: string;
  lots_short_by: number;
}

/**
 * One day of the capital lifecycle — the matrix the user asked for. Emitted only for
 * dates where something happens, and derived from the same ledger the Gantt is, so a
 * row here can never contradict a bar there.
 */
export interface DayRow {
  date: string;
  blocked_today: number;
  total_locked: number;
  unblocked_today: number;
  allotments_finalized: string[];
  listings: string[];
  spendable_balance: number;
  actions: string[];
}

export interface ScheduleResult {
  initial_capital: number;
  pans_used: string[];
  policy: string;
  allotment_assumption: string;
  capital_mode: CapitalMode;
  events: ScheduleEvent[];
  skipped: SkippedIpo[];
  daily_timeline: DayRow[];
  total_expected_profit: number;
  peak_capital_deployed: number;
}

export interface Comparison {
  value_first: ScheduleResult;
  jit_greedy: ScheduleResult;
  /** Positive means ranking by GMP beat close-date order. See D1. */
  delta_expected_profit: number;
  /** False when capital is abundant — then the two policies necessarily agree. */
  capital_constrained: boolean;
}

export interface Application {
  id: string;
  ipo_name: string;
  pan_holder: string;
  pan_masked: string;
  lots_applied: number;
  blocked_amount: number;
  bid_date: string;
  unblock_date: string | null;
  allotment_status: string;
  /** `true` allotted, `false` not allotted, `null` the registrar has not said yet. */
  allotted: boolean | null;
}

export interface Health {
  status: string;
  database: string;
  gmp_provider: string;
  scheduler_default_policy: string;
  production_warnings: string[];
}

export interface PlanRequest {
  policy?: SchedulingPolicy;
  assumption?: AllotmentAssumption;
  min_gmp?: number;
  start_date?: string;
}

/* ------------------------------------------------------------------ writes --
 *
 * Money is sent as a string, not a number. The backend parses it with `Decimal`,
 * and a JSON number would already have been through a float by then — the exact
 * class of loss D3 exists to avoid. Reading money as a number is fine (it is only
 * displayed); writing it as one is not.
 */

/** One dated entry in a PAN's fund ledger. The balance is the running total (D18). */
export interface CashMovement {
  id: string;
  kind: "OPENING" | "DEPOSIT" | "WITHDRAWAL";
  amount: number;
  /** `amount` with its direction applied, so the UI needs no table of signs. */
  signed_amount: number;
  note: string | null;
  occurred_on: string;
  balance_after: number | null;
}

export interface PanLedgerData {
  pan_id: string;
  holder_name: string;
  pan_masked: string;
  /** Cash in the account, *not* net of pending ASBA blocks. */
  available_balance: number;
  movements: CashMovement[];
}

export interface PanInput {
  holder_name: string;
  relation: string;
  /** Plaintext, and the only place one exists client-side. Never stored or echoed. */
  pan_number: string;
  upi_id: string;
  linked_bank_name?: string;
  opening_balance?: string;
}

/**
 * A calendar row as the user edits it. Note the absence of `gmp_percent`: the user
 * types the rupee premium and the server derives the percentage the ranking uses.
 */
export interface IpoInput {
  name: string;
  symbol?: string | null;
  issue_type: "Mainboard" | "SME";
  min_price: string;
  max_price: string;
  lot_size: number;
  latest_gmp: string;
  open_date: string;
  close_date: string;
  allotment_date?: string | null;
  listing_date?: string | null;
  registrar_name?: string | null;
  allotment_probability?: string;
}

/** What a live GMP refresh changed, and what it deliberately left alone. */
export interface GmpRefresh {
  source: string;
  updated: string[];
  unchanged_because_edited: string[];
  unmatched: string[];
  quotes_seen: number;
  disclaimer: string;
}

export interface ImportSummary {
  source: string;
  imported: number;
  updated: number;
  skipped: { name: string; reason: string }[];
  /** Rows left alone because the user had edited them. Reported, not swallowed. */
  unchanged_because_edited: string[];
  needs_review: number;
  note: string;
}
