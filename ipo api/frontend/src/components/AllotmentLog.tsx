"use client";

import { inr, shortDate } from "@/lib/format";
import type { Application } from "@/lib/types";
import type { Mutate } from "@/lib/usePortfolio";

/**
 * Committed bids, with the registrar's result recorded against each.
 *
 * The result is published nowhere this program can read — it arrives days after the
 * bid, on the registrar's own site — so it is typed in. Recording it is what turns a
 * plan into a history: an allotted bid kept its capital and will list, a rejected one
 * gave the money back the morning after allotment.
 *
 * Three states, not two. "Not allotted" and "not known yet" are different facts and
 * only the first one means the money is back, so a bare checkbox would have to lie
 * about one of them. Clicking the current state clears it back to unknown, which makes
 * a mis-click undoable.
 */

const STATES = [
  { value: true as boolean | null, label: "Allotted", tone: "bg-emerald-400 text-slate-900" },
  { value: false as boolean | null, label: "Not allotted", tone: "bg-slate-300 text-slate-900" },
];

function Tick({
  application,
  mutate,
}: {
  application: Application;
  mutate: Mutate;
}) {
  const set = (next: boolean | null) =>
    void mutate(
      "/api/portfolio",
      { action: "patch-application", id: application.id, allotted: next },
      { refresh: true },
    );

  return (
    <div className="flex gap-1">
      {STATES.map((state) => {
        const active = application.allotted === state.value;
        return (
          <button
            key={state.label}
            role="checkbox"
            aria-checked={active}
            aria-label={`${state.label}: ${application.ipo_name} on ${application.pan_masked}`}
            // Clicking the active state clears it — the escape hatch from a mis-tick.
            onClick={() => set(active ? null : state.value)}
            className={`rounded-md border px-2 py-1 text-[0.6875rem] transition-colors ${
              active
                ? `border-transparent font-medium ${state.tone}`
                : "border-[var(--color-hairline)] text-slate-300 hover:bg-white/5"
            }`}
          >
            {active ? "✓ " : ""}
            {state.label}
          </button>
        );
      })}
    </div>
  );
}

export function AllotmentLog({
  applications,
  mutate,
}: {
  applications: Application[];
  mutate: Mutate;
}) {
  const decided = applications.filter((a) => a.allotted !== null).length;

  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr className="border-b border-[var(--color-hairline)]">
              {["Issue", "PAN", "Bid", "Blocked", "Unblocks", "Result"].map((head) => (
                <th
                  key={head}
                  className="px-3 py-2 text-left text-[0.6875rem] font-medium uppercase tracking-wider text-slate-400"
                >
                  {head}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {applications.map((row) => (
              <tr key={row.id} className="border-b border-white/[0.04] hover:bg-white/[0.02]">
                <td className="px-3 py-2 text-sm text-slate-200">{row.ipo_name}</td>
                <td className="px-3 py-2 text-sm text-slate-400">
                  {row.pan_holder}
                  <span className="tnum ml-2 text-[0.6875rem] text-slate-600">
                    {row.pan_masked}
                  </span>
                </td>
                <td className="tnum px-3 py-2 text-sm text-slate-300">
                  {shortDate(row.bid_date)}
                </td>
                <td className="tnum px-3 py-2 text-sm text-slate-300">
                  {inr(row.blocked_amount)}
                </td>
                <td className="tnum px-3 py-2 text-sm text-slate-400">
                  {row.unblock_date ? shortDate(row.unblock_date) : "—"}
                </td>
                <td className="px-3 py-2">
                  <Tick application={row} mutate={mutate} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-[0.6875rem] text-slate-500">
        {decided} of {applications.length} result{applications.length === 1 ? "" : "s"} recorded.
        Untouched rows are &ldquo;not known yet&rdquo; — the registrar has not published.
      </p>
    </div>
  );
}
