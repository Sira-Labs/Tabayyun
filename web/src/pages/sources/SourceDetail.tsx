import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { ApiError } from "../../api";
import { buttonClass } from "../../components/Brand";
import { formatLocalTime } from "../../format";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import {
  TYPE_LABEL,
  isActive,
  sourcesApi,
  unpinnedCertificate,
  type Job,
  type SearchItem,
  type SourceDetail as Source,
} from "../../sources";
import { ErrorLine, dialogs, inputClass, linkButtonClass, panelClass } from "../admin/ui";
import { ConfigFields, CredentialsForm, FieldErrorList, configOf, valuesOf, type FieldValues } from "./forms";
import { HealthBadge, JobBadge, JobLine, useJob } from "./parts";

const CREDENTIAL_TYPES = new Set(["pi_web_api", "opc_ua"]);

/** `/sources/$sourceId`: a connector source with health, actions, settings and history (spec 024). */
export function SourceDetail() {
  const { sourceId } = useParams({ from: "/_app/sources/$sourceId" });
  const role = useWorkspaceRole();
  const source = useQuery({ queryKey: ["sources", sourceId, "detail"], queryFn: () => sourcesApi.get(sourceId) });

  if (source.isPending) return <p>Loading the source…</p>;
  if (source.isError) {
    return (
      <div className="space-y-2">
        <ErrorLine error={source.error} prefix="Could not load the source" />
        <Link to="/sources" className="underline">
          All sources
        </Link>
      </div>
    );
  }
  const s = source.data;
  const isEditor = atLeast(role, "editor");
  const isAdmin = atLeast(role, "admin");
  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <p className="text-sm">
          <Link to="/sources" className="underline">
            Sources
          </Link>
        </p>
        <h1 className="flex flex-wrap items-center gap-2 text-lg font-semibold">
          {s.name} <HealthBadge status={s.health.status} />
          {!s.enabled && <span className="text-sm font-normal text-slate-600 dark:text-slate-400">(disabled)</span>}
        </h1>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          {TYPE_LABEL[s.type] ?? s.type} · {s.n_series} series
          {s.polled_at && ` · last poll ${formatLocalTime(s.polled_at)}`}
        </p>
      </header>
      <HealthPanel source={s} />
      {s.type === "opc_ua" && <OpcUaPanel source={s} isAdmin={isAdmin} />}
      {isEditor && <Actions source={s} />}
      {isAdmin && <Settings source={s} />}
      {isAdmin && CREDENTIAL_TYPES.has(s.type) && <Credentials source={s} />}
      <History sourceId={s.id} />
    </div>
  );
}

function HealthPanel({ source }: { source: Source }) {
  const h = source.health;
  return (
    <section aria-labelledby="health-heading" className={panelClass}>
      <h2 id="health-heading" className="font-semibold">
        Health
      </h2>
      <dl className="mt-2 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
        <dt className="text-slate-600 dark:text-slate-400">Status</dt>
        <dd>{h.status ?? "unknown"}</dd>
        <dt className="text-slate-600 dark:text-slate-400">Last success</dt>
        <dd>{h.last_success_at ? formatLocalTime(h.last_success_at) : "—"}</dd>
        {h.last_error && (
          <>
            <dt className="text-slate-600 dark:text-slate-400">Last error</dt>
            <dd className="text-red-700 dark:text-red-400">
              {h.last_error.message} ({formatLocalTime(h.last_error.at)}{h.last_error.retryable ? ", will retry" : ""})
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}

// OPC UA: pinning the server certificate and the client certificate (spec 023).

function OpcUaPanel({ source, isAdmin }: { source: Source; isAdmin: boolean }) {
  const unpinned = unpinnedCertificate(source.health.last_error?.message);
  const client = useQueryClient();
  const certificate = useQuery({
    queryKey: ["sources", source.id, "client-certificate"],
    queryFn: () => sourcesApi.clientCertificate(source.id),
    enabled: source.credentials.set,
    retry: false,
  });
  const pin = useMutation({
    mutationFn: (sha256: string) =>
      sourcesApi.update(source.id, { config: { ...source.config, server_certificate_sha256: sha256 } }),
    onSuccess: (updated) => client.setQueryData(["sources", source.id, "detail"], updated),
  });
  const pinned = typeof source.config.server_certificate_sha256 === "string";
  const download = () => {
    if (!certificate.data) return;
    const url = URL.createObjectURL(new Blob([certificate.data.certificate_pem], { type: "application/x-pem-file" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `tabayyun-${source.id}.pem`;
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <section aria-labelledby="opcua-heading" className={panelClass}>
      <h2 id="opcua-heading" className="font-semibold">
        Certificates
      </h2>
      <div className="mt-2 space-y-3 text-sm">
        <p data-testid="server-certificate">
          Server certificate:{" "}
          {pinned ? <span className="font-mono text-xs">{String(source.config.server_certificate_sha256)}</span> : "not pinned"}
        </p>
        {unpinned && (
          <div className="rounded border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950">
            <p>
              The server presented <strong>{unpinned.subject}</strong> with SHA-256{" "}
              <span className="break-all font-mono text-xs">{unpinned.sha256}</span>. Compare it with the server's, then pin it.
            </p>
            {isAdmin && (
              <button
                type="button"
                className={`${buttonClass} mt-2`}
                disabled={pin.isPending}
                onClick={() => dialogs.confirm(`Pin the server certificate ${unpinned.sha256}?`) && pin.mutate(unpinned.sha256)}
              >
                Pin this certificate
              </button>
            )}
            <ErrorLine error={pin.error} prefix="Could not pin" />
          </div>
        )}
        {certificate.data && (
          <div className="space-y-1">
            <p>Client certificate (trust it on the server):</p>
            <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-[auto_1fr]">
              <dt className="text-slate-600 dark:text-slate-400">SHA-1</dt>
              <dd className="break-all font-mono text-xs">{certificate.data.sha1}</dd>
              <dt className="text-slate-600 dark:text-slate-400">SHA-256</dt>
              <dd className="break-all font-mono text-xs">{certificate.data.sha256}</dd>
              <dt className="text-slate-600 dark:text-slate-400">Application URI</dt>
              <dd className="break-all">{certificate.data.application_uri ?? "—"}</dd>
              <dt className="text-slate-600 dark:text-slate-400">Valid until</dt>
              <dd>{formatLocalTime(certificate.data.not_after)}</dd>
            </dl>
            <button type="button" className={linkButtonClass} onClick={download}>
              Download certificate (PEM)
            </button>
          </div>
        )}
        {!source.credentials.set && <p>The client certificate is generated when credentials are first set.</p>}
      </div>
    </section>
  );
}

// Actions (editors)

function Actions({ source }: { source: Source }) {
  return (
    <section aria-labelledby="actions-heading" className={`${panelClass} space-y-5`}>
      <h2 id="actions-heading" className="font-semibold">
        Actions
      </h2>
      <CheckAction sourceId={source.id} />
      <FetchAction sourceId={source.id} disabled={!source.enabled} />
      <SearchAction sourceId={source.id} />
      <MetadataAction sourceId={source.id} />
    </section>
  );
}

function CheckAction({ sourceId }: { sourceId: string }) {
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(sourceId, jobId);
  const start = useMutation({ mutationFn: () => sourcesApi.check(sourceId), onSuccess: (r) => setJobId(r.id) });
  return (
    <div className="space-y-1">
      <button type="button" className={buttonClass} disabled={start.isPending} onClick={() => start.mutate()}>
        Check connection
      </button>
      <ErrorLine error={start.error} prefix="Could not start the check" />
      {jobId && <JobLine job={job.data} />}
    </div>
  );
}

/** A datetime-local value as an ISO string in UTC. */
function isoOf(local: string): string {
  return new Date(local).toISOString();
}

function FetchAction({ sourceId, disabled }: { sourceId: string; disabled: boolean }) {
  const [startAt, setStartAt] = useState("");
  const [endAt, setEndAt] = useState("");
  const [force, setForce] = useState(false);
  const [windowError, setWindowError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(sourceId, jobId);
  const start = useMutation({
    mutationFn: () => sourcesApi.fetch(sourceId, { start: isoOf(startAt), end: isoOf(endAt), force }),
    onSuccess: (r) => setJobId(r.id),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const from = Date.parse(startAt);
    const to = Date.parse(endAt);
    if (!Number.isFinite(from) || !Number.isFinite(to)) {
      setWindowError("Enter both From and To.");
      return;
    }
    if (to <= from) {
      setWindowError("To must be later than From.");
      return;
    }
    setWindowError(null);
    start.mutate();
  };
  return (
    <form onSubmit={submit} className="space-y-2" aria-label="Fetch a window">
      <h3 className="text-sm font-semibold">Fetch a window</h3>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          From
          <input required type="datetime-local" className={inputClass} value={startAt} onChange={(e) => setStartAt(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          To
          <input required type="datetime-local" className={inputClass} value={endAt} onChange={(e) => setEndAt(e.target.value)} />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
          Fetch again what is cached
        </label>
        <button type="submit" className={buttonClass} disabled={start.isPending || disabled}>
          Fetch
        </button>
      </div>
      {windowError && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {windowError}
        </p>
      )}
      {disabled && <p className="text-sm text-slate-600 dark:text-slate-400">The source is disabled; enable it to fetch.</p>}
      <ErrorLine error={start.error} prefix="Could not start the fetch" />
      {jobId && <JobLine job={job.data} />}
      {job.data?.result?.point_errors && (
        <ul className="list-inside list-disc text-sm text-red-700 dark:text-red-400">
          {Object.entries(job.data.result.point_errors).map(([id, message]) => (
            <li key={id}>{message}</li>
          ))}
        </ul>
      )}
    </form>
  );
}

function SearchAction({ sourceId }: { sourceId: string }) {
  const client = useQueryClient();
  const [query, setQuery] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const job = useJob(sourceId, jobId);
  const start = useMutation({
    mutationFn: () => sourcesApi.search(sourceId, query.trim()),
    onSuccess: (r) => {
      setPicked(new Set());
      setJobId(r.id);
    },
  });
  const items: SearchItem[] = job.data?.result?.items ?? [];
  const register = useMutation({
    mutationFn: () =>
      sourcesApi.registerSeries(
        sourceId,
        items.filter((i) => picked.has(i.external_id)).map((i) => ({ external_id: i.external_id, name: i.name, unit: i.unit })),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["sources", sourceId, "detail"] });
      void client.invalidateQueries({ queryKey: ["series"] });
    },
  });
  const toggle = (id: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    start.mutate();
  };
  return (
    <div className="space-y-2">
      <form onSubmit={submit} aria-label="Point search" className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Find points
          <input required className={inputClass} placeholder="FIC1 or *.PV" value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
        <button type="submit" className={buttonClass} disabled={start.isPending}>
          Search
        </button>
      </form>
      <ErrorLine error={start.error} prefix="Could not start the search" />
      {jobId && <JobLine job={job.data} />}
      {job.data && !isActive(job.data.status) && job.data.status === "succeeded" && (
        <div className="space-y-2">
          {items.length === 0 ? (
            <p className="text-sm">No points match.</p>
          ) : (
            <fieldset className="space-y-1">
              <legend className="text-sm">
                {items.length} {items.length === 1 ? "match" : "matches"}
                {job.data.result?.truncated && " (more exist; narrow the search)"}
              </legend>
              {items.map((item) => (
                <label key={item.external_id} className="flex items-start gap-2 text-sm">
                  <input type="checkbox" className="mt-1" checked={picked.has(item.external_id)} onChange={() => toggle(item.external_id)} />
                  <span>
                    <span className="font-medium">{item.name}</span>
                    {item.unit && <span className="text-slate-600 dark:text-slate-400"> · {item.unit}</span>}
                    <span className="block break-all font-mono text-xs text-slate-600 dark:text-slate-400">{item.external_id}</span>
                    {item.description && <span className="block text-xs">{item.description}</span>}
                  </span>
                </label>
              ))}
            </fieldset>
          )}
          {items.length > 0 && (
            <button type="button" className={buttonClass} disabled={picked.size === 0 || register.isPending} onClick={() => register.mutate()}>
              Add {picked.size} as series
            </button>
          )}
          <ErrorLine error={register.error} prefix="Could not add the series" />
          {register.data && (
            <p className="text-sm" aria-live="polite">
              Added {register.data.created} new series{register.data.existing > 0 && `, ${register.data.existing} already existed`}.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function MetadataAction({ sourceId }: { sourceId: string }) {
  const client = useQueryClient();
  const [overwrite, setOverwrite] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(sourceId, jobId);
  const start = useMutation({
    mutationFn: () => sourcesApi.importMetadata(sourceId, overwrite),
    onSuccess: (r) => {
      setJobId(r.id);
      void client.invalidateQueries({ queryKey: ["series"] });
    },
  });
  const result = job.data?.result;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className={buttonClass} disabled={start.isPending} onClick={() => start.mutate()}>
          Import metadata
        </button>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />
          Also replace values edited by hand
        </label>
      </div>
      <ErrorLine error={start.error} prefix="Could not start the import" />
      {jobId && <JobLine job={job.data} />}
      {result?.updated !== undefined && (
        <div className="text-sm">
          <p>
            {result.updated} updated, {result.unchanged} unchanged, {result.failed} failed.
          </p>
          <ul className="list-inside list-disc">
            {Object.entries(result.series ?? {})
              .filter(([, change]) => change.error || (change.changed?.length ?? 0) > 0 || (change.skipped?.length ?? 0) > 0)
              .map(([id, change]) => (
                <li key={id}>
                  {change.error ? (
                    <span className="text-red-700 dark:text-red-400">{change.error}</span>
                  ) : (
                    <>
                      {change.changed?.join(", ")}
                      {change.skipped && <span className="text-amber-800 dark:text-amber-300"> (skipped: {change.skipped.join("; ")})</span>}
                    </>
                  )}
                </li>
              ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// Settings and credentials (admins)

function Settings({ source }: { source: Source }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(source.name);
  const [values, setValues] = useState<FieldValues>(() => valuesOf(source.type, source.config));
  const [jsonError, setJsonError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (body: { name?: string; config?: Record<string, unknown>; enabled?: boolean }) => sourcesApi.update(source.id, body),
    onSuccess: (updated) => {
      client.setQueryData(["sources", source.id, "detail"], updated);
      void client.invalidateQueries({ queryKey: ["sources", "list"] });
      setEditing(false);
    },
  });
  const open = () => {
    setName(source.name);
    setValues(valuesOf(source.type, source.config));
    setEditing(true);
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setJsonError(null);
    try {
      save.mutate({ name: name.trim(), config: configOf(source.type, values, source.config) });
    } catch {
      setJsonError("The config is not valid JSON.");
    }
  };
  return (
    <section aria-labelledby="settings-heading" className={panelClass}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="settings-heading" className="font-semibold">
          Settings
        </h2>
        <div className="flex gap-1">
          <button type="button" className={linkButtonClass} disabled={save.isPending} onClick={() => save.mutate({ enabled: !source.enabled })}>
            {source.enabled ? "Disable" : "Enable"}
          </button>
          {!editing && (
            <button type="button" className={linkButtonClass} onClick={open}>
              Edit
            </button>
          )}
        </div>
      </div>
      {editing ? (
        <form onSubmit={submit} className="mt-3 space-y-3" aria-label="Source settings">
          <label className="flex flex-col gap-1 text-sm">
            Name
            <input required className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <ConfigFields type={source.type} values={values} onChange={setValues} />
          {jsonError && (
            <p role="alert" className="text-sm text-red-700 dark:text-red-400">
              {jsonError}
            </p>
          )}
          <div className="flex gap-2">
            <button type="submit" className={buttonClass} disabled={save.isPending}>
              Save
            </button>
            <button type="button" className={linkButtonClass} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <pre className="mt-3 overflow-x-auto rounded bg-slate-50 p-3 text-xs dark:bg-slate-950">{JSON.stringify(source.config, null, 2)}</pre>
      )}
      <ErrorLine error={save.error} prefix="Could not save" />
      <FieldErrorList error={save.error} />
    </section>
  );
}

function Credentials({ source }: { source: Source }) {
  const client = useQueryClient();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["sources", source.id, "detail"] });
    void client.invalidateQueries({ queryKey: ["sources", source.id, "client-certificate"] });
  };
  const save = useMutation({
    mutationFn: (payload: Record<string, unknown>) => sourcesApi.setCredentials(source.id, payload),
    onSuccess: refresh,
  });
  const clear = useMutation({ mutationFn: () => sourcesApi.clearCredentials(source.id), onSuccess: refresh });
  return (
    <section aria-labelledby="credentials-heading" className={`${panelClass} space-y-3`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="credentials-heading" className="font-semibold">
          Credentials
        </h2>
        {source.credentials.set && (
          <button
            type="button"
            className={linkButtonClass}
            disabled={clear.isPending}
            onClick={() => dialogs.confirm("Remove the stored credentials?") && clear.mutate()}
          >
            Remove
          </button>
        )}
      </div>
      <p className="text-sm">
        {source.credentials.set && source.credentials.updated_at
          ? `Set on ${formatLocalTime(source.credentials.updated_at)}. They are never shown; enter them again to replace them.`
          : "Not set."}
      </p>
      <CredentialsForm type={source.type} pending={save.isPending} onSubmit={(payload) => save.mutate(payload)} />
      {save.isSuccess && (
        <p className="text-sm" aria-live="polite">
          Credentials saved.
        </p>
      )}
      <ErrorLine error={save.error} prefix="Could not save the credentials" />
      <FieldErrorList error={save.error} />
      <ErrorLine error={clear.error} prefix="Could not remove the credentials" />
    </section>
  );
}

// History

function History({ sourceId }: { sourceId: string }) {
  const jobs = useQuery({
    queryKey: ["sources", sourceId, "jobs"],
    queryFn: () => sourcesApi.jobs(sourceId),
    refetchInterval: (q) => (q.state.data?.items.some((j) => isActive(j.status)) ? 5000 : false),
  });
  const [open, setOpen] = useState<string | null>(null);
  const items = jobs.data?.items ?? [];
  return (
    <section aria-labelledby="history-heading" className="space-y-2">
      <h2 id="history-heading" className="font-semibold">
        History
      </h2>
      <ErrorLine error={jobs.error} prefix="Could not load the history" />
      {jobs.isSuccess && items.length === 0 && <p className="text-sm text-slate-600 dark:text-slate-400">No jobs yet.</p>}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">The source's latest jobs, newest first</caption>
            <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              <tr>
                <th scope="col" className="p-2">Job</th>
                <th scope="col" className="p-2">Status</th>
                <th scope="col" className="p-2">Started</th>
                <th scope="col" className="hidden p-2 text-right sm:table-cell">Rows</th>
                <th scope="col" className="p-2">
                  <span className="sr-only">Details</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((job) => (
                <JobRow key={job.id} sourceId={sourceId} job={job} open={open === job.id} onToggle={() => setOpen(open === job.id ? null : job.id)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function JobRow({ sourceId, job, open, onToggle }: { sourceId: string; job: Job; open: boolean; onToggle: () => void }) {
  const detail = useQuery({
    queryKey: ["sources", sourceId, "job", job.id],
    queryFn: () => sourcesApi.job(sourceId, job.id),
    enabled: open,
  });
  return (
    <>
      <tr className="border-t border-slate-200 dark:border-slate-800">
        <td className="p-2">{job.trigger}</td>
        <td className="p-2">
          <JobBadge status={job.status} />
          {job.error && <span className="block text-xs text-red-700 dark:text-red-400">{job.error}</span>}
        </td>
        <td className="p-2 text-xs">{formatLocalTime(job.started_at ?? job.created_at)}</td>
        <td className="hidden p-2 text-right tabular-nums sm:table-cell">{job.rows}</td>
        <td className="p-2 text-right">
          <button type="button" className={linkButtonClass} aria-expanded={open} onClick={onToggle}>
            {open ? "Hide" : "Details"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5} className="p-2">
            {detail.isPending && "Loading…"}
            {detail.error instanceof ApiError && <ErrorLine error={detail.error} prefix="Could not load the job" />}
            {detail.data && (
              <pre className="overflow-x-auto rounded bg-slate-50 p-3 text-xs dark:bg-slate-950">
                {JSON.stringify({ params: detail.data.params, result: detail.data.result }, null, 2)}
              </pre>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
