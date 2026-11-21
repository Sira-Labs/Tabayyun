import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { alertKeys, alertsApi, bodyOfForm, type AlertFormModel } from "../../alerts";
import { useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { AlertForm, AlertFormDataStatus, useAlertFormData } from "./AlertForm";

/** `/alerts/new`: create an alert rule (workspace admins, spec 031); then its page. */
export function AlertNew() {
  const role = useWorkspaceRole();
  const navigate = useNavigate();
  const client = useQueryClient();
  const data = useAlertFormData();
  const create = useMutation({
    mutationFn: (form: AlertFormModel) => alertsApi.create(bodyOfForm(form)),
    onSuccess: (rule) => {
      void client.invalidateQueries({ queryKey: alertKeys.all });
      void navigate({ to: "/alerts/$alertId", params: { alertId: rule.id } });
    },
  });

  if (role !== null && role !== "admin") {
    return <p role="alert">Only workspace admins create alert rules.</p>;
  }
  return (
    <section aria-labelledby="new-alert-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="new-alert-heading" className="text-lg font-semibold">
          New alert rule
        </h1>
        <Link to="/alerts" className="text-sm underline">
          All alerts
        </Link>
      </div>
      <AlertFormDataStatus data={data} />
      {data.catalogue.data && data.channels.data && (
        <AlertForm
          catalogue={data.catalogue.data}
          channels={data.channels.data}
          submitLabel="Create rule"
          pending={create.isPending}
          error={create.error}
          onSubmit={(form) => create.mutate(form)}
        />
      )}
    </section>
  );
}
