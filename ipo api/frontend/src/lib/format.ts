/** Display helpers. Nothing here decides anything — the engine already did. */

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

const INR_PAISE = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** `₹1,80,000` — Indian digit grouping, which is not what `en-US` produces. */
export function inr(value: number): string {
  return INR.format(value);
}

export function inrExact(value: number): string {
  return INR_PAISE.format(value);
}

export function pct(value: number, digits = 1): string {
  return `${value.toFixed(digits)}%`;
}

export function parseDate(iso: string): Date {
  // Dates from the API are plain `YYYY-MM-DD`. Appending a time keeps the
  // browser from shifting them a day backwards in negative-offset timezones.
  return new Date(`${iso}T00:00:00`);
}

export function shortDate(iso: string): string {
  return parseDate(iso).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
  });
}

export function longDate(iso: string): string {
  return parseDate(iso).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export function daysBetween(from: string, to: string): number {
  const ms = parseDate(to).getTime() - parseDate(from).getTime();
  return Math.round(ms / 86_400_000);
}

export function addDays(iso: string, days: number): string {
  const d = parseDate(iso);
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

export function todayIso(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

/** Colour ramp for GMP, used consistently by the matrix and the Gantt bars. */
export function gmpTone(gmp: number): string {
  if (gmp >= 50) return "text-emerald-300";
  if (gmp >= 25) return "text-lime-300";
  if (gmp >= 10) return "text-amber-300";
  return "text-slate-400";
}

export function gmpBar(gmp: number): string {
  if (gmp >= 50) return "bg-emerald-500/70 border-emerald-300/60";
  if (gmp >= 25) return "bg-lime-500/60 border-lime-300/50";
  if (gmp >= 10) return "bg-amber-500/60 border-amber-300/50";
  return "bg-slate-600/60 border-slate-400/40";
}
