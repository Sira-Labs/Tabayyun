import { useQuery } from "@tanstack/react-query";
import { apiGet } from "./api";
import { RunChecks } from "./RunChecks";

type Version = { version: string; env: string };

export function App() {
  const version = useQuery({ queryKey: ["version"], queryFn: () => apiGet<Version>("/api/version") });

  return (
    <main className="min-h-dvh bg-slate-50 px-4 py-8 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <div className="mx-auto max-w-5xl space-y-6">
        <header className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="flex items-center gap-3">
            <img src="/favicon.svg" alt="" width="40" height="40" className="h-10 w-10 rounded-lg" />
            <div>
              <h1 className="text-2xl font-semibold tracking-tight">Tabayyun</h1>
              <p className="text-sm text-slate-600 dark:text-slate-400">Verify time series before you act on them</p>
            </div>
          </div>
          <p className="text-xs text-slate-500">
            {version.isPending && "Connecting to API…"}
            {version.isError && <span className="text-red-600 dark:text-red-400">API unreachable: {version.error.message}</span>}
            {version.data && `API ${version.data.version} · ${version.data.env}`}
          </p>
        </header>
        <RunChecks />
      </div>
    </main>
  );
}
