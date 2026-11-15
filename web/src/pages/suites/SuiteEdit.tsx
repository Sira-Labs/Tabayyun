import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { suitePatch, suitesApi, type Suite, type SuiteInput } from "../../suites";
import { ErrorLine } from "../admin/ui";
import { FormDataStatus, SuiteForm, useSuiteFormData } from "./SuiteForm";

/** `/suites/:id/edit`: change a suite (editors, spec 027). Only what changed is sent. */
export function SuiteEdit() {
  const { suiteId } = useParams({ from: "/_app/suites/$suiteId/edit" });
  const role = useWorkspaceRole();
  const navigate = useNavigate();
  const client = useQueryClient();
  const data = useSuiteFormData();
  const suite = useQuery({
    queryKey: ["suites", suiteId, "detail"],
    queryFn: () => suitesApi.get(suiteId),
    refetchOnWindowFocus: false,
  });
  // The form and the patch use one snapshot of the suite, taken once any refetch of a cached
  // copy has settled. A later refetch must not move the comparison under the form: fields the
  // user did not touch would be sent back with their old values over someone else's change.
  const [snapshot, setSnapshot] = useState<Suite | null>(null);
  if (snapshot === null && suite.data && !suite.isFetching) setSnapshot(suite.data);
  const back = () => void navigate({ to: "/suites/$suiteId", params: { suiteId } });
  const save = useMutation({
    mutationFn: (patch: Partial<SuiteInput>) => suitesApi.update(suiteId, patch),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["suites"] });
      back();
    },
  });

  if (role !== null && !atLeast(role, "editor")) {
    return <p role="alert">Only editors and admins change suites.</p>;
  }
  const current = snapshot;
  const submit = (input: SuiteInput) => {
    if (!current) return;
    const patch = suitePatch(current, input);
    if (Object.keys(patch).length === 0) back();
    else save.mutate(patch);
  };
  return (
    <section aria-labelledby="edit-suite-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="edit-suite-heading" className="text-lg font-semibold">
          Edit suite{current ? `: ${current.name}` : ""}
        </h1>
        <Link to="/suites/$suiteId" params={{ suiteId }} className="text-sm underline">
          Back to the suite
        </Link>
      </div>
      {!current && !suite.isError && <p>Loading suite…</p>}
      <ErrorLine error={suite.error} prefix="Could not load the suite" />
      <FormDataStatus data={data} />
      {current && data.catalogue.data && data.datasets.data && (
        <SuiteForm
          key={current.id}
          catalogue={data.catalogue.data}
          datasets={data.datasets.data.items}
          suite={current}
          submitLabel="Save suite"
          pending={save.isPending}
          error={save.error}
          onSubmit={submit}
        />
      )}
    </section>
  );
}
