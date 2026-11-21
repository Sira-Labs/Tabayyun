import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { alertKeys, alertPatch, alertsApi, type AlertFormModel, type AlertRule, type AlertRuleBody } from "../../alerts";
import { useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { ErrorLine } from "../admin/ui";
import { AlertForm, AlertFormDataStatus, useAlertFormData } from "./AlertForm";

/** `/alerts/:id/edit`: change an alert rule (workspace admins, spec 031). Only what changed is sent. */
export function AlertEdit() {
  const { alertId } = useParams({ from: "/_app/alerts/$alertId/edit" });
  // Keyed by rule, so one rule's snapshot is never saved to another.
  return <AlertEditBody key={alertId} alertId={alertId} />;
}

function AlertEditBody({ alertId }: { alertId: string }) {
  const role = useWorkspaceRole();
  const navigate = useNavigate();
  const client = useQueryClient();
  const data = useAlertFormData();
  const rule = useQuery({ queryKey: alertKeys.detail(alertId), queryFn: () => alertsApi.get(alertId), refetchOnWindowFocus: false });
  // The form and the patch compare against one snapshot, taken once a refetch has settled, so
  // a later refetch cannot send untouched fields back over someone else's change.
  const [snapshot, setSnapshot] = useState<AlertRule | null>(null);
  if (snapshot === null && rule.data && !rule.isFetching) setSnapshot(rule.data);
  const back = () => void navigate({ to: "/alerts/$alertId", params: { alertId } });
  const save = useMutation({
    mutationFn: (patch: AlertRuleBody) => alertsApi.patch(alertId, patch),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: alertKeys.all });
      back();
    },
  });

  if (role !== null && role !== "admin") {
    return <p role="alert">Only workspace admins change alert rules.</p>;
  }
  const current = snapshot;
  const submit = (form: AlertFormModel) => {
    if (!current) return;
    const patch = alertPatch(current, form);
    if (Object.keys(patch).length === 0) back();
    else save.mutate(patch);
  };
  return (
    <section aria-labelledby="edit-alert-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="edit-alert-heading" className="text-lg font-semibold">
          Edit alert rule{current ? `: ${current.name}` : ""}
        </h1>
        <Link to="/alerts/$alertId" params={{ alertId }} className="text-sm underline">
          Back to the rule
        </Link>
      </div>
      {!current && !rule.isError && <p>Loading rule…</p>}
      <ErrorLine error={rule.error} prefix="Could not load the rule" />
      <AlertFormDataStatus data={data} />
      {current && data.catalogue.data && data.channels.data && (
        <AlertForm
          key={current.id}
          catalogue={data.catalogue.data}
          channels={data.channels.data}
          rule={current}
          submitLabel="Save rule"
          pending={save.isPending}
          error={save.error}
          onSubmit={submit}
        />
      )}
    </section>
  );
}
