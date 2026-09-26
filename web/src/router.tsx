import { Outlet, createRootRoute, createRoute, createRouter, redirect, type RouterHistory } from "@tanstack/react-router";
import { setUnauthorizedHandler } from "./api";
import { Account } from "./pages/Account";
import { Layout } from "./pages/Layout";
import { Login } from "./pages/Login";
import { RunForm } from "./pages/RunForm";
import { RunReport } from "./pages/RunReport";
import { RunsList } from "./pages/RunsList";

/** Fallback for unknown paths. */
function NotFound() {
  return <p className="p-6">Page not found.</p>;
}

const rootRoute = createRootRoute({ component: Outlet, notFoundComponent: NotFound });

// Everything but /login lives under the signed-in layout, which checks the session.
const appRoute = createRoute({ getParentRoute: () => rootRoute, id: "_app", component: Layout });

const indexRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/runs" });
  },
});
const runsRoute = createRoute({ getParentRoute: () => appRoute, path: "/runs", component: RunsList });
const newRunRoute = createRoute({ getParentRoute: () => appRoute, path: "/runs/new", component: RunForm });
const runRoute = createRoute({ getParentRoute: () => appRoute, path: "/runs/$runId", component: RunReport });
const accountRoute = createRoute({ getParentRoute: () => appRoute, path: "/settings/account", component: Account });

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: (search: Record<string, unknown>): { next?: string } =>
    typeof search.next === "string" ? { next: search.next } : {},
  component: Login,
});

export const routeTree = rootRoute.addChildren([
  appRoute.addChildren([indexRoute, runsRoute, newRunRoute, runRoute, accountRoute]),
  loginRoute,
]);

/** The app router; tests pass a memory history. A 401 from any API call goes to /login with
 * the current path as `next`. */
export function makeRouter(history?: RouterHistory) {
  const router = createRouter({ routeTree, history, defaultPreload: "intent" });
  setUnauthorizedHandler(() => {
    const { pathname, href } = router.state.location;
    if (pathname !== "/login") void router.navigate({ to: "/login", search: { next: href } });
  });
  return router;
}

export const router = makeRouter();

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
