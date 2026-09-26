import { useQuery } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { SIGN_IN_LABELS, SIGN_IN_METHODS, authApi, loginUrl, safeNext } from "../auth";
import { PlainFrame, buttonClass } from "../components/Brand";

/** `/login`: one button per enabled sign-in method; each is a navigation through the IdP. */
export function Login() {
  const { next } = useSearch({ from: "/login" });
  const options = useQuery({ queryKey: ["sign-in-options"], queryFn: authApi.signInOptions });
  const target = safeNext(next);
  const methods = SIGN_IN_METHODS.filter((m) => options.data?.[m]);

  return (
    <PlainFrame>
      <section aria-labelledby="login-heading" className="space-y-4">
        <h1 id="login-heading" className="text-lg font-semibold">
          Sign in
        </h1>
        {options.isPending && <p>Loading sign-in options…</p>}
        {options.isError && (
          <p role="alert" className="text-red-700 dark:text-red-400">
            Could not reach the API: {options.error.message}
          </p>
        )}
        {methods.length > 0 && (
          <ul className="flex flex-col gap-2">
            {methods.map((method) => (
              <li key={method}>
                <a href={loginUrl(method, target)} className={`${buttonClass} w-full`}>
                  {SIGN_IN_LABELS[method]}
                </a>
              </li>
            ))}
          </ul>
        )}
        {options.isSuccess && methods.length === 0 && (
          <p className="text-slate-600 dark:text-slate-400">
            This server runs without sign-in (dev mode).{" "}
            <Link to="/runs" className="underline">
              Continue
            </Link>
          </p>
        )}
      </section>
    </PlainFrame>
  );
}
