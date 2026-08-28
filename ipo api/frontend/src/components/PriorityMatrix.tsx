import { gmpTone, inr, pct, shortDate } from "@/lib/format";
import type { Ipo, ScheduleResult } from "@/lib/types";

/**
 * Every issue on the board, in the order the engine considers them (Rule 3: GMP
 * descending, then allotment date), with what was decided about each and why.
 *
 * An IPO that was silently dropped is indistinguishable from one that does not
 * exist, so a skipped issue keeps its row and carries its reason.
 */
export function PriorityMatrix({
  ipos,
  plan,
}: {
  ipos: Ipo[];
  plan: ScheduleResult;
}) {
  const scheduled = new Map(plan.events.map((e) => [e.ipo_id, e]));
  const skipped = new Map(plan.skipped.map((s) => [s.ipo_id, s]));

  // Unranked issues (no allotment date yet) sort last — they are not candidates.
  const rows = [...ipos].sort(
    (a, b) =>
      (a.priority_rank ?? Number.MAX_SAFE_INTEGER) -
      (b.priority_rank ?? Number.MAX_SAFE_INTEGER),
  );

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[52rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-[var(--color-hairline)] text-left text-[0.6875rem] uppercase tracking-wider text-slate-400">
            <th className="py-2 pr-3 font-medium">#</th>
            <th className="py-2 pr-3 font-medium">Issue</th>
            <th className="py-2 pr-3 text-right font-medium">GMP</th>
            <th className="py-2 pr-3 text-right font-medium">Lot cost</th>
            <th className="py-2 pr-3 text-right font-medium">Gain / lot</th>
            <th className="py-2 pr-3 font-medium">Closes</th>
            <th className="py-2 pr-3 font-medium">Unblocks</th>
            <th className="py-2 font-medium">Decision</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((ipo) => {
            const event = scheduled.get(ipo.id);
            const miss = skipped.get(ipo.id);
            return (
              <tr
                key={ipo.id}
                className={`border-b border-[var(--color-hairline)]/50 align-top ${
                  event ? "" : "text-slate-500"
                }`}
              >
                <td className="tnum py-2.5 pr-3 text-slate-500">
                  {ipo.priority_rank ?? "—"}
                </td>
                <td className="py-2.5 pr-3">
                  <div className="flex items-center gap-2">
                    <span
                      className={event ? "text-slate-100" : "text-slate-400"}
                    >
                      {ipo.name}
                    </span>
                    {ipo.issue_type === "SME" ? (
                      <span className="rounded border border-violet-400/40 px-1 py-px text-[0.625rem] uppercase text-violet-300">
                        SME
                      </span>
                    ) : null}
                  </div>
                  {miss ? (
                    <div className="mt-0.5 max-w-lg text-xs leading-snug text-slate-500">
                      {miss.reason}
                      {miss.lots_short_by > 0 ? (
                        <> Short by {inr(miss.lots_short_by)}.</>
                      ) : null}
                    </div>
                  ) : null}
                </td>
                <td
                  className={`tnum py-2.5 pr-3 text-right ${event ? gmpTone(ipo.gmp_percent) : ""}`}
                >
                  {pct(ipo.gmp_percent)}
                </td>
                <td className="tnum py-2.5 pr-3 text-right">
                  {inr(ipo.lot_cost)}
                </td>
                <td className="tnum py-2.5 pr-3 text-right">
                  {inr(ipo.expected_profit_per_lot)}
                </td>
                <td className="tnum py-2.5 pr-3">{shortDate(ipo.close_date)}</td>
                <td className="tnum py-2.5 pr-3">
                  {ipo.unblock_date ? shortDate(ipo.unblock_date) : "—"}
                </td>
                <td className="py-2.5">
                  {event ? (
                    <span className="whitespace-nowrap rounded bg-emerald-400/15 px-2 py-0.5 text-xs text-emerald-300">
                      {event.lots_applied} lot
                      {event.lots_applied === 1 ? "" : "s"} ·{" "}
                      {inr(event.blocked_amount)}
                    </span>
                  ) : (
                    <span className="whitespace-nowrap rounded bg-white/5 px-2 py-0.5 text-xs text-slate-400">
                      No bid
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-3 text-[0.6875rem] leading-relaxed text-slate-500">
        Lot cost is the cut-off price (top of the band) times the lot size — that is
        what ASBA actually freezes. One lot per PAN per issue: extra lots under the
        same PAN cannot improve the odds in a SEBI retail lottery.
      </p>
    </div>
  );
}
