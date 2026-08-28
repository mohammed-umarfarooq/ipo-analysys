"use client";

import { useState } from "react";

import { FundPanel } from "@/components/FundPanel";
import { inr } from "@/lib/format";
import type {
  AllotmentAssumption,
  Ipo,
  SchedulingPolicy,
  UserState,
} from "@/lib/types";
import type { Debounce, Mutate, SaveState } from "@/lib/usePortfolio";

/**
 * The docked inputs column: everything the plan is computed *from*.
 *
 * The split is deliberate. This rail holds what you decide — how much cash is in each
 * account, which policy, what GMP floor. The pane beside it holds only what the
 * engine derived from those. Nothing in the rail is a result and nothing in the pane
 * is editable, so there is never a question about which numbers you can change.
 *
 * Two kinds of input live here and they behave differently:
 *
 * * **Persisted** — balances, PAN details, the demat figure. These write to the
 *   database and survive a reload.
 * * **Planning parameters** — policy, assumption, GMP floor, start date. These are
 *   arguments to a single planning run, held in component state. Saving them would
 *   be claiming a "what if I only bid above 20%" question is a fact about you.
 */

export interface PlanInputs {
  policy: SchedulingPolicy;
  assumption: AllotmentAssumption;
  /** Blank means no floor. Kept as a string so a half-typed "1." is not a zero. */
  minGmp: string;
  startDate: string;
}

const PAN_RE = /^[A-Za-z]{5}[0-9]{4}[A-Za-z]$/;
const AMOUNT = /^\d{1,12}(\.\d{1,2})?$/;

const BLANK_PAN = {
  holder_name: "",
  relation: "Self",
  pan_number: "",
  upi_id: "",
  linked_bank_name: "",
  opening_balance: "",
};

function Group({ title, aside, children }: {
  title: string;
  aside?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="text-[0.6875rem] font-medium uppercase tracking-wider text-slate-400">
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function Toggle<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: { id: T; label: string; hint: string }[];
  value: T;
  onChange: (next: T) => void;
  ariaLabel: string;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className="flex rounded-lg border border-[var(--color-hairline)] bg-[var(--color-surface-raised)] p-0.5"
    >
      {options.map((option) => (
        <button
          key={option.id}
          role="radio"
          aria-checked={option.id === value}
          title={option.hint}
          onClick={() => onChange(option.id)}
          className={`flex-1 rounded-md px-2 py-1 text-[0.6875rem] transition-colors ${
            option.id === value
              ? "bg-slate-200 font-medium text-slate-900"
              : "text-slate-300 hover:bg-white/5"
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function InputsRail({
  user,
  ipos,
  plan,
  onPlanChange,
  mutate,
  debounce,
  saveState,
}: {
  user: UserState;
  ipos: Ipo[];
  plan: PlanInputs;
  onPlanChange: (patch: Partial<PlanInputs>) => void;
  mutate: Mutate;
  debounce: Debounce;
  saveState: SaveState;
}) {
  const [adding, setAdding] = useState(false);
  const [pan, setPan] = useState({ ...BLANK_PAN });

  const panValid =
    pan.holder_name.trim().length > 0 &&
    PAN_RE.test(pan.pan_number) &&
    pan.upi_id.trim().length >= 3 &&
    (pan.opening_balance === "" || AMOUNT.test(pan.opening_balance));

  async function submitPan() {
    if (!panValid) return;
    const created = await mutate(
      "/api/portfolio",
      {
        action: "add-pan",
        holder_name: pan.holder_name.trim(),
        relation: pan.relation.trim() || "Self",
        pan_number: pan.pan_number.toUpperCase(),
        upi_id: pan.upi_id.trim(),
        linked_bank_name: pan.linked_bank_name.trim() || undefined,
        opening_balance: pan.opening_balance || undefined,
      },
      { refresh: true },
    );
    if (created) {
      setPan({ ...BLANK_PAN });
      setAdding(false);
    }
  }

  const incomplete = ipos.filter((ipo) => ipo.missing.length > 0).length;

  return (
    <aside className="space-y-5 lg:sticky lg:top-6 lg:max-h-[calc(100vh-3rem)] lg:overflow-y-auto lg:pr-1">
      <Group
        title="Funds"
        aside={
          <span className="tnum text-sm font-semibold text-slate-100">
            {inr(user.liquid_capital)}
          </span>
        }
      >
        <div className="space-y-2">
          {/*
           * Pooled is the default because it is how a household actually thinks about
           * its money: one war-chest funding bids under whichever PAN is free. Per-PAN
           * is what ASBA literally does — it freezes cash in the applicant's own
           * account — so it stays one click away for an execution-accurate plan.
           */}
          <Toggle
            ariaLabel="Capital model"
            value={user.capital_mode}
            onChange={(next) =>
              void mutate("/api/portfolio", { action: "patch-user", capital_mode: next }, {
                refresh: true,
              })
            }
            options={[
              {
                id: "pooled" as const,
                label: "One pooled fund",
                hint: "Plan the total as a single fund any PAN may draw on. Assumes you can move cash into whichever account bids.",
              },
              {
                id: "per_pan" as const,
                label: "Per PAN",
                hint: "Ring-fence each holder's own balance, which is what ASBA actually freezes.",
              },
            ]}
          />
          <p className="text-[0.6875rem] leading-relaxed text-slate-500">
            {user.capital_mode === "pooled"
              ? "One fund of " +
                inr(user.liquid_capital) +
                " across " +
                user.pans.length +
                " PAN" +
                (user.pans.length === 1 ? "" : "s") +
                ", recycled as ASBA releases it at T+1. Still one lot per PAN per issue."
              : "Each holder bids only against their own balance — execution-accurate, and the stricter of the two."}
          </p>

          {user.pans.length === 0 ? (
            <p className="rounded-lg border border-dashed border-[var(--color-hairline)] px-3 py-4 text-center text-xs leading-relaxed text-slate-400">
              Add a PAN account to begin. Balances still live per holder because that is
              where ASBA freezes the money; pooled planning just lets one holder&apos;s
              idle cash fund another&apos;s bid.
            </p>
          ) : (
            user.pans.map((row) => (
              <FundPanel key={row.id} pan={row} mutate={mutate} debounce={debounce} />
            ))
          )}

          {adding ? (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void submitPan();
              }}
              className="space-y-1.5 rounded-lg border border-[var(--color-hairline)] bg-black/20 p-2.5"
            >
              <input
                autoFocus
                value={pan.holder_name}
                placeholder="Holder name"
                aria-label="Holder name"
                onChange={(event) => setPan({ ...pan, holder_name: event.target.value })}
                className="w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
              <div className="flex gap-1.5">
                <input
                  value={pan.relation}
                  placeholder="Relation"
                  aria-label="Relation"
                  onChange={(event) => setPan({ ...pan, relation: event.target.value })}
                  className="min-w-0 flex-1 rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
                />
                <input
                  value={pan.pan_number}
                  maxLength={10}
                  placeholder="ABCDE1234F"
                  aria-label="PAN number"
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(event) =>
                    setPan({ ...pan, pan_number: event.target.value.toUpperCase() })
                  }
                  className={`tnum min-w-0 flex-1 rounded border bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none ${
                    pan.pan_number === "" || PAN_RE.test(pan.pan_number)
                      ? "border-[var(--color-hairline)] focus:border-slate-500"
                      : "border-rose-400/60"
                  }`}
                />
              </div>
              <div className="flex gap-1.5">
                <input
                  value={pan.upi_id}
                  placeholder="UPI id"
                  aria-label="UPI id"
                  onChange={(event) => setPan({ ...pan, upi_id: event.target.value })}
                  className="min-w-0 flex-1 rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
                />
                <input
                  value={pan.opening_balance}
                  inputMode="decimal"
                  placeholder="Balance ₹"
                  aria-label="Opening balance"
                  onChange={(event) => setPan({ ...pan, opening_balance: event.target.value })}
                  className="tnum min-w-0 flex-1 rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
                />
              </div>
              <input
                value={pan.linked_bank_name}
                placeholder="Bank (optional)"
                aria-label="Linked bank"
                onChange={(event) => setPan({ ...pan, linked_bank_name: event.target.value })}
                className="w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
              {/* The one place a plaintext PAN exists client-side. It is hashed and
                  masked on arrival and never stored or echoed back (D11). */}
              <p className="text-[0.625rem] leading-snug text-slate-500">
                The PAN is hashed on arrival — the server keeps only{" "}
                <span className="tnum">ABCDE****F</span> and never returns the number.
              </p>
              <div className="flex gap-1.5">
                <button
                  type="submit"
                  disabled={!panValid}
                  className="flex-1 rounded bg-slate-200 px-2 py-1 text-xs font-medium text-slate-900 disabled:opacity-40"
                >
                  Add account
                </button>
                <button
                  type="button"
                  onClick={() => setAdding(false)}
                  className="rounded border border-[var(--color-hairline)] px-2 py-1 text-xs text-slate-400"
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <button
              onClick={() => setAdding(true)}
              className="w-full rounded-lg border border-dashed border-[var(--color-hairline)] py-1.5 text-xs text-slate-300 transition-colors hover:bg-white/5"
            >
              + Add PAN account
            </button>
          )}
        </div>
      </Group>

      <Group
        title="Planning"
        aside={
          saveState === "saving" ? (
            <span className="text-[0.6875rem] text-slate-500">saving…</span>
          ) : saveState === "saved" ? (
            <span className="text-[0.6875rem] text-emerald-400/80">saved</span>
          ) : null
        }
      >
        <div className="space-y-2.5">
          <div className="flex gap-2">
            <label className="flex-1 text-[0.6875rem] text-slate-400">
              Min GMP %
              <input
                value={plan.minGmp}
                inputMode="decimal"
                placeholder="none"
                onChange={(event) => onPlanChange({ minGmp: event.target.value })}
                className="tnum mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
            </label>
            <label className="flex-1 text-[0.6875rem] text-slate-400">
              Plan from
              <input
                type="date"
                value={plan.startDate}
                onChange={(event) => onPlanChange({ startDate: event.target.value })}
                className="tnum mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
              />
            </label>
          </div>

          <div>
            <div className="mb-1 text-[0.6875rem] text-slate-400">Allocation policy</div>
            <Toggle
              ariaLabel="Allocation policy"
              value={plan.policy}
              onChange={(policy) => onPlanChange({ policy })}
              options={[
                {
                  id: "value_first",
                  label: "Value first",
                  hint: "Claim capital for the highest-GMP issues, then fill gaps by date.",
                },
                {
                  id: "jit_greedy",
                  label: "By close date",
                  hint: "Bid on whatever closes next, if affordable.",
                },
              ]}
            />
          </div>

          <div>
            <div className="mb-1 text-[0.6875rem] text-slate-400">Allotment assumption</div>
            <Toggle
              ariaLabel="Allotment assumption"
              value={plan.assumption}
              onChange={(assumption) => onPlanChange({ assumption })}
              options={[
                {
                  id: "expected",
                  label: "Expect allotments",
                  hint: "Allotted money is debited, not returned. The honest default.",
                },
                {
                  id: "none_allotted",
                  label: "None allotted",
                  hint: "All blocked capital returns at T+1. Overstates future liquidity.",
                },
              ]}
            />
          </div>

          <label className="block text-[0.6875rem] text-slate-400">
            Demat balance ₹
            <input
              defaultValue={String(user.demat_balance)}
              inputMode="decimal"
              aria-label="Demat balance"
              title="Value of holdings already in the demat account. Not ASBA-freezable cash."
              onChange={(event) => {
                const value = event.target.value;
                if (!AMOUNT.test(value)) return;
                debounce("demat", () =>
                  mutate(
                    "/api/portfolio",
                    { action: "patch-user", demat_balance: value },
                    { refresh: true, replan: false },
                  ),
                );
              }}
              className="tnum mt-0.5 w-full rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
            />
          </label>
        </div>
      </Group>

      <Group
        title="Calendar"
        aside={
          <span className="text-[0.6875rem] text-slate-500">
            {ipos.length} issue{ipos.length === 1 ? "" : "s"}
          </span>
        }
      >
        <p className="text-[0.6875rem] leading-relaxed text-slate-500">
          {ipos.length === 0 ? (
            <>Empty. Import the live NSE list or add an issue in the editor below.</>
          ) : incomplete > 0 ? (
            <>
              <span className="text-amber-300">{incomplete}</span> need your input —
              lot size, allotment date or GMP. Fill them in the calendar editor and
              they enter the plan.
            </>
          ) : (
            <>Every issue has what the engine needs.</>
          )}
        </p>
        {user.pans.length === 0 && ipos.length === 0 ? (
          <button
            onClick={() =>
              void mutate("/api/portfolio", { action: "sample-data" }, { refresh: true })
            }
            className="mt-2 w-full rounded-lg border border-[var(--color-hairline)] py-1.5 text-xs text-slate-300 transition-colors hover:bg-white/5"
            title="Three PANs and a fabricated calendar, so the planner can be seen working."
          >
            Load sample data
          </button>
        ) : null}
      </Group>
    </aside>
  );
}
