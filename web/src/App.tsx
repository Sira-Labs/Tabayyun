import { useQuery } from "@tanstack/react-query";
import { apiGet } from "./api";

type Version = { version: string; env: string };

export function App() {
  const version = useQuery({ queryKey: ["version"], queryFn: () => apiGet<Version>("/api/version") });

  return (
    <main className="min-h-dvh bg-slate-50 px-4 py-8 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <div className="mx-auto max-w-3xl">
        <header className="mb-8">
          <h1 className="text-2xl font-semibold tracking-tight">Tabayyun</h1>
          <p className="text-sm text-slate-600 dark:text-slate-400">Time-series data quality</p>
        </header>
        <section className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="mb-2 text-base font-medium">API</h2>
          {version.isPending && <p className="text-sm">Connecting…</p>}
          {version.isError && (
            <p className="text-sm text-red-600 dark:text-red-400">API unreachable: {version.error.message}</p>
          )}
          {version.data && (
            <p className="text-sm">
              Version {version.data.version} · environment {version.data.env}
            </p>
          )}
        </section>
      </div>
    </main>
  );
}
