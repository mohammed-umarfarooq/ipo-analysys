import { inr, pct } from "@/lib/format";
import type { Comparison, ScheduleResult, UserState } from "@/lib/types";

function Kpi({
  label,
  value,
  sub,
  tone = "text-slate-100",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: string;
}) {
  return (
    <div className="flex-1 min-w-[9.5rem] rounded-lg border border-[var(--color-hairline)] bg-[var(--color-surface-raised)] px-4 py-3">
      <div className="text-[0.6875rem] uppercase tracking-wider text-slate-400">
        {label}
      </div>
      <div className={`tnum mt-1 text-2xl font-semibold ${tone}`}>{value}</div>
      {sub ? <div className="mt-0.5 text-xs text-slate-400">{sub}</div> : null}
    </div>
  );
}

export function KpiRibbon({
  user,
  plan,
  comparison,
}: {
  user: UserState;
  plan: ScheduleResult;
  comparison: Comparison;
}) {
  const bids = plan.events.reduce((n, e) => n + e.lots_applied, 0);
  const utilisation =
    user.liquid_capital > 0
      ? (plan.peak_capital_deployed / user.liquid_capital) * 100
      : 0;

  // Only meaningful when capital is actually scarce: with room for every issue
  // both policies bid on everything, so a zero delta says nothing about ranking.
  const delta = comparison.capital_constrained
    ? comparison.delta_expected_profit
    : null;

  return (
    <div className="flex flex-wrap gap-3">
      <Kpi
        label="Liquid capital"
        value={inr(user.liquid_capital)}
        sub={`${user.active_pan_count} active PAN${user.active_pan_count === 1 ? "" : "s"}, each frozen separately`}
      />
      <Kpi
        label="Planned bids"
        value={String(bids)}
        sub={`across ${plan.events.length} issue${plan.events.length === 1 ? "" : "s"}`}
      />
      <Kpi
        label="Expected gain"
        value={inr(plan.total_expected_profit)}
        tone="text-emerald-300"
        sub="at current GMP, before tax and charges"
      />
      <Kpi
        label="Peak capital frozen"
        value={inr(plan.peak_capital_deployed)}
        sub={`${pct(utilisation, 0)} of available cash at the busiest moment`}
      />
      <Kpi
        label="Value of ranking"
        value={delta === null ? "—" : `${delta > 0 ? "+" : ""}${inr(delta)}`}
        tone={delta && delta > 0 ? "text-amber-300" : "text-slate-400"}
        sub={
          delta === null
            ? "capital is not scarce; both policies fit every issue"
            : "vs. bidding in close-date order"
        }
      />
    </div>
  );
}
