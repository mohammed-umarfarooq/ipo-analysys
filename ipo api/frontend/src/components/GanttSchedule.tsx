"use client";

import { useMemo } from "react";

import {
  addDays,
  daysBetween,
  gmpBar,
  inr,
  pct,
  shortDate,
  todayIso,
} from "@/lib/format";
import type { ScheduleEvent } from "@/lib/types";

/** A minimum bar width so a same-day block is still visible and clickable. */
const MIN_WIDTH_PCT = 1.5;

interface Row {
  event: ScheduleEvent;
  left: number;
  width: number;
  days: number;
}

/**
 * The capital timeline.
 *
 * Each bar is one ASBA block, spanning `[action_date, unblock_date)` — the same
 * half-open interval the engine reasons over (Rule 2, T+1). The strip underneath
 * is the reason the schedule looks the way it does: frozen capital is a step
 * function that only rises when a block *starts*, so the peak is always found at
 * some bid date, and that is exactly where the engine checks affordability.
 */
export function GanttSchedule({
  events,
  liquidCapital,
}: {
  events: ScheduleEvent[];
  liquidCapital: number;
}) {
  const chart = useMemo(() => {
    if (events.length === 0) return null;

    const today = todayIso();
    const sorted = [...events].sort(
      (a, b) =>
        a.action_date.localeCompare(b.action_date) ||
        a.ipo_name.localeCompare(b.ipo_name),
    );

    const starts = sorted.map((e) => e.action_date);
    const ends = sorted.map((e) => e.unblock_date);
    const domainStart = [today, ...starts].sort()[0]!;
    const domainEnd = ends.sort().at(-1)!;
    // Pad the right edge so the last unblock does not sit flush against the frame.
    const paddedEnd = addDays(domainEnd, 1);
    const span = Math.max(daysBetween(domainStart, paddedEnd), 1);

    const offset = (iso: string) => (daysBetween(domainStart, iso) / span) * 100;

    const rows: Row[] = sorted.map((event) => {
      const left = offset(event.action_date);
      const raw = offset(event.unblock_date) - left;
      return {
        event,
        left,
        width: Math.max(raw, MIN_WIDTH_PCT),
        days: daysBetween(event.action_date, event.unblock_date),
      };
    });

    // Frozen total per day, from the published blocks only.
    const load = Array.from({ length: span }, (_, day) => {
      const on = addDays(domainStart, day);
      return sorted.reduce(
        (total, e) =>
          e.action_date <= on && on < e.unblock_date
            ? total + e.blocked_amount
            : total,
        0,
      );
    });
    const peak = Math.max(...load, 0);
    const peakDay = load.indexOf(peak);

    const tickStep = Math.max(Math.ceil(span / 7), 1);
    const ticks = Array.from(
      { length: Math.floor((span - 1) / tickStep) + 1 },
      (_, i) => i * tickStep,
    );

    return {
      rows,
      span,
      domainStart,
      load,
      peak,
      peakDay,
      ticks,
      tickStep,
      todayOffset: offset(today),
    };
  }, [events]);

  if (!chart) {
    return (
      <p className="rounded-lg border border-dashed border-[var(--color-hairline)] px-4 py-8 text-center text-sm text-slate-400">
        No bids are scheduled. Every issue was either filtered out or unaffordable —
        the reasons are listed below.
      </p>
    );
  }

  const { rows, span, domainStart, load, peak, peakDay, ticks, tickStep } =
    chart;

  return (
    <div>
      <div className="flex">
        <div className="w-52 shrink-0 pr-3" />
        <div className="relative flex-1">
          {/* Date axis */}
          <div className="relative h-5 border-b border-[var(--color-hairline)]">
            {ticks.map((day) => (
              <span
                key={day}
                className="tnum absolute -translate-x-1/2 text-[0.6875rem] text-slate-500"
                style={{ left: `${(day / span) * 100}%` }}
              >
                {shortDate(addDays(domainStart, day))}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div className="mt-1">
        {rows.map(({ event, left, width, days }) => (
          <div key={event.ipo_id} className="flex items-center py-[3px]">
            <div className="w-52 shrink-0 pr-3 text-right">
              <div className="truncate text-sm text-slate-200">
                {event.ipo_name}
              </div>
              <div className="tnum text-[0.6875rem] text-slate-500">
                {pct(event.gmp_percent)} GMP · {event.lots_applied} lot
                {event.lots_applied === 1 ? "" : "s"}
              </div>
            </div>

            <div className="relative h-9 flex-1 rounded bg-white/[0.03]">
              {/* Weekly gridlines, so a bar's length reads as a duration. */}
              {ticks.map((day) => (
                <span
                  key={day}
                  className="absolute inset-y-0 w-px bg-white/[0.06]"
                  style={{ left: `${(day / span) * 100}%` }}
                />
              ))}
              <span
                className="absolute inset-y-0 w-px bg-sky-400/50"
                style={{ left: `${chart.todayOffset}%` }}
              />
              <div
                title={`${event.ipo_name}: ${inr(event.blocked_amount)} frozen from ${shortDate(event.action_date)} to ${shortDate(event.unblock_date)} (${days} days), across ${event.pans_used.length} PAN${event.pans_used.length === 1 ? "" : "s"}`}
                className={`absolute top-1 bottom-1 flex items-center overflow-hidden rounded border px-2 ${gmpBar(event.gmp_percent)}`}
                style={{ left: `${left}%`, width: `${width}%` }}
              >
                <span className="tnum truncate text-[0.6875rem] font-medium text-slate-950">
                  {inr(event.blocked_amount)}
                </span>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Capital load */}
      <div className="mt-4 flex items-end">
        <div className="w-52 shrink-0 pr-3 text-right">
          <div className="text-[0.6875rem] uppercase tracking-wider text-slate-400">
            ASBA blocks
          </div>
          <div className="tnum text-sm text-slate-200">{inr(peak)}</div>
          <div className="tnum text-[0.6875rem] text-slate-500">
            peak, {shortDate(addDays(domainStart, peakDay))}
          </div>
        </div>
        <div className="relative flex h-16 flex-1 items-end gap-px">
          {load.map((amount, day) => (
            <div
              key={day}
              title={`${shortDate(addDays(domainStart, day))}: ${inr(amount)} frozen`}
              className={`flex-1 rounded-t ${
                day === peakDay ? "bg-amber-400/80" : "bg-slate-500/50"
              }`}
              style={{
                height: peak > 0 ? `${Math.max((amount / peak) * 100, 1)}%` : "1%",
              }}
            />
          ))}
          {liquidCapital > 0 && peak > 0 ? (
            <span
              title={`Total liquid capital: ${inr(liquidCapital)}`}
              className="absolute inset-x-0 border-t border-dashed border-rose-400/50"
              style={{
                bottom: `${Math.min((liquidCapital / peak) * 100, 100)}%`,
              }}
            />
          ) : null}
        </div>
      </div>

      <p className="mt-3 pl-52 text-[0.6875rem] leading-relaxed text-slate-500">
        Bars cover{" "}
        <span className="text-slate-400">[bid date, allotment date + 1)</span> —
        ASBA releases funds the morning after allotment. The dashed line is total
        liquid capital and gridlines are {tickStep} day
        {tickStep === 1 ? "" : "s"} apart. The strip counts only money frozen by
        these blocks, which is why its peak can sit below the{" "}
        <span className="text-slate-400">peak capital frozen</span> figure above:
        under the expected-allotment assumption, the share of each bid that gets
        allotted is debited and never returns, so it keeps consuming the account
        after the block has lifted.
      </p>
    </div>
  );
}
