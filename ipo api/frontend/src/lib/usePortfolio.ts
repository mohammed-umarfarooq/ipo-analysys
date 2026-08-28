"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import type { AllotmentAssumption, Comparison, PlanRequest } from "@/lib/types";

/**
 * Client-side state for an editable portfolio: write, re-plan, report.
 *
 * The whole dashboard is downstream of two things — how much money is in each PAN
 * account, and what is on the calendar. Editing either has to re-run the engine,
 * because every figure on screen is derived from them. So a mutation here is always
 * "save, then re-plan", never just "save".
 *
 * Two deliberate choices:
 *
 * * **The plan is replaced in place, not re-fetched by navigation.** `router.refresh()`
 *   re-renders the server component and is needed when the *set* of PANs or issues
 *   changes; for a balance edit it would be a heavier round trip for the same result.
 * * **A 409 is data, not an error.** "No active PAN has any capital" is the correct
 *   answer for an empty portfolio and the first thing a new user sees. It clears the
 *   plan and leaves the inputs alone rather than raising an error banner.
 */

export type SaveState = "idle" | "saving" | "saved" | "error";

export interface MutateOptions {
  /**
   * Re-render the server component afterwards.
   *
   * Needed whenever a write changes something the *server* rendered rather than
   * something the plan derives — the PAN list, the issue list, and balances, since
   * `user.liquid_capital` is on screen in the KPI ribbon. The debounce means a burst
   * of keystrokes still costs one refresh.
   */
  refresh?: boolean;
  /** Skip the re-plan — for reads such as fetching a ledger. */
  replan?: boolean;
}

/** The single write primitive every editable component is handed. */
export type Mutate = <T>(
  path: string,
  body: unknown,
  options?: MutateOptions,
) => Promise<T | null>;

/** Defer until typing stops. See {@link useDebouncedSave}. */
export type Debounce = (key: string, run: () => void) => void;

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const parsed = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(
      (parsed && typeof parsed === "object" && "error" in parsed
        ? String((parsed as { error: unknown }).error)
        : null) ?? `Request failed (HTTP ${response.status}).`,
    );
  }
  return parsed as T;
}

export function usePortfolio(initialComparison: Comparison | null) {
  const router = useRouter();
  const [comparison, setComparison] = useState(initialComparison);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const planOptions = useRef<PlanRequest>({ assumption: "expected" });

  // "Saved" is a transient acknowledgement, not a state to sit in.
  useEffect(() => {
    if (saveState !== "saved") return;
    const timer = setTimeout(() => setSaveState("idle"), 1800);
    return () => clearTimeout(timer);
  }, [saveState]);

  const replan = useCallback(async (options?: PlanRequest) => {
    if (options) planOptions.current = { ...planOptions.current, ...options };
    setBusy(true);
    try {
      const response = await fetch("/api/plan", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(planOptions.current),
      });
      const body = await response.json().catch(() => null);
      if (response.status === 409) {
        // Nothing to plan with yet. Not a failure — the onboarding state.
        setComparison(null);
        return;
      }
      if (!response.ok) throw new Error(body?.error ?? "Planning failed.");
      setComparison(body as Comparison);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setSaveState("error");
    } finally {
      setBusy(false);
    }
  }, []);

  /** Write something, then bring the plan back in line with it. */
  const mutate = useCallback(
    async <T,>(path: string, body: unknown, options: MutateOptions = {}): Promise<T | null> => {
      setSaveState("saving");
      setError(null);
      try {
        const result = await post<T>(path, body);
        if (options.replan !== false) await replan();
        if (options.refresh) router.refresh();
        setSaveState("saved");
        return result;
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
        setSaveState("error");
        return null;
      }
    },
    [replan, router],
  );

  const setAssumption = useCallback(
    (assumption: AllotmentAssumption) => replan({ assumption }),
    [replan],
  );

  return {
    comparison,
    saveState,
    error,
    busy,
    setError,
    mutate,
    replan,
    setAssumption,
    /** The options the last plan was computed with, for the commit call. */
    planOptions,
  };
}

/**
 * Defer an action until the user stops typing, keyed so that editing two fields does
 * not cancel one save with the other.
 *
 * Without this, a four-digit balance is four writes and four planning runs, three of
 * which describe a number the user was in the middle of typing.
 */
export function useDebouncedSave(ms = 500) {
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach(clearTimeout);
  }, []);

  return useCallback(
    (key: string, run: () => void) => {
      const existing = timers.current.get(key);
      if (existing) clearTimeout(existing);
      timers.current.set(
        key,
        setTimeout(() => {
          timers.current.delete(key);
          run();
        }, ms),
      );
    },
    [ms],
  );
}
