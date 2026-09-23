import { useQuery } from "@tanstack/react-query";
import { Link, Outlet } from "@tanstack/react-router";
import { apiGet } from "../api";

type Version = { version: string; env: string };

const navClass =
  "rounded px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-200 dark:text-slate-300 dark:hover:bg-slate-800";
const navActive = "bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-white";

/** Page frame: skip link, header with navigation and API status, then the route. */
export function Layout() {
  const version = useQuery({ queryKey: ["version"], queryFn: () => apiGet<Version>("/api/version") });

  return (
    <div className="min-h-dvh bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:rounded focus:bg-white focus:px-3 focus:py-2 dark:focus:bg-slate-900">
        Skip to content
      </a>
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <Link to="/runs" className="flex items-center gap-3">
            <img src="/favicon.svg" alt="" width="36" height="36" className="h-9 w-9 rounded-lg" />
            <span>
              <span className="block text-xl font-semibold tracking-tight">Tabayyun</span>
              <span className="block text-xs text-slate-600 dark:text-slate-400">Verify time series before you act on them</span>
            </span>
          </Link>
          <nav aria-label="Main" className="flex items-center gap-1">
            <Link to="/runs" activeOptions={{ exact: true }} className={navClass} activeProps={{ className: navActive }}>
              Runs
            </Link>
            <Link to="/runs/new" className={navClass} activeProps={{ className: navActive }}>
              New run
            </Link>
          </nav>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-5xl space-y-6 px-4 py-6">
        <Outlet />
      </main>
      <footer className="mx-auto max-w-5xl px-4 pb-6 text-xs text-slate-600 dark:text-slate-400">
        {version.isPending && "Connecting to API…"}
        {version.isError && <span className="text-red-700 dark:text-red-400">API unreachable: {version.error.message}</span>}
        {version.data && `API ${version.data.version} · ${version.data.env}`}
      </footer>
    </div>
  );
}
