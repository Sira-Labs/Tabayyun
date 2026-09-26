import { useQuery } from "@tanstack/react-query";
import { Link, Outlet } from "@tanstack/react-router";
import { useState } from "react";
import { ApiError, apiGet } from "../api";
import { authApi, signOut } from "../auth";
import { Brand } from "../components/Brand";
import { NoAccess } from "./NoAccess";

type Version = { version: string; env: string };

const navClass =
  "rounded px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-200 dark:text-slate-300 dark:hover:bg-slate-800";
const navActive = "bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-white";

/** The email a 403 `no_access` answer names, if any. */
function noAccessEmail(error: unknown): string | null | undefined {
  if (!(error instanceof ApiError) || error.status !== 403) return undefined;
  const body = error.body as { detail?: unknown; email?: unknown } | null;
  if (body?.detail !== "no_access") return undefined;
  return typeof body.email === "string" ? body.email : null;
}

/** Page frame of the signed-in app: checks the session (spec 013), then the header with
 * navigation, the user and sign-out, the route, and the API status. A 401 goes to /login
 * (the API client's handler); a user without access sees "No access yet". */
export function Layout() {
  const me = useQuery({ queryKey: ["me"], queryFn: authApi.me, retry: false, staleTime: 60_000 });
  const version = useQuery({ queryKey: ["version"], queryFn: () => apiGet<Version>("/api/version") });
  const [signOutError, setSignOutError] = useState<string | null>(null);

  const denied = noAccessEmail(me.error);
  if (denied !== undefined) return <NoAccess email={denied} />;
  if (me.isPending || (me.error instanceof ApiError && me.error.status === 401)) {
    return <p className="p-6 text-slate-600">Checking your session…</p>;
  }

  return (
    <div className="min-h-dvh bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:rounded focus:bg-white focus:px-3 focus:py-2 dark:focus:bg-slate-900">
        Skip to content
      </a>
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <Link to="/runs">
            <Brand />
          </Link>
          <nav aria-label="Main" className="flex flex-wrap items-center gap-1">
            <Link to="/runs" activeOptions={{ exact: true }} className={navClass} activeProps={{ className: navActive }}>
              Runs
            </Link>
            <Link to="/runs/new" className={navClass} activeProps={{ className: navActive }}>
              New run
            </Link>
            <Link to="/settings/account" className={navClass} activeProps={{ className: navActive }}>
              Account
            </Link>
            {me.data && me.data.sign_in_method !== "dev" && (
              <button type="button" className={navClass} onClick={() => signOut().catch((e: Error) => setSignOutError(e.message))}>
                Sign out <span className="sr-only">{me.data.user.display_name}</span>
              </button>
            )}
          </nav>
        </div>
        {me.data && (
          <div className="mx-auto max-w-5xl px-4 pb-2 text-xs text-slate-600 dark:text-slate-400">
            {me.data.user.display_name} · {me.data.org.name}
          </div>
        )}
      </header>
      <main id="main" className="mx-auto max-w-5xl space-y-6 px-4 py-6">
        {me.isError && (
          <p role="alert" className="text-red-700 dark:text-red-400">
            Could not check your session: {me.error.message}
          </p>
        )}
        {signOutError && (
          <p role="alert" className="text-red-700 dark:text-red-400">
            Could not sign out: {signOutError}
          </p>
        )}
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
