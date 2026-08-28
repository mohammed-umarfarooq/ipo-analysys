"use client";

import { useState } from "react";

import { inr, pct, shortDate, todayIso } from "@/lib/format";
import type { GmpRefresh, ImportSummary, Ipo, IpoInput } from "@/lib/types";
import type { Debounce, Mutate } from "@/lib/usePortfolio";

/**
 * The IPO calendar, editable in place.
 *
 * Nothing here is hardcoded. Rows arrive from **Import from NSE**, from this table's
 * own add form, or from the opt-in sample data — and the `source` column records
 * which, because an imported row that has since been edited must never be silently
 * overwritten by the next import.
 *
 * The three fields NSE does not publish are the three that decide everything:
 *
 * * **lot size** — the issuer sets it; an import can only estimate from SEBI's
 *   minimum application value, so an estimated row is flagged, not trusted.
 * * **allotment date** — until the registrar fixes it, the freeze window has no end
 *   and the issue cannot be scheduled at all (D10). It stays NULL rather than guessed.
 * * **GMP** — no official feed exists anywhere. It is always your number.
 *
 * So each row says what it still needs. An issue missing something is not hidden:
 * it sits in the table with the gap named, which is the only way to know what to type.
 */

const MONEY = /^\d{1,12}(\.\d{1,2})?$/;
const ISO = /^\d{4}-\d{2}-\d{2}$/;

/**
 * The name column stays put while the rest scrolls.
 *
 * Nine editable fields plus three native date pickers is wider than the pane, and a
 * row of inputs with the issue name scrolled out of view is a row you cannot safely
 * edit. The colour is the section's own background — `surface-raised` at 60% over
 * `surface` — resolved to a literal, since a sticky cell has to be opaque.
 */
const STICKY = "sticky left-0 z-10 bg-[oklch(0.22_0.016_260)]";

const SOURCE_LABEL: Record<string, string> = {
  user: "typed here",
  nse: "NSE",
  sample: "sample",
};

/**
 * An uncontrolled input that saves after typing stops.
 *
 * Uncontrolled on purpose. A controlled value re-synced from the server would fight
 * the user whenever a background refresh landed mid-keystroke — the field would jump
 * back to the saved value with the caret in the wrong place. `defaultValue` seeds it
 * once; the server is authoritative on reload, not on every render.
 */
function Cell({
  value,
  onCommit,
  valid,
  type = "text",
  className = "",
  placeholder,
  ariaLabel,
}: {
  value: string;
  onCommit: (next: string) => void;
  /** Reject a half-typed value rather than sending a 400 on every keystroke. */
  valid?: (next: string) => boolean;
  type?: "text" | "date";
  className?: string;
  placeholder?: string;
  ariaLabel: string;
}) {
  const [bad, setBad] = useState(false);
  return (
    <input
      type={type}
      defaultValue={value}
      placeholder={placeholder}
      aria-label={ariaLabel}
      aria-invalid={bad || undefined}
      onChange={(event) => {
        const next = event.target.value;
        const ok = !valid || valid(next);
        setBad(!ok);
        if (ok) onCommit(next);
      }}
      className={`min-w-0 rounded border bg-transparent px-1 py-0.5 text-xs text-slate-200 outline-none transition-colors focus:bg-black/30 ${
        bad
          ? "border-rose-400/60"
          : "border-transparent hover:border-[var(--color-hairline)] focus:border-slate-500"
      } ${className}`}
    />
  );
}

const BLANK: IpoInput = {
  name: "",
  issue_type: "Mainboard",
  min_price: "",
  max_price: "",
  lot_size: 0,
  latest_gmp: "0",
  open_date: todayIso(),
  close_date: todayIso(),
  allotment_date: null,
  // A retail lottery allots to a minority of applicants. This only affects how much
  // capital stays debited after allotment, never the expected gain.
  allotment_probability: "0.05",
};

export function IpoCalendarEditor({
  ipos,
  mutate,
  debounce,
}: {
  ipos: Ipo[];
  mutate: Mutate;
  debounce: Debounce;
}) {
  const [draft, setDraft] = useState<IpoInput | null>(null);
  const [summary, setSummary] = useState<ImportSummary | null>(null);
  const [importing, setImporting] = useState(false);
  const [gmp, setGmp] = useState<GmpRefresh | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  /** Patch one field. Structural enough to need the server's derived columns back. */
  const patch = (id: string, field: string, next: unknown) =>
    debounce(`ipo:${id}:${field}`, () =>
      mutate("/api/calendar", { action: "patch-ipo", id, [field]: next }, { refresh: true }),
    );

  async function runImport() {
    setImporting(true);
    const result = await mutate<ImportSummary>(
      "/api/calendar",
      { action: "import" },
      { refresh: true },
    );
    setImporting(false);
    if (result) setSummary(result);
  }

  /**
   * Pull live premiums. Best-effort by nature: there is no official GMP feed anywhere,
   * so this scrapes a public aggregator and a failure is a 502 the user can read. A
   * premium you typed is never overwritten — the backend reports those separately.
   */
  async function refreshGmp() {
    setRefreshing(true);
    const result = await mutate<GmpRefresh>(
      "/api/calendar",
      { action: "refresh-gmp" },
      { refresh: true },
    );
    setRefreshing(false);
    if (result) setGmp(result);
  }

  async function addIssue() {
    if (!draft) return;
    const ok =
      draft.name.trim() &&
      MONEY.test(draft.min_price) &&
      MONEY.test(draft.max_price) &&
      draft.lot_size > 0 &&
      ISO.test(draft.open_date) &&
      ISO.test(draft.close_date);
    if (!ok) return;
    const created = await mutate<Ipo>(
      "/api/calendar",
      { action: "add-ipo", ...draft, allotment_date: draft.allotment_date || null },
      { refresh: true },
    );
    if (created) setDraft(null);
  }

  const incomplete = ipos.filter((ipo) => ipo.missing.length > 0).length;

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button
          onClick={() => void runImport()}
          disabled={importing}
          className="rounded-lg border border-[var(--color-hairline)] px-3 py-1.5 text-xs text-slate-200 transition-colors hover:bg-white/5 disabled:opacity-50"
          title="Live upcoming issues from NSE. Lot size is estimated and GMP is never published."
        >
          {importing ? "Fetching from NSE…" : "⟳ Import from NSE"}
        </button>
        <button
          onClick={() => void refreshGmp()}
          disabled={refreshing || ipos.length === 0}
          className="rounded-lg border border-[var(--color-hairline)] px-3 py-1.5 text-xs text-slate-200 transition-colors hover:bg-white/5 disabled:opacity-50"
          title="Grey-market premiums from a public aggregator. Unofficial and unregulated; a premium you typed is never overwritten."
        >
          {refreshing ? "Fetching premiums…" : "⟳ Refresh GMP (live)"}
        </button>
        <button
          onClick={() => setDraft(draft ? null : { ...BLANK })}
          className="rounded-lg border border-[var(--color-hairline)] px-3 py-1.5 text-xs text-slate-200 transition-colors hover:bg-white/5"
        >
          {draft ? "Cancel" : "+ Add issue"}
        </button>
        <span className="text-xs text-slate-500">
          {ipos.length} issue{ipos.length === 1 ? "" : "s"}
          {incomplete > 0 ? ` · ${incomplete} need your input` : ""}
        </span>
      </div>

      {gmp ? (
        <div className="mb-3 rounded-lg border border-violet-400/30 bg-violet-400/[0.06] px-3 py-2 text-xs text-violet-100/80">
          <p>
            <span className="font-medium text-violet-200">
              {gmp.updated.length} premium{gmp.updated.length === 1 ? "" : "s"} updated
            </span>
            {` from ${gmp.quotes_seen} quote${gmp.quotes_seen === 1 ? "" : "s"}`}
            {gmp.unchanged_because_edited.length > 0
              ? ` · ${gmp.unchanged_because_edited.length} left alone because you typed your own`
              : ""}
            {gmp.unmatched.length > 0 ? ` · no quote for ${gmp.unmatched.length}` : ""}
          </p>
          <p className="mt-1 text-violet-100/60">{gmp.disclaimer}</p>
        </div>
      ) : null}

      {summary ? (
        <div className="mb-3 rounded-lg border border-sky-400/30 bg-sky-400/[0.06] px-3 py-2 text-xs text-sky-100/80">
          <p>
            <span className="font-medium text-sky-200">
              {summary.imported} new, {summary.updated} updated
            </span>
            {summary.needs_review > 0 ? ` · ${summary.needs_review} need review` : ""}
            {summary.unchanged_because_edited.length > 0
              ? ` · ${summary.unchanged_because_edited.length} left alone because you had edited them`
              : ""}
          </p>
          <p className="mt-1 text-sky-100/60">{summary.note}</p>
          {summary.skipped.length > 0 ? (
            <ul className="mt-1 space-y-0.5 text-sky-100/50">
              {summary.skipped.map((s) => (
                <li key={s.name}>
                  · {s.name} — {s.reason}
                </li>
              ))}
            </ul>
          ) : null}
          <button
            onClick={() => setSummary(null)}
            className="mt-1 text-sky-300/70 hover:text-sky-200"
          >
            Dismiss
          </button>
        </div>
      ) : null}

      {ipos.length === 0 && !draft ? (
        <p className="rounded-lg border border-dashed border-[var(--color-hairline)] px-4 py-8 text-center text-sm text-slate-400">
          The calendar is empty. Import the live NSE list, or add the issue you are
          tracking — nothing is invented for you.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[54rem] border-collapse text-xs">
            <thead>
              <tr className="border-b border-[var(--color-hairline)] text-left text-[0.6875rem] uppercase tracking-wider text-slate-400">
                <th className={`${STICKY} py-2 pr-3 font-medium`}>Issue</th>
                <th className="py-2 pr-2 font-medium">Type</th>
                <th className="py-2 pr-2 font-medium">Band ₹</th>
                <th className="py-2 pr-2 font-medium">Lot</th>
                <th className="py-2 pr-2 font-medium" title="Rupee premium per share">
                  GMP ₹
                </th>
                <th className="py-2 pr-2 text-right font-medium">Lot cost</th>
                <th className="py-2 pr-2 font-medium">Opens</th>
                <th className="py-2 pr-2 font-medium">Closes</th>
                <th className="py-2 pr-2 font-medium">Allotment</th>
                <th
                  className="py-2 pr-2 font-medium"
                  title="Share of bids you expect to be allotted. Allotted money is debited and never returns."
                >
                  P(allot)
                </th>
                <th className="py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {ipos.map((ipo) => (
                <tr
                  key={ipo.id}
                  className="border-b border-[var(--color-hairline)]/50 align-top"
                >
                  <td className={`${STICKY} py-1.5 pr-3`}>
                    <Cell
                      ariaLabel="Issue name"
                      value={ipo.name}
                      valid={(next) => next.trim().length > 0}
                      onCommit={(next) => patch(ipo.id, "name", next)}
                      className="w-36"
                    />
                    {/* One badge, not three. The gaps are read together — "what is
                        this row still missing" — and three stacked chips make a
                        nine-row table twice as tall for the same sentence. */}
                    {ipo.missing.length > 0 ? (
                      <div className="mt-0.5">
                        <span className="rounded bg-amber-400/15 px-1 py-px text-[0.625rem] text-amber-200">
                          ⚠ needs {ipo.missing.join(", ")}
                        </span>
                      </div>
                    ) : ipo.needs_review ? (
                      <div className="mt-0.5">
                        <span className="rounded bg-amber-400/10 px-1 py-px text-[0.625rem] text-amber-200/70">
                          check the estimate
                        </span>
                      </div>
                    ) : null}
                    {/* Provenance, not completeness: these rows *are* plannable, they
                        just rest on a scrape or on T+3 arithmetic rather than on a
                        published figure. Editing either field clears the badge. */}
                    {ipo.gmp_source === "live" || ipo.dates_estimated ? (
                      <div className="mt-0.5 flex flex-wrap gap-1">
                        {ipo.gmp_source === "live" ? (
                          <span
                            title="Grey-market premium scraped from a public aggregator. Unofficial and unregulated — type your own to override it."
                            className="rounded bg-violet-400/15 px-1 py-px text-[0.625rem] text-violet-200"
                          >
                            live · unofficial
                          </span>
                        ) : null}
                        {ipo.dates_estimated ? (
                          <span
                            title="Allotment and listing estimated from the close date under SEBI T+3, not published by the registrar. Confirming either date clears this."
                            className="rounded bg-sky-400/15 px-1 py-px text-[0.625rem] text-sky-200"
                          >
                            est. T+3
                          </span>
                        ) : null}
                      </div>
                    ) : null}
                    <div className="mt-0.5 text-[0.625rem] text-slate-600">
                      {SOURCE_LABEL[ipo.source] ?? ipo.source}
                    </div>
                  </td>
                  <td className="py-1.5 pr-2">
                    <select
                      value={ipo.issue_type === "SME" ? "SME" : "Mainboard"}
                      aria-label="Issue type"
                      onChange={(event) => patch(ipo.id, "issue_type", event.target.value)}
                      className="rounded border border-transparent bg-transparent py-0.5 text-xs text-slate-200 outline-none hover:border-[var(--color-hairline)] focus:border-slate-500"
                    >
                      <option value="Mainboard">Mainboard</option>
                      <option value="SME">SME</option>
                    </select>
                  </td>
                  <td className="py-1.5 pr-2">
                    <div className="flex items-center gap-0.5">
                      <Cell
                        ariaLabel="Minimum price"
                        value={String(ipo.min_price)}
                        valid={(next) => MONEY.test(next)}
                        onCommit={(next) => patch(ipo.id, "min_price", next)}
                        className="tnum w-14"
                      />
                      <span className="text-slate-600">–</span>
                      <Cell
                        ariaLabel="Maximum price"
                        value={String(ipo.max_price)}
                        valid={(next) => MONEY.test(next)}
                        onCommit={(next) => patch(ipo.id, "max_price", next)}
                        className="tnum w-14"
                      />
                    </div>
                    <div className="mt-0.5 text-[0.625rem] text-slate-600">
                      cut-off {inr(ipo.cutoff_price)}
                    </div>
                  </td>
                  <td className="py-1.5 pr-2">
                    <Cell
                      ariaLabel="Lot size"
                      value={String(ipo.lot_size)}
                      valid={(next) => /^\d{1,6}$/.test(next) && Number(next) > 0}
                      onCommit={(next) => patch(ipo.id, "lot_size", Number(next))}
                      className="tnum w-12"
                    />
                  </td>
                  <td className="py-1.5 pr-2">
                    <Cell
                      ariaLabel="Grey market premium in rupees"
                      value={String(ipo.latest_gmp)}
                      valid={(next) => MONEY.test(next)}
                      onCommit={(next) => patch(ipo.id, "latest_gmp", next)}
                      className="tnum w-14"
                    />
                    <div className="mt-0.5 text-[0.625rem] text-slate-600">
                      {pct(ipo.gmp_percent)}
                    </div>
                  </td>
                  <td className="tnum py-1.5 pr-2 text-right text-slate-300">
                    {inr(ipo.lot_cost)}
                    <div className="text-[0.625rem] text-slate-600">
                      +{inr(ipo.expected_profit_per_lot)}
                    </div>
                  </td>
                  <td className="py-1.5 pr-2">
                    <Cell
                      type="date"
                      ariaLabel="Open date"
                      value={ipo.open_date}
                      valid={(next) => ISO.test(next)}
                      onCommit={(next) => patch(ipo.id, "open_date", next)}
                      className="tnum w-[6.5rem]"
                    />
                  </td>
                  <td className="py-1.5 pr-2">
                    <Cell
                      type="date"
                      ariaLabel="Close date"
                      value={ipo.close_date}
                      valid={(next) => ISO.test(next)}
                      onCommit={(next) => patch(ipo.id, "close_date", next)}
                      className="tnum w-[6.5rem]"
                    />
                  </td>
                  <td className="py-1.5 pr-2">
                    <Cell
                      type="date"
                      ariaLabel="Allotment date"
                      value={ipo.allotment_date ?? ""}
                      valid={(next) => next === "" || ISO.test(next)}
                      onCommit={(next) => patch(ipo.id, "allotment_date", next || null)}
                      className={`tnum w-[6.5rem] ${ipo.allotment_date ? "" : "border-amber-400/40"}`}
                    />
                    <div className="mt-0.5 text-[0.625rem] text-slate-600">
                      {ipo.unblock_date
                        ? `unblocks ${shortDate(ipo.unblock_date)}`
                        : "unschedulable"}
                    </div>
                  </td>
                  <td className="py-1.5 pr-2">
                    <Cell
                      ariaLabel="Allotment probability"
                      value={ipo.allotment_probability.toFixed(3)}
                      valid={(next) => /^(0(\.\d{1,3})?|1(\.0{1,3})?)$/.test(next)}
                      onCommit={(next) => patch(ipo.id, "allotment_probability", next)}
                      className="tnum w-12"
                    />
                  </td>
                  <td className="py-1.5">
                    <button
                      onClick={() =>
                        void mutate(
                          "/api/calendar",
                          { action: "delete-ipo", id: ipo.id },
                          { refresh: true },
                        )
                      }
                      title="Remove this issue. Refused if a committed bid references it."
                      className="text-slate-600 transition-colors hover:text-rose-300"
                    >
                      ✕
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {draft ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void addIssue();
          }}
          className="mt-3 rounded-lg border border-[var(--color-hairline)] bg-black/20 p-3"
        >
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <label className="text-[0.6875rem] text-slate-400">
              Issue name
              <input
                autoFocus
                value={draft.name}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                className="mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
            </label>
            <label className="text-[0.6875rem] text-slate-400">
              Type
              <select
                value={draft.issue_type}
                onChange={(event) =>
                  setDraft({ ...draft, issue_type: event.target.value as "Mainboard" | "SME" })
                }
                className="mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              >
                <option value="Mainboard">Mainboard</option>
                <option value="SME">SME</option>
              </select>
            </label>
            <label className="text-[0.6875rem] text-slate-400">
              Price band (low – high)
              <div className="mt-0.5 flex items-center gap-1">
                <input
                  value={draft.min_price}
                  inputMode="decimal"
                  onChange={(event) => setDraft({ ...draft, min_price: event.target.value })}
                  className="tnum min-w-0 flex-1 rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
                />
                <input
                  value={draft.max_price}
                  inputMode="decimal"
                  onChange={(event) => setDraft({ ...draft, max_price: event.target.value })}
                  className="tnum min-w-0 flex-1 rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
                />
              </div>
            </label>
            <label className="text-[0.6875rem] text-slate-400">
              Lot size (shares)
              <input
                value={draft.lot_size || ""}
                inputMode="numeric"
                onChange={(event) =>
                  setDraft({ ...draft, lot_size: Number(event.target.value) || 0 })
                }
                className="tnum mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
            </label>
            <label className="text-[0.6875rem] text-slate-400">
              GMP per share ₹
              <input
                value={draft.latest_gmp}
                inputMode="decimal"
                onChange={(event) => setDraft({ ...draft, latest_gmp: event.target.value })}
                className="tnum mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
            </label>
            <label className="text-[0.6875rem] text-slate-400">
              Opens
              <input
                type="date"
                value={draft.open_date}
                onChange={(event) => setDraft({ ...draft, open_date: event.target.value })}
                className="tnum mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
            </label>
            <label className="text-[0.6875rem] text-slate-400">
              Closes
              <input
                type="date"
                value={draft.close_date}
                onChange={(event) => setDraft({ ...draft, close_date: event.target.value })}
                className="tnum mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
            </label>
            <label className="text-[0.6875rem] text-slate-400">
              Allotment (leave blank if unknown)
              <input
                type="date"
                value={draft.allotment_date ?? ""}
                onChange={(event) =>
                  setDraft({ ...draft, allotment_date: event.target.value || null })
                }
                className="tnum mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
            </label>
          </div>
          <div className="mt-2 flex items-center gap-2">
            <button
              type="submit"
              className="rounded-lg bg-slate-200 px-3 py-1.5 text-xs font-medium text-slate-900"
            >
              Add to calendar
            </button>
            <p className="text-[0.625rem] leading-snug text-slate-500">
              An issue with no allotment date is saved and listed, but cannot be
              scheduled — the freeze window has no end until the registrar fixes one.
            </p>
          </div>
        </form>
      ) : null}
    </div>
  );
}
