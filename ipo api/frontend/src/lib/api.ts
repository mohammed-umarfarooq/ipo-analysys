/**
 * Server-side client for the FastAPI backend.
 *
 * `server-only` makes the boundary a compile error rather than a convention: if
 * any of this is ever imported into a client component the build fails. That
 * matters because the backend currently has no authentication (see SECURITY.md).
 * The browser talks only to this app's own route handlers; those talk to FastAPI.
 * Nothing in the bundle knows the backend's address.
 */
import "server-only";

import type {
  Application,
  CapitalMode,
  Comparison,
  GmpRefresh,
  Health,
  ImportSummary,
  Ipo,
  IpoInput,
  Pan,
  PanInput,
  PanLedgerData,
  PlanRequest,
  ScheduleResult,
  UserState,
} from "@/lib/types";

const BASE = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";

/** Raised when the backend is unreachable or answers with an error status. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
    readonly detail?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Node's fetch reports every connection failure as the bare string
 * "fetch failed" and hides the reason one level down in `cause`. That distinction
 * is the whole diagnosis — ECONNREFUSED means "start uvicorn", ETIMEDOUT means
 * something quite different — so it is worth unwrapping.
 */
function describeFetchFailure(cause: unknown): string {
  if (!(cause instanceof Error)) return String(cause);
  const inner = (cause as { cause?: unknown }).cause;
  if (inner instanceof Error) {
    const code = (inner as { code?: string }).code;
    return code ? `${inner.message} (${code})` : inner.message;
  }
  return cause.message;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
      // Every figure here is derived from live balances and a dated IPO calendar.
      // A cached plan is a wrong plan, so nothing is cached.
      cache: "no-store",
    });
  } catch (cause) {
    throw new ApiError(
      `Cannot reach the scheduler API at ${BASE}. Is uvicorn running?`,
      null,
      describeFetchFailure(cause),
    );
  }

  if (!response.ok) {
    // FastAPI puts the human-readable reason in `detail`; a 409 from
    // /api/schedule means "no active PAN has any capital", which is a real
    // answer and worth showing verbatim rather than as "request failed".
    const body = await response.json().catch(() => null);
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : response.statusText;
    throw new ApiError(detail, response.status, detail);
  }

  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/api/health"),
  userState: () => request<UserState>("/api/user/state"),
  ipos: () => request<Ipo[]>("/api/ipos"),
  history: () => request<Application[]>("/api/portfolio/history"),

  schedule: (body: PlanRequest = {}) =>
    request<ScheduleResult>("/api/schedule", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Both policies in one call. The UI's policy toggle then switches between two
   * already-computed plans instead of re-planning, which keeps the D1 delta on
   * screen at all times rather than making the user remember the other number.
   */
  compare: (body: PlanRequest = {}) =>
    request<Comparison>("/api/schedule/compare", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Writes `ipo_applications`. Never exposed as a copilot tool — see SECURITY.md. */
  commit: (body: PlanRequest = {}) =>
    request<{ applications_created: number; already_recorded: number }>(
      "/api/schedule/commit",
      { method: "POST", body: JSON.stringify(body) },
    ),

  /* --------------------------------------------------------------- writes --
   *
   * None of these carry a user id: the backend resolves identity from its own
   * state. That is the same property the read methods rely on, and it matters
   * more here — the consequence of a caller-supplied identity would be a
   * modified portfolio, not merely a disclosed one.
   *
   * Like `commit`, none of these are reachable by the copilot. Its tools call
   * the read methods only.
   */

  patchUser: (body: { name?: string; demat_balance?: string; capital_mode?: CapitalMode }) =>
    request<UserState>("/api/user", { method: "PATCH", body: JSON.stringify(body) }),

  /** The plaintext PAN goes no further than this call; the reply is masked. */
  addPan: (body: PanInput) =>
    request<Pan>("/api/pans", { method: "POST", body: JSON.stringify(body) }),

  patchPan: (
    id: string,
    body: {
      holder_name?: string;
      relation?: string;
      upi_id?: string;
      linked_bank_name?: string;
      is_active?: boolean;
      balance?: string;
    },
  ) => request<Pan>(`/api/pans/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  /** 409 when the PAN has committed bids — deleting it would erase them (D19). */
  deletePan: (id: string) =>
    request<{ deleted: string }>(`/api/pans/${id}`, { method: "DELETE" }),

  ledger: (id: string) => request<PanLedgerData>(`/api/pans/${id}/movements`),

  addMovement: (
    id: string,
    body: { kind: "DEPOSIT" | "WITHDRAWAL"; amount: string; note?: string; occurred_on?: string },
  ) =>
    request<PanLedgerData>(`/api/pans/${id}/movements`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  deleteMovement: (id: string) =>
    request<PanLedgerData>(`/api/movements/${id}`, { method: "DELETE" }),

  addIpo: (body: IpoInput) =>
    request<Ipo>("/api/ipos", { method: "POST", body: JSON.stringify(body) }),

  /** Any edit promotes the row to `source: "user"`, so a re-import cannot undo it. */
  patchIpo: (id: string, body: Partial<IpoInput>) =>
    request<Ipo>(`/api/ipos/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  deleteIpo: (id: string) =>
    request<{ deleted: string }>(`/api/ipos/${id}`, { method: "DELETE" }),

  /** Live NSE calendar. Partial by construction — see the summary's `note`. */
  importIpos: () => request<ImportSummary>("/api/ipos/import", { method: "POST" }),

  /**
   * Grey-market premiums scraped from a public aggregator. Unofficial and best-effort:
   * a 502 here means the page moved, and typing the premium by hand still works. A
   * premium the user typed is never overwritten.
   */
  refreshGmp: () => request<GmpRefresh>("/api/ipos/refresh-gmp", { method: "POST" }),

  /** Record the registrar's result. `null` means "not known yet", not "not allotted". */
  patchApplication: (id: string, body: { allotted: boolean | null }) =>
    request<Application>(`/api/applications/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  sampleData: () =>
    request<{ pans_created: number; ipos_created: number }>("/api/demo/sample-data", {
      method: "POST",
    }),
};
