import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { suitePatch, suitesApi, type SuiteInput } from "../../suites";
import { ErrorLine } from "../admin/ui";
import { FormDataStatus, SuiteForm, useSuiteFormData } from "./SuiteForm";

/** `/suites/:id/edit`: change a suite (editors, spec 027). Only what changed is sent. */
export function SuiteEdit() {
  const { suiteId } = useParams({ from: "/_app/suites/$suiteId/edit" });
  const role = useWorkspaceRole();
  const navigate = useNavigate();
  const client = useQueryClient();
  const data = useSuiteFormData();
  // Not refetched while the form is open: the form starts from the suite as it was loaded.
  const suite = useQuery({
    queryKey: ["suites", suiteId, "detail"],
    queryFn: () => suitesApi.get(suiteId),
    refetchOnWindowFocus: false,
  });
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
  const current = suite.data;
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
      {suite.isPending && <p>Loading suite…</p>}
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
