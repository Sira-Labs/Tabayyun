import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";
import { buttonClass } from "../../components/Brand";
import { formatNumber } from "../../format";
import { bulkMessage, inboxApi, inboxKeys, muteEnd, type Assignee, type FindingChange } from "../../findingsInbox";
import { ErrorLine, inputClass, linkButtonClass, selectClass } from "../admin/ui";
import { Dialog } from "./Dialog";
import { fieldClass, mutedClass } from "./parts";

const MAX_REASON = 500;
const UNASSIGN = "none";
/** A change with the words that say it was done: `12 findings <done>.` */
type Action = { change: FindingChange; done: string };
type Send = (action: Action) => void;
/** A change as it goes out: the ids it was started with, which the selection may leave. */
type Sent = { action: Action; ids: readonly string[] };
/** Reports a bulk change: its result in words, the ids it was sent for and those of them
 * that stay selected, also when the list no longer shows them. */
export type BulkDone = (message: string, sent: readonly string[], keep?: readonly string[]) => void;

const stay = (n: number, one: string, many: string) => `${formatNumber(n)} ${n === 1 ? one : many}`;

/** The actions on the selected findings, for editors (spec 029): acknowledge, mute, resolve,
 * reopen, assign, false positive. Each is one bulk change; `onDone` gets the result in words
 * once the list and the groups have been asked to reload. */
export function BulkActions({
  ids,
  onDone,
  onHold,
}: {
  ids: readonly string[];
  onDone: BulkDone;
  /** Called when a change starts (its ids, running) and when the server refused it (its ids,
   * not running); `onDone` ends every other change. */
  onHold: (ids: readonly string[], running: boolean) => void;
}) {
  const client = useQueryClient();
  const [dialog, setDialog] = useState<"mute" | "resolve" | "assign" | null>(null);
  const bulk = useMutation({
    mutationFn: (sent: Sent) => inboxApi.bulk(sent.ids, sent.action.change),
    onSuccess: (outcome, { action, ids: sent }) => {
      void client.invalidateQueries({ queryKey: inboxKeys.all });
      setDialog(null);
      if (outcome.error === null) {
        onDone(bulkMessage(action.done, outcome), sent);
        return;
      }
      // A request failed. What got no answer was looked up, and the lookup says what a finding
      // is now, not what the request did: one found as asked is said to be so, one that is
      // not (never changed, or changed again by someone since) stays selected, and so does
      // one that could not be looked up either. What was not sent stays selected too.
      const why = outcome.error instanceof Error ? outcome.error.message : String(outcome.error);
      const { found, differing, unchecked, unsent } = outcome;
      const words: string[] = [];
      if (outcome.updated.length + outcome.failed.length > 0) words.push(bulkMessage(action.done, outcome));
      if (found.length > 0) words.push(`${stay(found.length, "finding got no answer and is", "findings got no answer and are")} now ${action.done}.`);
      if (differing.length > 0) {
        words.push(`${stay(differing.length, "got no answer and does not show the change now: it stays", "got no answer and do not show the change now: they stay")} selected.`);
      }
      if (unchecked.length > 0) {
        words.push(`${stay(unchecked.length, "got no answer and could not be checked: it stays", "got no answer and could not be checked: they stay")} selected.`);
      }
      if (unsent.length > 0) words.push(`${stay(unsent.length, "was not sent and stays", "were not sent and stay")} selected.`);
      onDone(`${words.join(" ")} The request failed: ${why}.`, sent, [...differing, ...unchecked, ...unsent]);
    },
    // The server refused the request, so nothing was changed. The list and the groups are
    // reloaded all the same. The sent findings stay held, also when the list no longer shows
    // them: they stay selected, so these actions and the refusal they show stay on the page.
    onError: (_error, sent) => {
      onHold(sent.ids, false);
      void client.invalidateQueries({ queryKey: inboxKeys.all });
    },
  });
  // From here until the result is reported the sent findings stay selected, whatever a
  // reload of the list shows in between.
  const send: Send = (action) => {
    onHold(ids, true);
    bulk.mutate({ action, ids });
  };
  // A request that was sent cannot be taken back: while it runs the dialog stays, so its
  // result is reported in the dialog that started it and no second change starts beside it.
  const close = () => {
    if (bulk.isPending) return;
    setDialog(null);
    bulk.reset();
  };
  const button = (label: string, onClick: () => void) => (
    <button type="button" className={linkButtonClass} disabled={bulk.isPending} onClick={onClick}>
      {label}
    </button>
  );
  const error = <ErrorLine error={bulk.error} prefix="Could not change the findings" />;

  return (
    <div role="group" aria-label="Actions on the selected findings" className="flex flex-wrap items-center gap-x-1 gap-y-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-sm dark:border-slate-800 dark:bg-slate-900">
      <span className="px-2 font-medium">{formatNumber(ids.length)} selected</span>
      {button("Acknowledge", () => send({ change: { status: "acked" }, done: "acknowledged" }))}
      {button("Mute…", () => setDialog("mute"))}
      {button("Resolve…", () => setDialog("resolve"))}
      {button("Reopen", () => send({ change: { status: "open" }, done: "reopened" }))}
      {button("Assign…", () => setDialog("assign"))}
      {button("False positive", () => send({ change: { feedback: "false_positive" }, done: "marked as false positive" }))}
      {button("Remove false positive", () => send({ change: { feedback: null }, done: "no longer marked as false positive" }))}
      {dialog === null && error}
      {dialog === "mute" && <MuteDialog n={ids.length} busy={bulk.isPending} error={error} onSend={send} onClose={close} />}
      {dialog === "resolve" && <ResolveDialog n={ids.length} busy={bulk.isPending} error={error} onSend={send} onClose={close} />}
      {dialog === "assign" && <AssignDialog n={ids.length} busy={bulk.isPending} error={error} onSend={send} onClose={close} />}
    </div>
  );
}

type DialogProps = { n: number; busy: boolean; error: ReactNode; onSend: Send; onClose: () => void };

/** `Mute 3 findings`. */
function title(verb: string, n: number): string {
  return `${verb} ${formatNumber(n)} ${n === 1 ? "finding" : "findings"}`;
}

/** The buttons every dialog ends with. */
function Buttons({ submit, busy, disabled, onClose }: { submit: string; busy: boolean; disabled?: boolean; onClose: () => void }) {
  return (
    <div className="flex justify-end gap-2">
      <button type="button" className={buttonClass} disabled={busy} onClick={onClose}>
        Cancel
      </button>
      <button type="submit" className={buttonClass} disabled={busy || disabled}>
        {submit}
      </button>
    </div>
  );
}

/** Mute: a reason is required; the end is optional and must lie in the future. */
function MuteDialog({ n, busy, error, onSend, onClose }: DialogProps) {
  const [reason, setReason] = useState("");
  const [until, setUntil] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const end = muteEnd(until);
    const why = reason.trim();
    setProblem(why === "" ? "Give a reason: it is what the audit log keeps." : end.error);
    if (why === "" || end.error) return;
    onSend({ change: { status: "muted", reason: why, ...(end.iso ? { muted_until: end.iso } : {}) }, done: "muted" });
  };
  return (
    <Dialog title={title("Mute", n)} onClose={onClose}>
      <form onSubmit={submit} noValidate className="space-y-3">
        <label className={fieldClass}>
          Reason
          <input className={inputClass} value={reason} maxLength={MAX_REASON} required aria-required="true" onChange={(e) => setReason(e.target.value)} />
        </label>
        <label className={fieldClass}>
          Until (optional)
          <input type="datetime-local" className={inputClass} value={until} onChange={(e) => setUntil(e.target.value)} />
          <span className={`text-xs ${mutedClass}`}>Your local time. The findings open again then; without an end they stay muted.</span>
        </label>
        {problem && (
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {problem}
          </p>
        )}
        {error}
        <Buttons submit="Mute" busy={busy} onClose={onClose} />
      </form>
    </Dialog>
  );
}

/** Resolve, with an optional reason. */
function ResolveDialog({ n, busy, error, onSend, onClose }: DialogProps) {
  const [reason, setReason] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const why = reason.trim();
    onSend({ change: { status: "resolved", ...(why ? { reason: why } : {}) }, done: "resolved" });
  };
  return (
    <Dialog title={title("Resolve", n)} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <label className={fieldClass}>
          Reason (optional)
          <input className={inputClass} value={reason} maxLength={MAX_REASON} onChange={(e) => setReason(e.target.value)} />
        </label>
        {error}
        <Buttons submit="Resolve" busy={busy} onClose={onClose} />
      </form>
    </Dialog>
  );
}

/** Assign to one of the workspace's editors, or take the assignment away. */
function AssignDialog({ n, busy, error, onSend, onClose }: DialogProps) {
  const assignees = useQuery({ queryKey: inboxKeys.assignees, queryFn: inboxApi.assignees, staleTime: 60_000 });
  const people: Assignee[] = assignees.data?.items ?? [];
  const [choice, setChoice] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (choice === UNASSIGN) return onSend({ change: { assignee_id: null }, done: "unassigned" });
    const person = people.find((p) => p.id === choice);
    if (person) onSend({ change: { assignee_id: person.id }, done: `assigned to ${person.display_name}` });
  };
  return (
    <Dialog title={title("Assign", n)} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <label className={fieldClass}>
          Assign to
          <select className={selectClass} value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value="" disabled>
              Choose…
            </option>
            {people.map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name}
              </option>
            ))}
            <option value={UNASSIGN}>Unassign</option>
          </select>
        </label>
        <ErrorLine error={assignees.error} prefix="Could not load the people" />
        {error}
        <Buttons submit="Assign" busy={busy} disabled={choice === ""} onClose={onClose} />
      </form>
    </Dialog>
  );
}
