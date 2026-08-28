"use client";

import { inr, pct, shortDate, todayIso } from "@/lib/format";
import type { ScheduleEvent, UserState } from "@/lib/types";

/**
 * Per-PAN capital, and how hard the plan leans on each one.
 *
 * This panel exists because the single most expensive mistake in this domain is
 * treating the family's cash as one pool. An ASBA mandate freezes money in the
 * *applicant's own* bank account, so a bid under a parent's PAN cannot be funded
 * from your balance. The peak column is therefore per-PAN, not a share of a total.
 */
export function PanLedger({
  user,
  events,
}: {
  user: UserState;
  events: ScheduleEvent[];
}) {
  const active = user.pans.filter((p) => p.is_active);

  // Peak load per PAN: capital frozen is a step function that only rises when a
  // block starts, so evaluating it at every bid date finds the maximum exactly.
  const peaks = new Map<string, { amount: number; on: string }>();
  const probes = [todayIso(), ...events.map((e) => e.action_date)];
  for (const pan of active) {
    let best = { amount: 0, on: probes[0]! };
    for (const probe of probes) {
      const load = events.reduce((total, e) => {
        if (!e.pans_used.includes(pan.id)) return total;
        if (!(e.action_date <= probe && probe < e.unblock_date)) return total;
        return total + e.blocked_amount / e.lots_applied;
      }, 0);
      if (load > best.amount) best = { amount: load, on: probe };
    }
    peaks.set(pan.id, best);
  }

  const bidsFor = (panId: string) =>
    events.filter((e) => e.pans_used.includes(panId)).length;

  const busiest =
    events.length > 0
      ? [...peaks.values()].sort((a, b) => b.amount - a.amount)[0]
      : undefined;

  return (
    <div className="space-y-2.5">
      {active.map((pan) => {
        const peak = peaks.get(pan.id)!;
        const used =
          pan.available_balance > 0
            ? (peak.amount / pan.available_balance) * 100
            : 0;
        const bids = bidsFor(pan.id);
        return (
          <div key={pan.id}>
            <div className="flex items-baseline justify-between gap-3 text-sm">
              <div className="flex items-baseline gap-2">
                <span className="text-slate-200">{pan.holder_name}</span>
                <span className="text-xs text-slate-500">{pan.relation}</span>
                <span className="tnum text-xs text-slate-600">
                  {pan.pan_masked}
                </span>
              </div>
              <div className="tnum text-xs text-slate-400">
                {inr(peak.amount)} / {inr(pan.available_balance)}
              </div>
            </div>
            <div
              className="mt-1 h-1.5 overflow-hidden rounded-full bg-white/[0.06]"
              title={`Peak ${inr(peak.amount)} frozen on ${peak.on}, against a ${inr(pan.available_balance)} balance`}
            >
              <div
                className={`h-full rounded-full ${
                  used > 99.5 ? "bg-amber-400/80" : "bg-sky-400/70"
                }`}
                style={{ width: `${Math.min(used, 100)}%` }}
              />
            </div>
            <div className="mt-0.5 text-[0.6875rem] text-slate-500">
              {bids === 0
                ? "unused by this plan"
                : `${bids} bid${bids === 1 ? "" : "s"} · ${pct(used, 0)} of this account's cash at peak`}
            </div>
          </div>
        );
      })}

      {user.pans.length > active.length ? (
        <p className="pt-1 text-[0.6875rem] text-slate-500">
          {user.pans.length - active.length} inactive PAN
          {user.pans.length - active.length === 1 ? "" : "s"} excluded.
        </p>
      ) : null}

      <p className="border-t border-[var(--color-hairline)] pt-2.5 text-[0.6875rem] leading-relaxed text-slate-500">
        Balances are per account and cannot be pooled. Amounts are shown against the
        holder&apos;s own cash;{" "}
        {busiest ? (
          <>the busiest day in this plan is {shortDate(busiest.on)}.</>
        ) : (
          <>no capital is committed.</>
        )}
      </p>
    </div>
  );
}
