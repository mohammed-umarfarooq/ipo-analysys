import { z } from "zod";

import { ApiError, api } from "@/lib/api";

/**
 * The browser's only door to the scheduler.
 *
 * It returns *both* policies in one response so the dashboard's policy toggle is
 * instant and the comparison is always on screen. The input is re-validated here
 * even though FastAPI validates it too — this handler is the trust boundary the
 * browser can reach, and it must not become a way to forward arbitrary JSON.
 */
const bodySchema = z.object({
  assumption: z.enum(["none_allotted", "expected"]).optional(),
  min_gmp: z.number().min(0).max(100).optional(),
  start_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
});

export async function POST(req: Request) {
  const parsed = bodySchema.safeParse(await req.json().catch(() => ({})));
  if (!parsed.success) {
    return Response.json(
      { error: "Invalid planning options.", issues: parsed.error.issues },
      { status: 400 },
    );
  }

  try {
    return Response.json(await api.compare(parsed.data));
  } catch (error) {
    if (error instanceof ApiError) {
      // 409 means "no active PAN has any capital" — a real answer, not a fault.
      return Response.json({ error: error.message }, { status: error.status ?? 502 });
    }
    throw error;
  }
}
