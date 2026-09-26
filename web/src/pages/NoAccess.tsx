import { useState } from "react";
import { signOut } from "../auth";
import { PlainFrame, buttonClass } from "../components/Brand";

/** Signed in, but without a membership in any organisation (403 `no_access`). */
export function NoAccess({ email }: { email: string | null }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <PlainFrame>
      <section aria-labelledby="no-access-heading" className="space-y-4">
        <h1 id="no-access-heading" className="text-lg font-semibold">
          No access yet
        </h1>
        <p>
          You are signed in{email ? <> as <strong>{email}</strong></> : null}, but this account has no access to an
          organisation yet. Ask an owner of your organisation to invite you, then sign in again.
        </p>
        <button type="button" className={buttonClass} onClick={() => signOut().catch((e: Error) => setError(e.message))}>
          Sign out
        </button>
        {error && (
          <p role="alert" className="text-red-700 dark:text-red-400">
            Could not sign out: {error}
          </p>
        )}
      </section>
    </PlainFrame>
  );
}
