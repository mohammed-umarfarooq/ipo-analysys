import { z } from "zod";

import { ApiError, api } from "@/lib/api";

/**
 * The browser's only door to the IPO calendar.
 *
 * Same shape and same reasoning as `api/portfolio`: one exhaustive discriminated
 * union, validated here because this is the boundary the browser can reach.
 *
 * Note what is *not* in the schema: `gmp_percent`. The user types a rupee premium
 * and the backend derives the percentage that Rule 3 ranks on. Accepting both would
 * let the ranking disagree with the figure on screen.
 */

const money = z
  .string()
  .regex(/^\d{1,12}(\.\d{1,2})?$/, "expected an amount like 788 or 788.50");

const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, "expected a date like 2026-08-25");

const issueFields = {
  name: z.string().min(1).max(150),
  symbol: z.string().max(50).nullish(),
  issue_type: z.enum(["Mainboard", "SME"]),
  min_price: money,
  max_price: money,
  lot_size: z.number().int().positive(),
  latest_gmp: money,
  open_date: isoDate,
  close_date: isoDate,
  allotment_date: isoDate.nullish(),
  listing_date: isoDate.nullish(),
  registrar_name: z.string().max(100).nullish(),
  // A probability, so 0–1 rather than 0–100. Mirrors the DB CHECK.
  allotment_probability: z.string().regex(/^(0(\.\d{1,3})?|1(\.0{1,3})?)$/),
};

const createSchema = z.object(issueFields);

/**
 * The patch variant is spelled out rather than derived with `.partial()` so the
 * inferred type stays a real object type. A mapped `Object.fromEntries` would widen
 * it to `Record<string, unknown>`, which would then be forwarded as `any` — losing
 * exactly the type safety this file exists to provide.
 */
const patchSchema = createSchema.partial().extend({ id: z.string().min(1) });

const bodySchema = z.discriminatedUnion("action", [
  createSchema.extend({ action: z.literal("add-ipo") }),
  patchSchema.extend({ action: z.literal("patch-ipo") }),
  z.object({ action: z.literal("delete-ipo"), id: z.string().min(1) }),
  z.object({ action: z.literal("import") }),
  z.object({ action: z.literal("refresh-gmp") }),
]);

export async function POST(req: Request) {
  const parsed = bodySchema.safeParse(await req.json().catch(() => null));
  if (!parsed.success) {
    return Response.json(
      { error: parsed.error.issues[0]?.message ?? "Invalid issue.", issues: parsed.error.issues },
      { status: 400 },
    );
  }

  try {
    const body = parsed.data;
    switch (body.action) {
      case "add-ipo": {
        const { action: _a, ...fields } = body;
        return Response.json(await api.addIpo(fields));
      }
      case "patch-ipo": {
        const { action: _a, id, ...fields } = body;
        return Response.json(await api.patchIpo(id, fields));
      }
      case "delete-ipo":
        return Response.json(await api.deleteIpo(body.id));
      case "import":
        return Response.json(await api.importIpos());
      case "refresh-gmp":
        return Response.json(await api.refreshGmp());
    }
  } catch (error) {
    if (error instanceof ApiError) {
      // A 502 here is NSE being unreachable, and the backend's message says what to
      // do about it ("add issues manually"). Worth showing verbatim.
      return Response.json({ error: error.message }, { status: error.status ?? 502 });
    }
    throw error;
  }
}
