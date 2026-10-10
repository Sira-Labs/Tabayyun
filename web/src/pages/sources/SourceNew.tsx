import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { buttonClass } from "../../components/Brand";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { CONNECTOR_TYPES, TYPE_LABEL, sourcesApi, type ConnectorType } from "../../sources";
import { ErrorLine, inputClass, panelClass, selectClass } from "../admin/ui";
import { ConfigFields, FieldErrorList, configOf, valuesOf, type FieldValues } from "./forms";

const SYNTHETIC_EXAMPLE = { points: [{ external_id: "flow", base: 50, amplitude: 10 }] };

/** `/sources/new`: create a connector source (admins, spec 024); then its page. */
export function SourceNew() {
  const role = useWorkspaceRole();
  const navigate = useNavigate();
  const [type, setType] = useState<ConnectorType>("pi_web_api");
  const [name, setName] = useState("");
  const [values, setValues] = useState<FieldValues>(() => valuesOf("pi_web_api", {}));
  const [jsonError, setJsonError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: (config: Record<string, unknown>) => sourcesApi.create({ type, name: name.trim(), config }),
    onSuccess: (source) => void navigate({ to: "/sources/$sourceId", params: { sourceId: source.id } }),
  });

  const pickType = (next: ConnectorType) => {
    setType(next);
    setValues(valuesOf(next, next === "synthetic" ? SYNTHETIC_EXAMPLE : {}));
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setJsonError(null);
    try {
      create.mutate(configOf(type, values));
    } catch {
      setJsonError("The config is not valid JSON.");
    }
  };

  if (role !== null && !atLeast(role, "admin")) {
    return <p role="alert">Only workspace admins create sources.</p>;
  }
  return (
    <section aria-labelledby="new-source-heading" className="space-y-4">
      <h1 id="new-source-heading" className="text-lg font-semibold">
        New source
      </h1>
      <form onSubmit={submit} className={`${panelClass} space-y-4`}>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-sm">
            Type
            <select className={selectClass} value={type} onChange={(e) => pickType(e.target.value as ConnectorType)}>
              {CONNECTOR_TYPES.map((t) => (
                <option key={t} value={t}>
                  {TYPE_LABEL[t]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Name
            <input required maxLength={200} className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
        </div>
        <ConfigFields type={type} values={values} onChange={setValues} />
        {jsonError && (
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {jsonError}
          </p>
        )}
        <ErrorLine error={create.error} prefix="Could not create the source" />
        <FieldErrorList error={create.error} />
        <button type="submit" className={buttonClass} disabled={create.isPending}>
          Create source
        </button>
      </form>
    </section>
  );
}
