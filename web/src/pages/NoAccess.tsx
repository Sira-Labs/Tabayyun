import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { signOut } from "../auth";
import { PlainFrame, buttonClass } from "../components/Brand";

/** Signed in, but without a membership in any organisation (403 `no_access`). "Check again"
 * asks `/api/auth/me` once more, which joins an invitation sent since (spec 014). */
export function NoAccess({ email }: { email: string | null }) {
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  return (
    <PlainFrame>
      <section aria-labelledby="no-access-heading" className="space-y-4">
        <h1 id="no-access-heading" className="text-lg font-semibold">
          No access yet
        </h1>
        <p>
          You are signed in{email ? <> as <strong>{email}</strong></> : null}, but this account has no access to an
          organisation yet. Ask an admin of your organisation to invite this email address, then check again.
        </p>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={buttonClass} onClick={() => void client.invalidateQueries({ queryKey: ["me"] })}>
            Check again
          </button>
          <button type="button" className={buttonClass} onClick={() => signOut().catch((e: Error) => setError(e.message))}>
            Sign out
          </button>
        </div>
        {error && (
          <p role="alert" className="text-red-700 dark:text-red-400">
            Could not sign out: {error}
          </p>
        )}
      </section>
    </PlainFrame>
  );
}
