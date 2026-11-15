import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { suitesApi } from "../../suites";
import { FormDataStatus, SuiteForm, useSuiteFormData } from "./SuiteForm";

/** `/suites/new`: create a suite (editors, spec 027); then its page. */
export function SuiteNew() {
  const role = useWorkspaceRole();
  const navigate = useNavigate();
  const client = useQueryClient();
  const data = useSuiteFormData();
  const create = useMutation({
    mutationFn: suitesApi.create,
    onSuccess: (suite) => {
      void client.invalidateQueries({ queryKey: ["suites"] });
      void navigate({ to: "/suites/$suiteId", params: { suiteId: suite.id } });
    },
  });

  if (role !== null && !atLeast(role, "editor")) {
    return <p role="alert">Only editors and admins create suites.</p>;
  }
  return (
    <section aria-labelledby="new-suite-heading" className="space-y-4">
      <h1 id="new-suite-heading" className="text-lg font-semibold">
        New suite
      </h1>
      <FormDataStatus data={data} />
      {data.catalogue.data && data.datasets.data && (
        <SuiteForm
          catalogue={data.catalogue.data}
          datasets={data.datasets.data.items}
          submitLabel="Create suite"
          pending={create.isPending}
          error={create.error}
          onSubmit={(input) => create.mutate(input)}
        />
      )}
    </section>
  );
}
