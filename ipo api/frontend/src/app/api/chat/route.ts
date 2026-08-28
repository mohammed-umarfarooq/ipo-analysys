import { anthropic } from "@ai-sdk/anthropic";
import {
  convertToModelMessages,
  createUIMessageStreamResponse,
  isStepCount,
  streamText,
  toUIMessageStream,
  type UIMessage,
} from "ai";
import { z } from "zod";

import { ApiError, api } from "@/lib/api";
import type { AllotmentAssumption, SchedulingPolicy } from "@/lib/types";

export const maxDuration = 30;

/**
 * The copilot.
 *
 * Every tool here is **read-only**. `POST /api/schedule/commit` writes bids to the
 * database and is deliberately absent: model input is attacker-controlled in the
 * presence of prompt injection, so a state change stays behind an explicit click
 * in the UI. See SECURITY.md.
 *
 * The tools carry no user identity either. FastAPI resolves the user from its own
 * state, so there is no `user_id` for a model to be talked into changing.
 */

const SYSTEM = `You are the copilot for an IPO cashflow scheduler used by a retail
investor in India who applies across several family PANs.

Ground rules for what you say:

- The tools are your only source of truth. Never state a balance, GMP, lot cost,
  date or profit figure that did not come from a tool call in this conversation.
  If you need a number, call a tool. If a tool fails, say so plainly.
- Grey Market Premium is an unofficial, unregulated indicator with no exchange
  behind it. Treat it as a hypothesis about listing gain, never as a forecast.
- You are not a financial adviser and must not recommend that the user invest.
  Explain what the schedule does and what it costs; leave the decision to them.
- Amounts are Indian rupees. Write them as ₹1,23,456.

Domain facts you can rely on without a tool call:

- ASBA blocks money in the *applicant's own* bank account. Balances are per PAN
  and cannot be pooled: a bid under a relative's PAN is funded by that relative's
  account, never by the user's.
- One lot per PAN per issue. Extra lots under a single PAN cannot raise the
  chance of allotment, because retail oversubscription is settled by a lottery
  over unique applications.
- Blocked funds are released the morning after the allotment date (T+1), so one
  bid ties up capital over [bid date, allotment date + 1).
- An issue whose allotment date the registrar has not fixed yet cannot be
  scheduled at all: without an allotment date the length of the freeze is unknown.

Two policies exist. "value_first" claims scarce capital for the highest-GMP
issues first; "jit_greedy" bids on whatever closes next while it is affordable.
They differ only when capital runs out. When asked which is better, call
compare_policies and quote the actual difference rather than asserting one wins.

Be concise. Prefer a short table or a few lines to a long explanation.`;

/** Turn a backend failure into something the model can relay honestly. */
function explain(error: unknown): string {
  if (error instanceof ApiError) {
    return error.status === null
      ? `The scheduler API is not reachable: ${error.message}`
      : `The scheduler API returned ${error.status}: ${error.message}`;
  }
  return `Unexpected failure: ${error instanceof Error ? error.message : String(error)}`;
}

const policySchema = z
  .enum(["value_first", "jit_greedy"])
  .describe("value_first ranks by GMP; jit_greedy bids in close-date order.");

const assumptionSchema = z
  .enum(["none_allotted", "expected"])
  .describe(
    "expected treats allotted capital as permanently spent; none_allotted assumes every bid fails and all cash returns.",
  );

export async function POST(req: Request) {
  if (!process.env.ANTHROPIC_API_KEY) {
    // Fail with a readable reason rather than a provider stack trace. The UI
    // normally hides the composer entirely in this case; this is the backstop.
    return Response.json(
      {
        error:
          "ANTHROPIC_API_KEY is not set, so the copilot is disabled. The dashboard works without it.",
      },
      { status: 503 },
    );
  }

  const { messages }: { messages: UIMessage[] } = await req.json();

  const result = streamText({
    model: anthropic(process.env.COPILOT_MODEL ?? "claude-sonnet-5"),
    system: SYSTEM,
    messages: await convertToModelMessages(messages),
    // Enough steps to read the portfolio, plan, and compare before answering.
    stopWhen: isStepCount(6),
    tools: {
      read_portfolio: {
        description:
          "The user's PANs and the cash ASBA can freeze in each. Balances are per account, never pooled.",
        inputSchema: z.object({}),
        execute: async () => {
          try {
            const state = await api.userState();
            return {
              liquid_capital: state.liquid_capital,
              active_pans: state.active_pan_count,
              committed_applications: state.committed_application_count,
              pans: state.pans
                .filter((p) => p.is_active)
                .map((p) => ({
                  holder: p.holder_name,
                  relation: p.relation,
                  // Masked only. The PAN number never leaves the backend.
                  pan: p.pan_masked,
                  available_balance: p.available_balance,
                })),
            };
          } catch (error) {
            return { error: explain(error) };
          }
        },
      },

      list_ipos: {
        description:
          "Every IPO on the board with its GMP, lot cost, dates and priority rank. Includes issues that cannot be scheduled yet.",
        inputSchema: z.object({}),
        execute: async () => {
          try {
            const ipos = await api.ipos();
            return ipos.map((i) => ({
              name: i.name,
              type: i.issue_type,
              rank: i.priority_rank,
              gmp_percent: i.gmp_percent,
              lot_cost: i.lot_cost,
              expected_gain_per_lot: i.expected_profit_per_lot,
              close_date: i.close_date,
              allotment_date: i.allotment_date,
              unblock_date: i.unblock_date,
              schedulable: i.schedulable,
              note: i.note,
            }));
          } catch (error) {
            return { error: explain(error) };
          }
        },
      },

      plan_schedule: {
        description:
          "Run the allocation engine and return the resulting bid schedule, including every issue it declined and why.",
        inputSchema: z.object({
          policy: policySchema.optional(),
          assumption: assumptionSchema.optional(),
          min_gmp: z
            .number()
            .min(0)
            .max(100)
            .optional()
            .describe("Minimum GMP percent an issue must clear to be considered."),
        }),
        execute: async (input: {
          policy?: SchedulingPolicy;
          assumption?: AllotmentAssumption;
          min_gmp?: number;
        }) => {
          try {
            const plan = await api.schedule(input);
            return {
              policy: plan.policy,
              assumption: plan.allotment_assumption,
              total_expected_profit: plan.total_expected_profit,
              peak_capital_frozen: plan.peak_capital_deployed,
              bids: plan.events.map((e) => ({
                ipo: e.ipo_name,
                gmp_percent: e.gmp_percent,
                lots: e.lots_applied,
                blocked: e.blocked_amount,
                bid_date: e.action_date,
                unblock_date: e.unblock_date,
                expected_profit: e.expected_profit,
              })),
              skipped: plan.skipped.map((s) => ({
                ipo: s.ipo_name,
                gmp_percent: s.gmp_percent,
                reason: s.reason,
              })),
            };
          } catch (error) {
            return { error: explain(error) };
          }
        },
      },

      compare_policies: {
        description:
          "Plan the same calendar both ways and return what ranking by GMP is worth against bidding in close-date order.",
        inputSchema: z.object({
          assumption: assumptionSchema.optional(),
          min_gmp: z.number().min(0).max(100).optional(),
        }),
        execute: async (input: {
          assumption?: AllotmentAssumption;
          min_gmp?: number;
        }) => {
          try {
            const c = await api.compare(input);
            return {
              capital_constrained: c.capital_constrained,
              delta_expected_profit: c.delta_expected_profit,
              value_first: {
                expected_profit: c.value_first.total_expected_profit,
                issues: c.value_first.events.map((e) => e.ipo_name),
              },
              jit_greedy: {
                expected_profit: c.jit_greedy.total_expected_profit,
                issues: c.jit_greedy.events.map((e) => e.ipo_name),
              },
              note: c.capital_constrained
                ? "Capital is scarce, so the orderings genuinely diverge."
                : "Capital is sufficient for every eligible issue, so both policies bid on all of them and the delta is necessarily zero.",
            };
          } catch (error) {
            return { error: explain(error) };
          }
        },
      },
    },
  });

  return createUIMessageStreamResponse({
    stream: toUIMessageStream({
      stream: result.stream,
      // Tool errors are swallowed by default; the user should see them.
      onError: explain,
    }),
  });
}
