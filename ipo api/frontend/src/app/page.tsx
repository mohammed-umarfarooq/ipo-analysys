import { Dashboard } from "@/components/Dashboard";
import { ApiError, api } from "@/lib/api";

// Every figure is derived from live balances and a dated calendar, so the page is
// rendered per request. A cached dashboard would show a plan for a past week.
export const dynamic = "force-dynamic";

function Problem({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl items-center px-6">
      <div className="w-full rounded-xl border border-[var(--color-hairline)] bg-[var(--color-surface-raised)] p-6">
        <h1 className="text-base font-semibold text-slate-50">{title}</h1>
        <div className="mt-3 space-y-3 text-sm leading-relaxed text-slate-300">
          {children}
        </div>
      </div>
    </main>
  );
}

export default async function Page() {
  // The `try` wraps only the fetches, deliberately. If it also wrapped the JSX
  // below, this `catch` would look like it handled render errors while React
  // renders too late for that to be true — so a genuine failure inside
  // <Dashboard> would fall through to a "scheduler API error" panel that names
  // the wrong culprit. Render failures belong to an error boundary.
  let data: [
    Awaited<ReturnType<typeof api.health>>,
    Awaited<ReturnType<typeof api.userState>>,
    Awaited<ReturnType<typeof api.ipos>>,
    Awaited<ReturnType<typeof api.history>>,
    Awaited<ReturnType<typeof api.compare>> | null,
  ];

  try {
    // One round trip each, in parallel. The comparison carries both policies so
    // the toggle in the UI needs no further request.
    data = await Promise.all([
      api.health(),
      api.userState(),
      api.ipos(),
      api.history(),
      /**
       * A plan is optional; the rest of the page is not.
       *
       * The engine answers 409 when no active PAN holds any capital — which is
       * exactly the first run, before anything has been entered. That is a starting
       * point, not a failure, so it becomes a null plan and the dashboard renders
       * the inputs that will produce a real one. The 409 is caught here rather than
       * below so this stays one parallel round trip, and so a genuine outage still
       * reaches the panels underneath.
       */
      api.compare({ assumption: "expected" }).catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 409) return null;
        throw error;
      }),
    ]);
  } catch (error) {
    if (error instanceof ApiError && error.status === null) {
      return (
        <Problem title="The scheduler API is not running">
          <p>
            This dashboard renders a plan computed by the FastAPI engine; it has no
            allocation logic of its own and nothing to show without it.
          </p>
          <p className="text-slate-400">Start the backend:</p>
          <pre className="overflow-x-auto rounded-lg bg-black/50 p-3 text-xs text-slate-300">
            cd backend{"\n"}uv run uvicorn app.main:app --reload --port 8000
          </pre>
          <p className="text-xs text-slate-500">{error.detail}</p>
        </Problem>
      );
    }

    if (error instanceof ApiError) {
      return (
        <Problem title="The scheduler API returned an error">
          <p>{error.message}</p>
          <p className="text-xs text-slate-500">HTTP {error.status}</p>
        </Problem>
      );
    }

    throw error;
  }

  const [health, user, ipos, applications, comparison] = data;

  return (
    <Dashboard
      user={user}
      ipos={ipos}
      applications={applications}
      initialComparison={comparison}
      chatEnabled={Boolean(process.env.ANTHROPIC_API_KEY)}
      warnings={health.production_warnings}
    />
  );
}
