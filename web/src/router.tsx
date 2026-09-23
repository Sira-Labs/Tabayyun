import { createRootRoute, createRoute, createRouter, redirect, type RouterHistory } from "@tanstack/react-router";
import { Layout } from "./pages/Layout";
import { RunForm } from "./pages/RunForm";
import { RunReport } from "./pages/RunReport";
import { RunsList } from "./pages/RunsList";

function NotFound() {
  return <p>Page not found.</p>;
}

const rootRoute = createRootRoute({ component: Layout, notFoundComponent: NotFound });

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/runs" });
  },
});
const runsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/runs", component: RunsList });
const newRunRoute = createRoute({ getParentRoute: () => rootRoute, path: "/runs/new", component: RunForm });
const runRoute = createRoute({ getParentRoute: () => rootRoute, path: "/runs/$runId", component: RunReport });

export const routeTree = rootRoute.addChildren([indexRoute, runsRoute, newRunRoute, runRoute]);

/** The app router; tests pass a memory history. */
export function makeRouter(history?: RouterHistory) {
  return createRouter({ routeTree, history, defaultPreload: "intent" });
}

export const router = makeRouter();

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
