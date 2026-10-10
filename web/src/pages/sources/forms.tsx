// The config and credentials forms of the sources pages (spec 024). Config forms keep the keys
// they do not show; credentials forms are write-only and never filled from the server.
import { useState, type FormEvent, type ReactNode } from "react";
import { ApiError } from "../../api";
import { buttonClass } from "../../components/Brand";
import type { ConnectorType, FieldError } from "../../sources";
import { inputClass, selectClass } from "../admin/ui";

type FieldKind = "text" | "textarea" | "number" | "select" | "checkbox";
type FieldSpec = { key: string; label: string; kind: FieldKind; required?: boolean; options?: string[]; hint?: string };
export type FieldValues = Record<string, string | boolean>;

const POLL = { key: "poll_interval_s", label: "Poll interval (seconds, empty for none)", kind: "number" } as const;

export const CONFIG_FIELDS: Record<Exclude<ConnectorType, "synthetic">, FieldSpec[]> = {
  pi_web_api: [
    { key: "base_url", label: "Base URL", kind: "text", required: true, hint: "https://host/piwebapi" },
    { key: "data_server", label: "Data server", kind: "text", required: true, hint: "\\\\PISRV01" },
    { key: "asset_database", label: "AF database (optional)", kind: "text", hint: "\\\\AFSRV01\\Plant" },
    { key: "ca_pem", label: "Certificate authority (PEM, optional)", kind: "textarea" },
    POLL,
  ],
  opc_ua: [
    { key: "endpoint_url", label: "Endpoint URL", kind: "text", required: true, hint: "opc.tcp://host:4840" },
    {
      key: "security_policy",
      label: "Security policy",
      kind: "select",
      options: ["Basic256Sha256", "Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss", "None"],
    },
    { key: "security_mode", label: "Security mode", kind: "select", options: ["SignAndEncrypt", "Sign"] },
    { key: "allow_insecure", label: "Allow an unencrypted connection (policy None)", kind: "checkbox" },
    { key: "server_certificate_sha256", label: "Server certificate SHA-256", kind: "text" },
    { key: "browse_root", label: "Browse root (optional)", kind: "text", hint: "i=85" },
    POLL,
  ],
};

/** Form values of a config, for the fields of its type. */
export function valuesOf(type: string, config: Record<string, unknown>): FieldValues {
  if (type === "synthetic" || !(type in CONFIG_FIELDS)) return { json: JSON.stringify(config, null, 2) };
  const out: FieldValues = {};
  for (const field of CONFIG_FIELDS[type as keyof typeof CONFIG_FIELDS]) {
    const value = config[field.key];
    if (field.kind === "checkbox") out[field.key] = value === true;
    else if (value !== undefined && value !== null) out[field.key] = String(value);
    else if (field.kind === "select") out[field.key] = field.options?.[0] ?? "";
    else out[field.key] = "";
  }
  return out;
}

/** A config from form values; keys the form does not show are kept from `previous`.
 * Throws a SyntaxError for invalid synthetic JSON. */
export function configOf(type: string, values: FieldValues, previous: Record<string, unknown> = {}): Record<string, unknown> {
  if (type === "synthetic" || !(type in CONFIG_FIELDS)) return JSON.parse(String(values.json ?? "{}")) as Record<string, unknown>;
  const out: Record<string, unknown> = { ...previous };
  for (const field of CONFIG_FIELDS[type as keyof typeof CONFIG_FIELDS]) {
    const value = values[field.key];
    delete out[field.key];
    if (field.kind === "checkbox") {
      if (value === true) out[field.key] = true;
    } else if (typeof value === "string" && value.trim() !== "") {
      out[field.key] = field.kind === "number" ? Number(value) : field.kind === "textarea" ? value : value.trim();
    }
  }
  return out;
}

/** The field errors of an `invalid_config` or `invalid_credentials` answer, as lines. */
export function fieldErrors(error: unknown): string[] {
  if (!(error instanceof ApiError)) return [];
  const errors = (error.body as { errors?: FieldError[] } | null)?.errors;
  return (errors ?? []).map((e) => `${e.loc.filter((p) => p !== "body").join(".") || "config"}: ${e.msg}`);
}

/** A labelled field. */
function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="flex flex-col gap-1 text-sm">{label}{children}</label>;
}

/** The config fields of a type (or the JSON box of a synthetic source), controlled. */
export function ConfigFields({
  type,
  values,
  onChange,
}: {
  type: string;
  values: FieldValues;
  onChange: (values: FieldValues) => void;
}) {
  const set = (key: string, value: string | boolean) => onChange({ ...values, [key]: value });
  if (type === "synthetic" || !(type in CONFIG_FIELDS)) {
    return (
      <Field label="Config (JSON)">
        <textarea
          className={`${inputClass} min-h-40 font-mono text-xs`}
          value={String(values.json ?? "")}
          onChange={(e) => set("json", e.target.value)}
        />
      </Field>
    );
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {CONFIG_FIELDS[type as keyof typeof CONFIG_FIELDS].map((field) => {
        const value = values[field.key];
        if (field.kind === "checkbox") {
          return (
            <label key={field.key} className="flex items-center gap-2 text-sm sm:col-span-2">
              <input type="checkbox" checked={value === true} onChange={(e) => set(field.key, e.target.checked)} />
              {field.label}
            </label>
          );
        }
        if (field.kind === "select") {
          return (
            <Field key={field.key} label={field.label}>
              <select className={selectClass} value={String(value ?? "")} onChange={(e) => set(field.key, e.target.value)}>
                {field.options?.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            </Field>
          );
        }
        if (field.kind === "textarea") {
          return (
            <label key={field.key} className="flex flex-col gap-1 text-sm sm:col-span-2">
              {field.label}
              <textarea
                className={`${inputClass} min-h-24 font-mono text-xs`}
                value={String(value ?? "")}
                onChange={(e) => set(field.key, e.target.value)}
              />
            </label>
          );
        }
        return (
          <Field key={field.key} label={field.label}>
            <input
              className={inputClass}
              required={field.required}
              type={field.kind === "number" ? "number" : "text"}
              placeholder={field.hint}
              value={String(value ?? "")}
              onChange={(e) => set(field.key, e.target.value)}
            />
          </Field>
        );
      })}
    </div>
  );
}

/** A list of the server's field errors under a form. */
export function FieldErrorList({ error }: { error: unknown }) {
  const lines = fieldErrors(error);
  if (lines.length === 0) return null;
  return (
    <ul className="list-inside list-disc text-sm text-red-700 dark:text-red-400">
      {lines.map((line) => (
        <li key={line}>{line}</li>
      ))}
    </ul>
  );
}

type Credentials = Record<string, unknown>;

/** The credentials of a PI Web API or OPC UA source as a write-only form; `onSubmit` gets the
 * payload and resolves when it is stored. */
export function CredentialsForm({
  type,
  onSubmit,
  pending,
}: {
  type: string;
  onSubmit: (payload: Credentials) => void;
  pending: boolean;
}) {
  const [kind, setKind] = useState(type === "opc_ua" ? "anonymous" : "basic");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [ownCert, setOwnCert] = useState(false);
  const [certificate, setCertificate] = useState("");
  const [key, setKey] = useState("");
  const kinds = type === "opc_ua" ? ["anonymous", "username"] : ["basic", "bearer"];

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const payload: Credentials = { kind };
    if (kind === "basic" || kind === "username") Object.assign(payload, { username, password });
    if (kind === "bearer") payload.token = token;
    if (type === "opc_ua" && ownCert) Object.assign(payload, { client_certificate_pem: certificate, client_private_key_pem: key });
    onSubmit(payload);
    setPassword("");
    setToken("");
    setKey("");
  };

  return (
    <form onSubmit={submit} className="space-y-3" aria-label="Credentials">
      <label className="flex flex-col gap-1 text-sm">
        Kind
        <select className={selectClass} value={kind} onChange={(e) => setKind(e.target.value)}>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </label>
      {(kind === "basic" || kind === "username") && (
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-sm">
            Username
            <input required className={inputClass} autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Password
            <input
              required
              type="password"
              className={inputClass}
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
        </div>
      )}
      {kind === "bearer" && (
        <label className="flex flex-col gap-1 text-sm">
          Token
          <input required type="password" className={inputClass} autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
        </label>
      )}
      {type === "opc_ua" && (
        <>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={ownCert} onChange={(e) => setOwnCert(e.target.checked)} />
            Use our own client certificate (otherwise Tabayyun generates one)
          </label>
          {ownCert && (
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="flex flex-col gap-1 text-sm">
                Client certificate (PEM)
                <textarea required className={`${inputClass} min-h-24 font-mono text-xs`} value={certificate} onChange={(e) => setCertificate(e.target.value)} />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                Private key (PEM, unencrypted)
                <textarea required className={`${inputClass} min-h-24 font-mono text-xs`} value={key} onChange={(e) => setKey(e.target.value)} />
              </label>
            </div>
          )}
        </>
      )}
      <button type="submit" className={buttonClass} disabled={pending}>
        Save credentials
      </button>
    </form>
  );
}
