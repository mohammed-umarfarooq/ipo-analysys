"use client";

import { useCallback, useState } from "react";

import { AllotmentLog } from "@/components/AllotmentLog";
import { CashflowMatrix } from "@/components/CashflowMatrix";
import { ChatDrawer } from "@/components/ChatDrawer";
import { GanttSchedule } from "@/components/GanttSchedule";
import { InputsRail, type PlanInputs } from "@/components/InputsRail";
import { IpoCalendarEditor } from "@/components/IpoCalendarEditor";
import { KpiRibbon } from "@/components/KpiRibbon";
import { PanLedger } from "@/components/PanLedger";
import { PriorityMatrix } from "@/components/PriorityMatrix";
import { inr, todayIso } from "@/lib/format";
import type {
  Application,
  Comparison,
  Ipo,
  PlanRequest,
  ScheduleResult,
  UserState,
} from "@/lib/types";
import { useDebouncedSave, usePortfolio } from "@/lib/usePortfolio";

/**
 * The shell: a docked rail of inputs, and a pane of everything derived from them.
 *
 * `initialComparison` is nullable because an empty portfolio is a legitimate state
 * and now the *first* one. The engine returns 409 for "no active PAN has any
 * capital" — correct, because that is a different answer from "nothing was
 * affordable" — but it is a starting point to be filled in, not an error page. So a
 * missing plan renders as zeros beside the inputs that will produce a real one.
 */

const EMPTY_PLAN: ScheduleResult = {
  initial_capital: 0,
  pans_used: [],
  policy: "value_first",
  allotment_assumption: "expected",
  capital_mode: "pooled",
  events: [],
  skipped: [],
  daily_timeline: [],
  total_expected_profit: 0,
  peak_capital_deployed: 0,
};

const EMPTY_COMPARISON: Comparison = {
  value_first: EMPTY_PLAN,
  jit_greedy: EMPTY_PLAN,
  delta_expected_profit: 0,
  capital_constrained: false,
};

/** Planning parameters as the API wants them. Blank fields are simply absent. */
function toRequest(inputs: PlanInputs): PlanRequest {
  const floor = Number(inputs.minGmp);
  return {
    assumption: inputs.assumption,
    min_gmp:
      inputs.minGmp.trim() !== "" && Number.isFinite(floor) && floor >= 0 && floor <= 100
        ? floor
        : undefined,
    start_date: /^\d{4}-\d{2}-\d{2}$/.test(inputs.startDate) ? inputs.startDate : undefined,
  };
}

function Section({
  title,
  hint,
  children,
  aside,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
  aside?: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-[var(--color-hairline)] bg-[var(--color-surface-raised)]/60 p-5">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-medium text-slate-100">{title}</h2>
          {hint ? (
            <p className="mt-0.5 max-w-2xl text-xs leading-relaxed text-slate-400">{hint}</p>
          ) : null}
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Dashboard({
  user,
  ipos,
  applications,
  initialComparison,
  chatEnabled,
  warnings,
}: {
  user: UserState;
  ipos: Ipo[];
  applications: Application[];
  initialComparison: Comparison | null;
  chatEnabled: boolean;
  warnings: string[];
}) {
  const { comparison, saveState, error, busy, setError, mutate, replan } =
    usePortfolio(initialComparison);
  const debounce = useDebouncedSave();

  const [inputs, setInputs] = useState<PlanInputs>({
    policy: "value_first",
    assumption: "expected",
    minGmp: "",
    startDate: todayIso(),
  });
  const [confirming, setConfirming] = useState(false);
  const [committed, setCommitted] = useState<string | null>(null);

  const plan =
    comparison === null
      ? EMPTY_PLAN
      : inputs.policy === "value_first"
        ? comparison.value_first
        : comparison.jit_greedy;
  const bids = plan.events.reduce((n, e) => n + e.lots_applied, 0);

  /**
   * Switching policy needs no request — both plans arrive in one response. The
   * assumption changes what the engine computes, so it re-plans immediately; the
   * two typed fields wait for typing to stop.
   */
  const changePlan = useCallback(
    (patch: Partial<PlanInputs>) => {
      setInputs((previous) => {
        const next = { ...previous, ...patch };
        setCommitted(null);
        if ("assumption" in patch) void replan(toRequest(next));
        else if ("minGmp" in patch || "startDate" in patch)
          debounce("plan", () => void replan(toRequest(next)));
        return next;
      });
    },
    [debounce, replan],
  );

  async function commit() {
    setError(null);
    try {
      const response = await fetch("/api/commit", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ policy: inputs.policy, ...toRequest(inputs) }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error ?? "Commit failed.");
      setCommitted(
        body.applications_created === 0
          ? "Already committed — nothing new to record."
          : `Recorded ${body.applications_created} application${body.applications_created === 1 ? "" : "s"}.`,
      );
      await replan();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setConfirming(false);
    }
  }

  const dimmed = busy ? "opacity-50 transition-opacity" : "";

  return (
    <main className="mx-auto max-w-[88rem] px-6 py-6 pb-24">
      <header className="mb-5">
        <h1 className="text-lg font-semibold text-slate-50">
          IPO Copilot &amp; Cashflow Scheduler
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          {user.name} · {user.active_pan_count} active PAN
          {user.active_pan_count === 1 ? "" : "s"} · {inr(user.liquid_capital)} of
          ASBA-freezable cash · {user.committed_application_count} committed application
          {user.committed_application_count === 1 ? "" : "s"}
        </p>
      </header>

      {warnings.length > 0 ? (
        <div className="mb-5 rounded-lg border border-amber-400/30 bg-amber-400/[0.07] px-4 py-3">
          <p className="text-xs font-medium text-amber-200">Development configuration</p>
          <ul className="mt-1 space-y-0.5 text-xs text-amber-100/70">
            {warnings.map((w) => (
              <li key={w}>· {w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {error ? (
        <div className="mb-5 flex items-start justify-between gap-3 rounded-lg border border-rose-400/40 bg-rose-400/10 px-4 py-3">
          <p className="text-sm text-rose-200">{error}</p>
          <button
            onClick={() => setError(null)}
            className="shrink-0 text-xs text-rose-300/70 hover:text-rose-200"
          >
            Dismiss
          </button>
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[22rem_1fr] lg:items-start">
        <InputsRail
          user={user}
          ipos={ipos}
          plan={inputs}
          onPlanChange={changePlan}
          mutate={mutate}
          debounce={debounce}
          saveState={saveState}
        />

        <div className="min-w-0 space-y-5">
          <KpiRibbon
            user={user}
            plan={plan}
            comparison={comparison ?? EMPTY_COMPARISON}
          />

          <Section
            title="Capital timeline"
            hint="One bar per ASBA block, from the bid date until the morning after allotment."
            aside={
              <div className="flex items-center gap-2">
                {committed ? (
                  <span className="text-xs text-emerald-300">{committed}</span>
                ) : null}
                {confirming ? (
                  <>
                    <button
                      onClick={() => void commit()}
                      disabled={busy}
                      className="rounded-lg bg-amber-400 px-3 py-1.5 text-xs font-medium text-slate-900 disabled:opacity-50"
                    >
                      Confirm {bids} bid{bids === 1 ? "" : "s"}
                    </button>
                    <button
                      onClick={() => setConfirming(false)}
                      className="rounded-lg border border-[var(--color-hairline)] px-3 py-1.5 text-xs text-slate-300"
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => setConfirming(true)}
                    disabled={busy || bids === 0}
                    className="rounded-lg border border-[var(--color-hairline)] px-3 py-1.5 text-xs text-slate-200 transition-colors hover:bg-white/5 disabled:opacity-40"
                    title="Records this plan in ipo_applications. Does not place real bids."
                  >
                    Commit plan
                  </button>
                )}
              </div>
            }
          >
            {comparison === null ? (
              <div className="rounded-lg border border-dashed border-[var(--color-hairline)] px-4 py-8 text-center">
                <p className="text-sm text-slate-300">
                  {user.pans.length === 0
                    ? "Add a PAN account and its balance in the rail to get a plan."
                    : "No active PAN has any capital yet — add funds in the rail."}
                </p>
                <p className="mx-auto mt-1.5 max-w-md text-xs leading-relaxed text-slate-500">
                  The engine refuses to return an empty schedule here, because
                  &ldquo;nothing is affordable&rdquo; and &ldquo;you have no funded
                  accounts&rdquo; are different answers and only one of them is a
                  planning result.
                </p>
              </div>
            ) : (
              <div className={dimmed}>
                <GanttSchedule events={plan.events} liquidCapital={user.liquid_capital} />
              </div>
            )}
          </Section>

          <Section
            title="Cashflow matrix"
            hint={
              user.capital_mode === "pooled"
                ? "One fund, day by day: what each bid freezes, what ASBA releases the morning after allotment, and what is left to spend. Only days where something happens get a row."
                : "Day by day across all accounts: what each bid freezes, what ASBA releases the morning after allotment, and what is left to spend."
            }
            aside={
              <span className="rounded-md border border-[var(--color-hairline)] px-2 py-1 text-[0.6875rem] text-slate-400">
                {user.capital_mode === "pooled" ? "one pooled fund" : "per-PAN (ASBA-accurate)"}
              </span>
            }
          >
            <div className={dimmed}>
              <CashflowMatrix
                timeline={plan.daily_timeline}
                initialCapital={plan.initial_capital || user.liquid_capital}
              />
            </div>
          </Section>

          <Section
            title="Per-PAN capital"
            hint={
              user.capital_mode === "pooled"
                ? "Where the pooled fund actually sits. ASBA freezes money in the applicant's own account, so a pooled plan assumes you can move cash into whichever PAN bids."
                : "ASBA freezes money in the applicant's own account, so headroom is per holder."
            }
          >
            <PanLedger user={user} events={plan.events} />
          </Section>

          <Section
            title="Priority matrix"
            hint="Every issue on the board, ranked by GMP then allotment date, with what was decided about each."
          >
            {ipos.length === 0 ? (
              <p className="rounded-lg border border-dashed border-[var(--color-hairline)] px-4 py-6 text-center text-sm text-slate-400">
                Nothing to rank yet.
              </p>
            ) : (
              <div className={dimmed}>
                <PriorityMatrix ipos={ipos} plan={plan} />
              </div>
            )}
          </Section>

          {applications.length > 0 ? (
            <Section
              title="Committed applications"
              hint="Bids you have recorded. Tick one once the registrar publishes the result — allotted means the money stayed out, not allotted means it came back at T+1."
            >
              <AllotmentLog applications={applications} mutate={mutate} />
            </Section>
          ) : null}

          <Section
            title="IPO calendar"
            hint="Import the live NSE list or type your own. Edits save as you make them, and the plan re-computes."
          >
            <IpoCalendarEditor ipos={ipos} mutate={mutate} debounce={debounce} />
          </Section>

          <footer className="pt-1 text-xs leading-relaxed text-slate-500">
            Grey market premium is an unofficial, unregulated indicator with no official
            feed: figures marked <span className="text-slate-400">live</span> are scraped
            from a public aggregator&apos;s dealer quotes and the rest are yours. Expected
            gain is arithmetic on that premium, not a forecast. Allotment and listing dates
            marked <span className="text-slate-400">est.</span> are computed from the close
            date under SEBI&apos;s T+3 timeline, not published by the registrar, and imported
            lot sizes are estimates from SEBI&apos;s minimum application value until you
            confirm the issuer&apos;s number. Nothing here is financial advice.
          </footer>
        </div>
      </div>

      <ChatDrawer enabled={chatEnabled} />
    </main>
  );
}
