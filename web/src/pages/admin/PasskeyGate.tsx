import { loginUrl } from "../../auth";
import { buttonClass } from "../../components/Brand";

/** Shown when an admin call answers `second-factor-required` (spec 013's gate, applied by spec
 * 014): admin actions need a passkey sign-in from the last 12 hours. */
export function PasskeyGate() {
  return (
    <section aria-labelledby="passkey-gate-heading" className="space-y-3">
      <h2 id="passkey-gate-heading" className="font-semibold">
        Sign in with a passkey to continue
      </h2>
      <p>
        Admin actions need a sign-in with a passkey from the last 12 hours. Google and GitHub sign-ins do not count for
        them.
      </p>
      <div className="flex flex-wrap gap-2">
        <a href={loginUrl("passkey", "/admin")} className={buttonClass}>
          Sign in with a passkey
        </a>
        <a href="/api/auth/passkeys" className={buttonClass}>
          Manage passkeys
        </a>
      </div>
      <p className="text-sm text-slate-600 dark:text-slate-400">
        No passkey yet? Add one under “Manage passkeys” first, then sign in with it.
      </p>
    </section>
  );
}
