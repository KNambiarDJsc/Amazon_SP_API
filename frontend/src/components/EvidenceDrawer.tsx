import { useEffect, useRef, useState } from "react";
import { useCheckDetail, useRecheck, type CheckDetail } from "../api/queries";
import { conditionLabel } from "../lib/conditions";
import { formatTime, humanize, safeHttpUrl } from "../lib/format";
import { Badge, Button, ErrorNote, Spinner, StatusBadge } from "./ui";
import { ManualEvidenceForm } from "./ManualEvidenceForm";

export function EvidenceDrawer({
  jobId,
  checkId,
  onClose,
}: {
  jobId: string;
  checkId: string;
  onClose: () => void;
}) {
  const detail = useCheckDetail(jobId, checkId);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    panel.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-slate-900/30" onClick={onClose} aria-hidden="true" />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label="Check evidence"
        tabIndex={-1}
        className="relative flex h-full w-full max-w-xl flex-col overflow-y-auto bg-white shadow-xl outline-none"
      >
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-slate-200 bg-white px-5 py-3">
          <h2 className="font-semibold">Evidence</h2>
          <Button variant="ghost" onClick={onClose} aria-label="Close evidence">
            Close
          </Button>
        </div>
        <div className="space-y-6 p-5">
          {detail.isPending && <Spinner />}
          {detail.isError && <ErrorNote error={detail.error} onRetry={() => detail.refetch()} />}
          {detail.data && <Body jobId={jobId} d={detail.data} />}
        </div>
      </div>
    </div>
  );
}

function Body({ jobId, d }: { jobId: string; d: CheckDetail }) {
  const recheck = useRecheck(jobId, d.id);
  const done = d.state === "DONE";
  // Open on arrival when a person is needed, then leave it to the user: the status changing after
  // they record evidence must not collapse the form that is showing them the result.
  const [formOpen, setFormOpen] = useState(d.status === "NEEDS_MANUAL_CHECK");
  return (
    <>
      <section className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-lg font-semibold">{d.asin}</span>
          <StatusBadge status={done ? d.status : null} />
        </div>
        <p className="text-sm text-slate-700">{d.title ?? "Untitled product"}</p>
        <p className="text-sm text-slate-500">
          {d.brand} · {conditionLabel(d.condition)} · marketplace {d.marketplace_id}
        </p>
        {d.rationale && <p className="rounded-md bg-slate-50 p-3 text-sm text-slate-800">{d.rationale}</p>}
        {d.rule_id && (
          <p className="text-xs text-slate-500">
            Rule <code>{d.rule_id}</code>
            {d.rules_version && <> · rules {d.rules_version}</>} · checked {formatTime(d.checked_at)}
          </p>
        )}
      </section>

      {(d.reason_codes.length > 0 || d.messages.length > 0 || d.approval_links.length > 0) && (
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">What Amazon said</h3>
          {d.reason_codes.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {d.reason_codes.map((c) => (
                <Badge key={c} tone="slate">
                  {c}
                </Badge>
              ))}
            </div>
          )}
          {d.messages.map((m) => (
            <p key={m} className="text-sm text-slate-700">
              “{m}”
            </p>
          ))}
          {d.approval_links.map((l, i) => {
            const href = safeHttpUrl(l.resource);
            const text = l.title ?? "Approval";
            return href ? (
              <a
                key={i}
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="block text-sm text-sky-800 underline"
              >
                {text}
              </a>
            ) : (
              <p key={i} className="text-sm text-slate-600">
                {text}
              </p>
            );
          })}
        </section>
      )}

      <section className="space-y-1">
        <h3 className="text-sm font-semibold">Catalog match</h3>
        <dl className="grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
          <dt className="text-slate-500">Brand on Amazon</dt>
          <dd>{d.candidate.brand_attribute ?? "—"}</dd>
          <dt className="text-slate-500">Brand match</dt>
          <dd>{humanize(d.candidate.brand_match)}</dd>
          <dt className="text-slate-500">Category</dt>
          <dd>{d.candidate.category_name ?? "—"}</dd>
          <dt className="text-slate-500">Product type</dt>
          <dd>{d.candidate.product_type ?? "—"}</dd>
        </dl>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Evidence history</h3>
        <p className="text-xs text-slate-500">
          Newest last. Earlier evidence is never overwritten; the status comes from combining it.
        </p>
        {d.observations.length === 0 && <p className="text-sm text-slate-500">No evidence recorded yet.</p>}
        <ol className="space-y-2">
          {d.observations.map((o) => (
            <li key={o.id} className="rounded-md border border-slate-200 p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Badge
                  tone={
                    o.source === "SP_API_RESTRICTIONS" ? "blue" : o.source === "MANUAL" ? "violet" : "slate"
                  }
                >
                  {humanize(o.source)}
                </Badge>
                <Badge tone={o.outcome === "OK" ? "green" : "amber"}>{humanize(o.outcome)}</Badge>
                <span className="text-xs text-slate-500">{formatTime(o.observed_at)}</span>
              </div>
              {o.error_detail && <p className="mt-1 text-rose-700">{o.error_detail}</p>}
              {o.signals && Object.keys(o.signals).length > 0 && <Facts data={o.signals} />}
              {o.raw && (
                <details className="mt-1">
                  <summary className="cursor-pointer text-xs text-slate-500">Raw response</summary>
                  <pre className="mt-1 max-h-48 overflow-auto rounded bg-slate-50 p-2 text-xs">
                    {JSON.stringify(o.raw, null, 2)}
                  </pre>
                </details>
              )}
            </li>
          ))}
        </ol>
      </section>

      {done && (
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Ask Amazon again</h3>
          <p className="text-sm text-slate-600">Re-runs the API check and adds the answer to the history.</p>
          <Button onClick={() => recheck.mutate()} disabled={recheck.isPending}>
            {recheck.isPending ? "Asking Amazon…" : "Re-check with Amazon"}
          </Button>
          {recheck.error && <ErrorNote error={recheck.error} />}
        </section>
      )}

      {done && (
        <details
          open={formOpen}
          onToggle={(e) => setFormOpen(e.currentTarget.open)}
          className="rounded-md border border-slate-200 p-3"
        >
          <summary className="cursor-pointer text-sm font-semibold">
            Record evidence from Seller Central
          </summary>
          <div className="mt-3">
            <ManualEvidenceForm jobId={jobId} checkId={d.id} asin={d.asin} />
          </div>
        </details>
      )}
    </>
  );
}

function Facts({ data }: { data: Record<string, unknown> }) {
  const rows = Object.entries(data).filter(
    ([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && v.length === 0),
  );
  if (rows.length === 0) return null;
  return (
    <dl className="mt-1 grid grid-cols-[8rem_minmax(0,1fr)] gap-y-0.5 text-xs">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-slate-500">{humanize(k)}</dt>
          <dd className="[overflow-wrap:anywhere]">
            {typeof v === "object" ? JSON.stringify(v) : String(v)}
          </dd>
        </div>
      ))}
    </dl>
  );
}
