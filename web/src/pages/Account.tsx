import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { METHOD_NAMES, authApi, browser, deviceName, type Device } from "../auth";
import { buttonClass } from "../components/Brand";
import { formatLocalTime } from "../format";

const ADMIN_ROLES = new Set(["owner", "admin"]);

/** `/settings/account`: who is signed in, signed-in devices, and passkeys. */
export function Account() {
  const client = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: authApi.me, retry: false });
  const devices = useQuery({ queryKey: ["sessions"], queryFn: authApi.sessions });
  const refresh = () => client.invalidateQueries({ queryKey: ["sessions"] });
  const revoke = useMutation({
    mutationFn: (device: Device) => authApi.revokeSession(device.id),
    // Signing out this device ends the session: back to the sign-in page.
    onSuccess: (_, device) => (device.current ? browser.assign("/login") : refresh()),
  });
  const revokeOthers = useMutation({ mutationFn: authApi.revokeOthers, onSuccess: refresh });
  const others = devices.data?.filter((d) => !d.current).length ?? 0;
  const dev = me.data?.sign_in_method === "dev";

  return (
    <section aria-labelledby="account-heading" className="space-y-6">
      <h1 id="account-heading" className="text-lg font-semibold">
        Account
      </h1>
      {me.data && (
        <p>
          Signed in as <strong>{me.data.user.email}</strong> ({me.data.role} of {me.data.org.name}) with{" "}
          {METHOD_NAMES[me.data.sign_in_method] ?? me.data.sign_in_method}.
        </p>
      )}
      {me.data && ADMIN_ROLES.has(me.data.role) && !me.data.passkey_fresh && (
        <p role="note" className="rounded border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-700 dark:bg-amber-950">
          Admin actions will ask for a sign-in with a passkey from the last 12 hours. Add a passkey under “Manage
          passkeys”, then sign in with it.
        </p>
      )}

      <section aria-labelledby="devices-heading" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="devices-heading" className="font-semibold">
            Signed-in devices
          </h2>
          <button type="button" className={buttonClass} disabled={others === 0 || revokeOthers.isPending} onClick={() => revokeOthers.mutate()}>
            Sign out all other devices
          </button>
        </div>
        {dev && <p className="text-slate-600 dark:text-slate-400">This server runs without sign-in (dev mode).</p>}
        {devices.isPending && <p>Loading devices…</p>}
        {devices.isError && (
          <p role="alert" className="text-red-700 dark:text-red-400">
            Could not load devices: {devices.error.message}
          </p>
        )}
        {(revoke.isError || revokeOthers.isError) && (
          <p role="alert" className="text-red-700 dark:text-red-400">
            Could not sign out: {(revoke.error ?? revokeOthers.error)?.message}
          </p>
        )}
        {devices.data && devices.data.length > 0 && (
          <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white dark:divide-slate-800 dark:border-slate-800 dark:bg-slate-900">
            {devices.data.map((d) => (
              <li key={d.id} className="flex flex-wrap items-center justify-between gap-2 p-3" aria-label={deviceName(d.user_agent)}>
                <div className="text-sm">
                  <div className="font-medium">
                    {deviceName(d.user_agent)}
                    {d.current && <span className="ml-2 rounded bg-emerald-100 px-2 py-0.5 text-xs text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200">This device</span>}
                  </div>
                  <div className="text-xs text-slate-600 dark:text-slate-400">
                    {METHOD_NAMES[d.sign_in_method] ?? d.sign_in_method} · last active {formatLocalTime(d.last_seen_at)}
                    {d.ip_address && ` · ${d.ip_address}`}
                  </div>
                </div>
                <button type="button" className={buttonClass} disabled={revoke.isPending} onClick={() => revoke.mutate(d)}>
                  Sign out
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="passkeys-heading" className="space-y-2">
        <h2 id="passkeys-heading" className="font-semibold">
          Passkeys
        </h2>
        <p className="text-sm text-slate-600 dark:text-slate-400">Add, rename or remove passkeys at the sign-in service.</p>
        {!dev && (
          <a href="/api/auth/passkeys" className={buttonClass}>
            Manage passkeys
          </a>
        )}
      </section>
    </section>
  );
}
