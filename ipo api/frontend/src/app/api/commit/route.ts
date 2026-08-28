import { revalidatePath } from "next/cache";
import { z } from "zod";

import { ApiError, api } from "@/lib/api";

/**
 * Writes the plan to `ipo_applications`.
 *
 * This is the one state-changing path in the app, and it is reachable only from an
 * explicit two-step confirmation in the UI — never from the copilot's tools. The
 * database enforces one application per (ipo, PAN) and exactly one lot each, so a
 * repeat commit is a no-op rather than a duplicate bid.
 */
const bodySchema = z.object({
  policy: z.enum(["value_first", "jit_greedy"]).optional(),
  assumption: z.enum(["none_allotted", "expected"]).optional(),
  min_gmp: z.number().min(0).max(100).optional(),
});

export async function POST(req: Request) {
  const parsed = bodySchema.safeParse(await req.json().catch(() => ({})));
  if (!parsed.success) {
    return Response.json(
      { error: "Invalid commit options.", issues: parsed.error.issues },
      { status: 400 },
    );
  }

  try {
    const result = await api.commit(parsed.data);
    // The committed count is rendered by the server component.
    revalidatePath("/");
    return Response.json(result);
  } catch (error) {
    if (error instanceof ApiError) {
      return Response.json({ error: error.message }, { status: error.status ?? 502 });
    }
    throw error;
  }
}
