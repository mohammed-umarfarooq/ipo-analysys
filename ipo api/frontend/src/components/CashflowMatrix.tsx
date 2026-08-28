"use client";

import { inr, longDate, shortDate, todayIso } from "@/lib/format";
import type { DayRow } from "@/lib/types";

/**
 * The day-by-day cashflow matrix.
 *
 * The Gantt answers "which bids happen when". This answers the question that decides
 * whether a plan is executable at all: **how much cash is left on any given day, and
 * when does the frozen money come back?**
 *
 * Every column comes straight from the engine's committed ledger — nothing here is
 * re-derived in the browser. That is deliberate: a running total computed client-side
 * could drift from the schedule beside it, and the whole point of the table is that it
 * reconciles. Each row holds `total_locked = previous total + blocked − unblocked`.
 *
 * Rows exist only for dates where something happens. A quiet fortnight is not thirty
 * identical rows.
 */

const HEAD =
  "px-3 py-2 text-left text-[0.6875rem] font-medium uppercase tracking-wider text-slate-400";
const CELL = "px-3 py-2 align-top text-sm";

/** Blank cells read better as a dash than as ₹0 — nothing happened, rather than zero. */
function Amount({ value, tone }: { value: number; tone?: string }) {
  if (value === 0) return <span className="text-slate-600">—</span>;
  return <span className={`tnum ${tone ?? "text-slate-200"}`}>{inr(value)}</span>;
}

function Names({ items }: { items: string[] }) {
  if (items.length === 0) return <span className="text-slate-600">—</span>;
  return (
    <div className="space-y-0.5">
      {items.map((name) => (
        <div key={name} className="truncate text-[0.8125rem] text-slate-300">
          {name}
        </div>
      ))}
    </div>
  );
}

export function CashflowMatrix({
  timeline,
  initialCapital,
}: {
  timeline: DayRow[];
  initialCapital: number;
}) {
  if (timeline.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-[var(--color-hairline)] px-4 py-8 text-center text-sm text-slate-400">
        Nothing is scheduled yet, so there is no cashflow to show. Add capital to a PAN
        account or an issue to the calendar and the matrix fills in.
      </p>
    );
  }

  const today = todayIso();
  const lowest = Math.min(...timeline.map((row) => row.spendable_balance));
  const peak = Math.max(...timeline.map((row) => row.total_locked));

  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b border-[var(--color-hairline)]">
              <th className={HEAD}>Date</th>
              <th className={HEAD}>Action</th>
              <th className={`${HEAD} text-right`}>Blocked today</th>
              <th className={`${HEAD} text-right`}>Total locked</th>
              <th className={HEAD}>Allotment finalized</th>
              <th className={`${HEAD} text-right`}>Unblocked (T+1)</th>
              <th className={HEAD}>Listing</th>
              <th className={`${HEAD} text-right`}>Spendable balance</th>
            </tr>
          </thead>
          <tbody>
            {timeline.map((row) => {
              const isToday = row.date === today;
              return (
                <tr
                  key={row.date}
                  className={`border-b border-white/[0.04] ${
                    isToday ? "bg-sky-400/[0.06]" : "hover:bg-white/[0.02]"
                  }`}
                >
                  <td className={`${CELL} whitespace-nowrap`}>
                    <span className="tnum text-slate-200">{shortDate(row.date)}</span>
                    {isToday && (
                      <span className="ml-2 text-[0.6875rem] text-sky-300">today</span>
                    )}
                  </td>
                  <td className={CELL}>
                    <Names items={row.actions} />
                  </td>
                  <td className={`${CELL} text-right`}>
                    <Amount value={row.blocked_today} tone="text-amber-300" />
                  </td>
                  <td className={`${CELL} text-right`}>
                    <Amount
                      value={row.total_locked}
                      tone={
                        row.total_locked > 0 && row.total_locked === peak
                          ? "text-amber-200 font-medium"
                          : "text-slate-300"
                      }
                    />
                  </td>
                  <td className={CELL}>
                    <Names items={row.allotments_finalized} />
                  </td>
                  <td className={`${CELL} text-right`}>
                    <Amount value={row.unblocked_today} tone="text-emerald-300" />
                  </td>
                  <td className={CELL}>
                    <Names items={row.listings} />
                  </td>
                  <td className={`${CELL} text-right`}>
                    <span className="tnum font-medium text-slate-100">
                      {inr(row.spendable_balance)}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-[0.6875rem] text-slate-500">
        <span>
          Starting fund <span className="tnum text-slate-300">{inr(initialCapital)}</span>
        </span>
        <span>
          Peak locked <span className="tnum text-amber-200">{inr(peak)}</span>
        </span>
        <span>
          Lowest spendable <span className="tnum text-slate-300">{inr(lowest)}</span>
        </span>
        <span>
          {timeline.length} active day{timeline.length === 1 ? "" : "s"} between{" "}
          {longDate(timeline[0]!.date)} and {longDate(timeline.at(-1)!.date)}
        </span>
      </div>
    </div>
  );
}
