"use client";

import { useCallback, useState } from "react";

import { inr, shortDate, todayIso } from "@/lib/format";
import type { Pan, PanLedgerData } from "@/lib/types";
import type { Debounce, Mutate } from "@/lib/usePortfolio";

/**
 * One PAN account's cash position, and the dated movements that explain it.
 *
 * The balance shown is **cash in the account** — not cash net of pending ASBA
 * blocks. That distinction is easy to get wrong and expensive: the engine plans
 * *against* this number, and the "Per-PAN capital" panel is where you see how much
 * of it a plan freezes. Saying so on screen is cheaper than a support conversation.
 *
 * "Set balance" is not a third kind of movement. It records the deposit or
 * withdrawal that closes the gap, so the ledger still says which way money went and
 * the total never becomes a number with no history behind it (D18).
 */

const AMOUNT = /^\d{1,12}(\.\d{1,2})?$/;

type Verb = "DEPOSIT" | "WITHDRAWAL" | "SET";

const VERBS: Record<Verb, { label: string; cta: string; hint: string }> = {
  DEPOSIT: {
    label: "+ Add funds",
    cta: "Add",
    hint: "Money arriving — salary, a transfer, a refund from an unallotted bid.",
  },
  WITHDRAWAL: {
    label: "− Withdraw",
    cta: "Withdraw",
    hint: "Money leaving this account and no longer available to freeze.",
  },
  SET: {
    label: "Set balance",
    cta: "Set",
    hint: "Type what the bank says. The difference is recorded as a movement.",
  },
};

const SIGN_TONE: Record<string, string> = {
  DEPOSIT: "text-emerald-300",
  OPENING: "text-slate-300",
  WITHDRAWAL: "text-rose-300",
};

export function FundPanel({
  pan,
  mutate,
  debounce,
}: {
  pan: Pan;
  mutate: Mutate;
  debounce: Debounce;
}) {
  const [ledger, setLedger] = useState<PanLedgerData | null>(null);
  const [open, setOpen] = useState(false);
  const [verb, setVerb] = useState<Verb | null>(null);
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [when, setWhen] = useState(todayIso());
  const [confirmDelete, setConfirmDelete] = useState(false);

  // The ledger response carries the new balance, so once the panel has been opened
  // the figure here stays current without waiting for the server re-render.
  const balance = ledger?.available_balance ?? pan.available_balance;

  const loadLedger = useCallback(async () => {
    const data = await mutate<PanLedgerData>(
      "/api/portfolio",
      { action: "ledger", id: pan.id },
      { replan: false },
    );
    if (data) setLedger(data);
  }, [mutate, pan.id]);

  async function expand() {
    const next = !open;
    setOpen(next);
    if (next && !ledger) await loadLedger();
  }

  function reset() {
    setVerb(null);
    setAmount("");
    setNote("");
    setWhen(todayIso());
  }

  async function submit() {
    if (!AMOUNT.test(amount)) return;
    const data =
      verb === "SET"
        ? await mutate<Pan>(
            "/api/portfolio",
            { action: "patch-pan", id: pan.id, balance: amount },
            { refresh: true },
          )
        : await mutate<PanLedgerData>(
            "/api/portfolio",
            {
              action: "add-movement",
              id: pan.id,
              kind: verb,
              amount,
              note: note.trim() || undefined,
              occurred_on: when,
            },
            { refresh: true },
          );
    if (!data) return; // The hook is already showing why.
    reset();
    // A `patch-pan` reply is the PAN, not the ledger, so that path refetches.
    if (verb === "SET") await loadLedger();
    else setLedger(data as PanLedgerData);
    setOpen(true);
  }

  async function removeMovement(id: string) {
    const data = await mutate<PanLedgerData>(
      "/api/portfolio",
      { action: "delete-movement", id },
      { refresh: true },
    );
    if (data) setLedger(data);
  }

  return (
    <div className="rounded-lg border border-[var(--color-hairline)] bg-[var(--color-surface-raised)] p-3">
      <div className="flex items-baseline justify-between gap-2">
        <div className="min-w-0">
          <input
            defaultValue={pan.holder_name}
            aria-label="Holder name"
            onChange={(event) => {
              const holder_name = event.target.value.trim();
              if (!holder_name) return;
              debounce(`pan-name:${pan.id}`, () =>
                mutate(
                  "/api/portfolio",
                  { action: "patch-pan", id: pan.id, holder_name },
                  { refresh: true },
                ),
              );
            }}
            className="w-full truncate rounded bg-transparent text-sm text-slate-100 outline-none focus:bg-white/5 focus:px-1"
          />
          <div className="tnum mt-0.5 flex items-center gap-1.5 text-[0.6875rem] text-slate-500">
            <span>{pan.relation}</span>
            <span>{pan.pan_masked}</span>
            {pan.is_active ? null : (
              <span className="rounded bg-white/5 px-1 text-slate-400">inactive</span>
            )}
          </div>
        </div>
        <button
          onClick={expand}
          aria-expanded={open}
          className="tnum shrink-0 text-sm font-medium text-slate-100 tabular-nums hover:text-white"
          title="Cash in this account. Not net of pending ASBA blocks."
        >
          {inr(balance)}
          <span className="ml-1 text-[0.625rem] text-slate-500">{open ? "▲" : "▼"}</span>
        </button>
      </div>

      <div className="mt-2 flex flex-wrap gap-1">
        {(Object.keys(VERBS) as Verb[]).map((id) => (
          <button
            key={id}
            title={VERBS[id].hint}
            onClick={() => {
              setVerb(verb === id ? null : id);
              setAmount("");
            }}
            className={`rounded border px-1.5 py-0.5 text-[0.6875rem] transition-colors ${
              verb === id
                ? "border-slate-300 bg-slate-200 text-slate-900"
                : "border-[var(--color-hairline)] text-slate-300 hover:bg-white/5"
            }`}
          >
            {VERBS[id].label}
          </button>
        ))}
      </div>

      {verb ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
          className="mt-2 space-y-1.5"
        >
          <div className="flex gap-1.5">
            <input
              autoFocus
              value={amount}
              inputMode="decimal"
              placeholder={verb === "SET" ? "New balance" : "Amount"}
              aria-label={verb === "SET" ? "New balance" : "Amount"}
              onChange={(event) => setAmount(event.target.value)}
              className="tnum min-w-0 flex-1 rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-100 outline-none focus:border-slate-500"
            />
            <button
              type="submit"
              disabled={!AMOUNT.test(amount)}
              className="rounded bg-slate-200 px-2 py-1 text-xs font-medium text-slate-900 disabled:opacity-40"
            >
              {VERBS[verb].cta}
            </button>
            <button
              type="button"
              onClick={reset}
              className="rounded border border-[var(--color-hairline)] px-2 py-1 text-xs text-slate-400"
            >
              ✕
            </button>
          </div>
          {verb === "SET" ? null : (
            <div className="flex gap-1.5">
              <input
                value={note}
                maxLength={140}
                placeholder="Note (salary, rent…)"
                aria-label="Note"
                onChange={(event) => setNote(event.target.value)}
                className="min-w-0 flex-1 rounded border border-[var(--color-hairline)] bg-black/30 px-2 py-1 text-xs text-slate-300 outline-none focus:border-slate-500"
              />
              <input
                type="date"
                value={when}
                aria-label="Date"
                onChange={(event) => setWhen(event.target.value)}
                className="tnum rounded border border-[var(--color-hairline)] bg-black/30 px-1.5 py-1 text-xs text-slate-300 outline-none focus:border-slate-500"
              />
            </div>
          )}
          <p className="text-[0.625rem] leading-snug text-slate-500">{VERBS[verb].hint}</p>
        </form>
      ) : null}

      {open ? (
        <div className="mt-2.5 border-t border-[var(--color-hairline)] pt-2">
          {ledger === null ? (
            <p className="text-[0.6875rem] text-slate-500">Loading movements…</p>
          ) : ledger.movements.length === 0 ? (
            <p className="text-[0.6875rem] text-slate-500">
              No movements recorded yet.
            </p>
          ) : (
            <ul className="space-y-px">
              {ledger.movements.map((movement) => (
                <li
                  key={movement.id}
                  className="group flex items-baseline gap-2 py-0.5 text-[0.6875rem]"
                >
                  <span className="tnum w-14 shrink-0 text-slate-500">
                    {shortDate(movement.occurred_on)}
                  </span>
                  <span
                    className={`tnum w-20 shrink-0 text-right ${SIGN_TONE[movement.kind] ?? "text-slate-300"}`}
                  >
                    {movement.signed_amount > 0 ? "+" : "−"}
                    {inr(Math.abs(movement.signed_amount)).replace("₹", "")}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-slate-500">
                    {movement.note ?? movement.kind.toLowerCase()}
                  </span>
                  <button
                    onClick={() => void removeMovement(movement.id)}
                    title="Remove this entry and unwind the balance"
                    className="shrink-0 text-slate-600 opacity-0 transition-opacity hover:text-rose-300 group-hover:opacity-100"
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="mt-2 flex items-center gap-2 border-t border-[var(--color-hairline)] pt-2 text-[0.6875rem]">
            <button
              onClick={() =>
                void mutate(
                  "/api/portfolio",
                  { action: "patch-pan", id: pan.id, is_active: !pan.is_active },
                  { refresh: true },
                )
              }
              className="text-slate-400 hover:text-slate-200"
              title="An inactive PAN keeps its history but is excluded from planning."
            >
              {pan.is_active ? "Deactivate" : "Reactivate"}
            </button>
            {confirmDelete ? (
              <>
                <button
                  onClick={() =>
                    void mutate(
                      "/api/portfolio",
                      { action: "delete-pan", id: pan.id },
                      { refresh: true },
                    )
                  }
                  className="text-rose-300 hover:text-rose-200"
                >
                  Delete permanently
                </button>
                <button
                  onClick={() => setConfirmDelete(false)}
                  className="text-slate-500 hover:text-slate-300"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                onClick={() => setConfirmDelete(true)}
                className="text-slate-500 hover:text-rose-300"
              >
                Delete
              </button>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
