"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useEffect, useRef, useState } from "react";

const SUGGESTIONS = [
  "What is this plan doing with my money?",
  "Is ranking by GMP actually worth anything here?",
  "Which issues did it skip, and why?",
  "What happens if I assume nothing gets allotted?",
];

const TOOL_LABELS: Record<string, string> = {
  read_portfolio: "Reading balances",
  list_ipos: "Reading the IPO board",
  plan_schedule: "Running the scheduler",
  compare_policies: "Comparing both policies",
};

function ToolChip({
  name,
  state,
  errorText,
}: {
  name: string;
  state: string;
  errorText?: string;
}) {
  const label = TOOL_LABELS[name] ?? name;
  const failed = state === "output-error";
  const done = state === "output-available";
  return (
    <div
      className={`my-1 inline-flex items-center gap-2 rounded border px-2 py-1 text-[0.6875rem] ${
        failed
          ? "border-rose-400/40 bg-rose-400/10 text-rose-200"
          : "border-[var(--color-hairline)] bg-white/[0.04] text-slate-400"
      }`}
    >
      <span
        className={`size-1.5 rounded-full ${
          failed
            ? "bg-rose-400"
            : done
              ? "bg-emerald-400"
              : "animate-pulse bg-sky-400"
        }`}
      />
      {failed ? `${label} failed: ${errorText ?? "unknown error"}` : label}
    </div>
  );
}

export function ChatDrawer({ enabled }: { enabled: boolean }) {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const scroller = useRef<HTMLDivElement>(null);

  const { messages, sendMessage, status, error } = useChat({
    transport: new DefaultChatTransport({ api: "/api/chat" }),
  });

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [messages, status]);

  function submit(text: string) {
    const trimmed = text.trim();
    if (!trimmed || status === "streaming" || status === "submitted") return;
    sendMessage({ text: trimmed });
    setInput("");
  }

  return (
    <>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="fixed right-5 bottom-5 z-50 rounded-full border border-[var(--color-hairline)] bg-[var(--color-surface-raised)] px-4 py-2.5 text-sm text-slate-200 shadow-lg shadow-black/40 transition-colors hover:bg-white/10"
      >
        {open ? "Close copilot" : "Ask the copilot"}
      </button>

      <aside
        aria-hidden={!open}
        className={`fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col border-l border-[var(--color-hairline)] bg-[var(--color-surface)] transition-transform duration-200 ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <header className="border-b border-[var(--color-hairline)] px-5 py-4">
          <h2 className="text-sm font-medium text-slate-100">Copilot</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Reads your plan through the scheduler API. It cannot place or commit
            bids.
          </p>
        </header>

        {!enabled ? (
          <div className="flex flex-1 items-center px-5">
            <div className="rounded-lg border border-[var(--color-hairline)] bg-[var(--color-surface-raised)] p-4 text-sm leading-relaxed text-slate-300">
              <p className="font-medium text-slate-100">Copilot is disabled</p>
              <p className="mt-2 text-slate-400">
                <code className="rounded bg-black/40 px-1 py-0.5 text-xs">
                  ANTHROPIC_API_KEY
                </code>{" "}
                is not set. Add it to{" "}
                <code className="rounded bg-black/40 px-1 py-0.5 text-xs">
                  frontend/.env.local
                </code>{" "}
                and restart the dev server. Everything else on this page works
                without it — the schedule comes from the engine, not the model.
              </p>
            </div>
          </div>
        ) : (
          <>
            <div ref={scroller} className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
              {messages.length === 0 ? (
                <div className="space-y-2">
                  <p className="text-xs text-slate-500">Try asking:</p>
                  {SUGGESTIONS.map((s) => (
                    <button
                      key={s}
                      onClick={() => submit(s)}
                      className="block w-full rounded-lg border border-[var(--color-hairline)] px-3 py-2 text-left text-xs text-slate-300 transition-colors hover:bg-white/5"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              ) : null}

              {messages.map((message) => (
                <div key={message.id}>
                  <div className="mb-1 text-[0.6875rem] uppercase tracking-wider text-slate-500">
                    {message.role === "user" ? "You" : "Copilot"}
                  </div>
                  <div className="space-y-1 text-sm leading-relaxed text-slate-200">
                    {message.parts.map((part, index) => {
                      if (part.type === "text") {
                        return (
                          <p key={index} className="whitespace-pre-wrap">
                            {part.text}
                          </p>
                        );
                      }
                      // One chip for every tool rather than a case per tool: the
                      // interesting states (running, done, failed) are the same
                      // for all four, and the outputs are for the model to read,
                      // not the user.
                      if (part.type.startsWith("tool-")) {
                        const call = part as unknown as {
                          type: string;
                          toolCallId: string;
                          state: string;
                          errorText?: string;
                        };
                        return (
                          <ToolChip
                            key={call.toolCallId}
                            name={call.type.slice("tool-".length)}
                            state={call.state}
                            errorText={call.errorText}
                          />
                        );
                      }
                      return null;
                    })}
                  </div>
                </div>
              ))}

              {status === "submitted" ? (
                <p className="text-xs text-slate-500">Thinking…</p>
              ) : null}

              {error ? (
                <p className="rounded border border-rose-400/40 bg-rose-400/10 px-3 py-2 text-xs text-rose-200">
                  {error.message}
                </p>
              ) : null}
            </div>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                submit(input);
              }}
              className="border-t border-[var(--color-hairline)] p-4"
            >
              <div className="flex gap-2">
                <input
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="Ask about the schedule…"
                  aria-label="Message the copilot"
                  className="flex-1 rounded-lg border border-[var(--color-hairline)] bg-[var(--color-surface-raised)] px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-slate-500"
                />
                <button
                  type="submit"
                  disabled={
                    !input.trim() ||
                    status === "streaming" ||
                    status === "submitted"
                  }
                  className="rounded-lg bg-slate-200 px-3 py-2 text-sm font-medium text-slate-900 transition-opacity disabled:opacity-40"
                >
                  Send
                </button>
              </div>
              <p className="mt-2 text-[0.6875rem] text-slate-500">
                Informational only. Grey market premium is unofficial and
                unregulated; nothing here is financial advice.
              </p>
            </form>
          </>
        )}
      </aside>
    </>
  );
}
