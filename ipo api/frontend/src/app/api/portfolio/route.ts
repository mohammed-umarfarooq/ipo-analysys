import { z } from "zod";

import { ApiError, api } from "@/lib/api";

/**
 * The browser's only door to the funds and PAN endpoints.
 *
 * One handler with a discriminated union rather than eight route files. The union
 * is the point: it is exhaustive, so a new action cannot be added without a schema
 * for it, and TypeScript checks the dispatch below covers every case. Eight
 * near-identical files would validate the same things eight times and leave the
 * "did I remember to validate?" question open at each one.
 *
 * Input is re-validated here even though FastAPI validates it too. This handler is
 * the trust boundary the browser can reach; FastAPI is not exposed to it. It must
 * not become a way to forward arbitrary JSON to an unauthenticated API.
 */

/** Money as a decimal string — see the note in `lib/types.ts` on why not a number. */
const amount = z
  .string()
  .regex(/^\d{1,12}(\.\d{1,2})?$/, "expected an amount like 15000 or 15000.50");

const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);

const bodySchema = z.discriminatedUnion("action", [
  z.object({
    action: z.literal("patch-user"),
    name: z.string().min(1).max(100).optional(),
    demat_balance: amount.optional(),
    // Pooled or ring-fenced planning. An enum, so an unknown mode is a 400 here
    // rather than a silently per-PAN plan.
    capital_mode: z.enum(["pooled", "per_pan"]).optional(),
  }),
  z.object({
    action: z.literal("add-pan"),
    holder_name: z.string().min(1).max(100),
    relation: z.string().max(50).default("Self"),
    // Validated in the same shape the backend enforces, so a typo is caught before
    // it becomes an irreversible hash — the number itself is never stored (D11).
    pan_number: z.string().regex(/^[A-Za-z]{5}[0-9]{4}[A-Za-z]$/, "PAN must look like ABCDE1234F"),
    upi_id: z.string().min(3).max(100),
    linked_bank_name: z.string().max(100).optional(),
    opening_balance: amount.optional(),
  }),
  z.object({
    action: z.literal("patch-pan"),
    id: z.string().min(1),
    holder_name: z.string().min(1).max(100).optional(),
    relation: z.string().max(50).optional(),
    upi_id: z.string().min(3).max(100).optional(),
    linked_bank_name: z.string().max(100).optional(),
    is_active: z.boolean().optional(),
    balance: amount.optional(),
  }),
  z.object({ action: z.literal("delete-pan"), id: z.string().min(1) }),
  z.object({ action: z.literal("ledger"), id: z.string().min(1) }),
  z.object({
    action: z.literal("add-movement"),
    id: z.string().min(1),
    kind: z.enum(["DEPOSIT", "WITHDRAWAL"]),
    amount,
    note: z.string().max(140).optional(),
    occurred_on: isoDate.optional(),
  }),
  z.object({ action: z.literal("delete-movement"), id: z.string().min(1) }),
  z.object({
    action: z.literal("patch-application"),
    id: z.string().min(1),
    // Nullable rather than optional: null means "the registrar has not said yet",
    // which is a value the user can choose, not a missing field.
    allotted: z.boolean().nullable(),
  }),
  z.object({ action: z.literal("sample-data") }),
]);

export async function POST(req: Request) {
  const parsed = bodySchema.safeParse(await req.json().catch(() => null));
  if (!parsed.success) {
    return Response.json(
      { error: parsed.error.issues[0]?.message ?? "Invalid request.", issues: parsed.error.issues },
      { status: 400 },
    );
  }

  try {
    const body = parsed.data;
    switch (body.action) {
      case "patch-user":
        return Response.json(await api.patchUser(body));
      case "add-pan":
        return Response.json(await api.addPan(body));
      case "patch-pan": {
        const { action: _a, id, ...fields } = body;
        return Response.json(await api.patchPan(id, fields));
      }
      case "delete-pan":
        return Response.json(await api.deletePan(body.id));
      case "ledger":
        return Response.json(await api.ledger(body.id));
      case "add-movement": {
        const { action: _a, id, ...fields } = body;
        return Response.json(await api.addMovement(id, fields));
      }
      case "delete-movement":
        return Response.json(await api.deleteMovement(body.id));
      case "patch-application":
        return Response.json(await api.patchApplication(body.id, { allotted: body.allotted }));
      case "sample-data":
        return Response.json(await api.sampleData());
    }
  } catch (error) {
    if (error instanceof ApiError) {
      // The backend's messages are written to be read by a person — an overdrawn
      // withdrawal or a PAN with committed bids explains itself — so they are
      // passed through rather than replaced with "request failed".
      return Response.json({ error: error.message }, { status: error.status ?? 502 });
    }
    throw error;
  }
}
