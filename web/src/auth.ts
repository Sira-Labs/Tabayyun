// Sign-in, the current user and devices (spec 013). The api is a backend-for-frontend: the
// browser holds only an HttpOnly session cookie, and sign-in and sign-out are full-page
// navigations through the identity provider.
import { apiGet, apiSend } from "./api";

export type SignInMethod = "google" | "github" | "passkey";
export type SignInOptions = Record<SignInMethod, boolean>;

export type Me = {
  user: { id: string; email: string; display_name: string };
  org: { id: string; name: string };
  role: string;
  /** `google`, `github`, `passkey`, or `dev` when the server runs without login. */
  sign_in_method: string;
  passkey_fresh: boolean;
};

export type Device = {
  id: string;
  current: boolean;
  sign_in_method: string;
  created_at: string;
  last_seen_at: string;
  user_agent: string | null;
  ip_address: string | null;
};

export const SIGN_IN_METHODS: SignInMethod[] = ["google", "github", "passkey"];

export const SIGN_IN_LABELS: Record<SignInMethod, string> = {
  google: "Continue with Google",
  github: "Continue with GitHub",
  passkey: "Sign in with a passkey",
};

export const METHOD_NAMES: Record<string, string> = { google: "Google", github: "GitHub", passkey: "Passkey", dev: "Dev mode" };

/** `value` when it is a path on this site, else `/` (the server checks again). */
export function safeNext(value: string | undefined | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return "/";
  return value;
}

/** Where a sign-in button navigates. */
export function loginUrl(method: SignInMethod, next: string): string {
  return `/api/auth/login?${new URLSearchParams({ method, next: safeNext(next) })}`;
}

/** Page navigation, replaceable in tests (jsdom cannot navigate). */
export const browser = {
  assign(url: string): void {
    window.location.assign(url);
  },
};

export const authApi = {
  me: () => apiGet<Me>("/api/auth/me"),
  signInOptions: () => apiGet<SignInOptions>("/api/auth/sign-in-options"),
  sessions: () => apiGet<Device[]>("/api/auth/sessions"),
  revokeSession: (id: string) => apiSend<void>("DELETE", `/api/auth/sessions/${encodeURIComponent(id)}`),
  revokeOthers: () => apiSend<void>("POST", "/api/auth/sessions/revoke-others"),
  logout: () => apiSend<{ logout_url: string }>("POST", "/api/auth/logout"),
};

/** Sign out here, then at the identity provider (which returns to the app). */
export async function signOut(): Promise<void> {
  const { logout_url } = await authApi.logout();
  browser.assign(logout_url);
}

/** A short name for a browser from its user agent, e.g. `Firefox on Windows`. */
export function deviceName(userAgent: string | null): string {
  if (!userAgent) return "Unknown device";
  const browserName = /Edg\//.test(userAgent)
    ? "Edge"
    : /Firefox\//.test(userAgent)
      ? "Firefox"
      : /Chrome\//.test(userAgent)
        ? "Chrome"
        : /Safari\//.test(userAgent)
          ? "Safari"
          : "Browser";
  const os = /iPhone|iPad/.test(userAgent)
    ? "iOS"
    : /Android/.test(userAgent)
      ? "Android"
      : /Windows/.test(userAgent)
        ? "Windows"
        : /Mac OS X/.test(userAgent)
          ? "macOS"
          : /Linux/.test(userAgent)
            ? "Linux"
            : null;
  return os ? `${browserName} on ${os}` : browserName;
}
