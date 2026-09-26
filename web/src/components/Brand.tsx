/** Logo and tagline; a link target is up to the caller. */
export function Brand() {
  return (
    <span className="flex items-center gap-3">
      <img src="/favicon.svg" alt="" width="36" height="36" className="h-9 w-9 rounded-lg" />
      <span>
        <span className="block text-xl font-semibold tracking-tight">Tabayyun</span>
        <span className="block text-xs text-slate-600 dark:text-slate-400">Verify time series before you act on them</span>
      </span>
    </span>
  );
}

/** Page frame without navigation, for sign-in and "no access". */
export function PlainFrame({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-dvh bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <main id="main" className="mx-auto flex max-w-md flex-col gap-6 px-4 py-12">
        <Brand />
        {children}
      </main>
    </div>
  );
}

export const buttonClass =
  "inline-flex min-h-11 items-center justify-center rounded border border-slate-300 bg-white px-4 py-2 text-sm font-medium hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800";
