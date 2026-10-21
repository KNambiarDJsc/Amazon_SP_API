import { useCallback, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { useJob, useJobAction, useKeepListsInSync, type Job } from "../api/queries";
import { ActivityList } from "../components/ActivityList";
import { BrandsTable } from "../components/BrandsTable";
import { ChecksTable } from "../components/ChecksTable";
import { EvidenceDrawer } from "../components/EvidenceDrawer";
import { ExportPanel } from "../components/ExportPanel";
import { ReviewList } from "../components/ReviewList";
import {
  Button,
  Card,
  ErrorNote,
  JobStatusBadge,
  Notice,
  ProgressBar,
  Spinner,
  inputClass,
} from "../components/ui";
import { formatTime } from "../lib/format";
import { RESTRICTED, countOf, type EligibilityStatus } from "../lib/status";

type TabId = "listable" | "restricted" | "review" | "all" | "brands" | "activity";

const TABS: { id: TabId; label: string }[] = [
  { id: "listable", label: "Verified listable" },
  { id: "restricted", label: "Restricted" },
  { id: "review", label: "Needs review" },
  { id: "all", label: "All checks" },
  { id: "brands", label: "Brands" },
  { id: "activity", label: "Activity" },
];

const STATUS_FILTER: Partial<Record<TabId, EligibilityStatus[]>> = {
  listable: ["VERIFIED_LISTABLE"],
  restricted: RESTRICTED,
};

const HINT: Record<string, string> = {
  listable: "No ASIN has been confirmed listable yet.",
  restricted: "No ASIN has been found restricted yet.",
  all: "No checks have been created yet.",
};

export function JobPage() {
  const { jobId = "" } = useParams();
  const job = useJob(jobId);
  useKeepListsInSync(job.data);
  const [params, setParams] = useSearchParams();

  const tab = (TABS.find((t) => t.id === params.get("tab"))?.id ?? "all") as TabId;
  const q = params.get("q") ?? "";
  const page = Math.max(0, Number(params.get("page") ?? 0) || 0);
  const checkId = params.get("check");

  const update = useCallback(
    (changes: Record<string, string | null>, replace = false) =>
      setParams(
        (cur) => {
          const next = new URLSearchParams(cur);
          for (const [k, v] of Object.entries(changes)) {
            if (v === null || v === "") next.delete(k);
            else next.set(k, v);
          }
          return next;
        },
        { replace },
      ),
    [setParams],
  );

  // The search box is local so typing is instant; the URL (and the query) follow after a pause.
  const [search, setSearch] = useState(q);
  useEffect(() => setSearch(q), [q]);
  useEffect(() => {
    if (search === q) return;
    const t = setTimeout(() => update({ q: search, page: null }, true), 300);
    return () => clearTimeout(t);
  }, [search, q, update]);

  if (job.isPending) return <Spinner label="Loading job" />;
  if (job.isError) {
    const gone = job.error instanceof ApiError && job.error.status === 404;
    return (
      <div className="space-y-3">
        <ErrorNote
          error={gone ? new Error("This job does not exist, or it belongs to another account.") : job.error}
          onRetry={gone ? undefined : () => job.refetch()}
        />
        <Link to="/" className="text-sm text-sky-800 underline">
          Back to jobs
        </Link>
      </div>
    );
  }

  const j = job.data;
  const by = j.progress.by_status;
  const counts: Record<TabId, number | null> = {
    listable: countOf(by, ["VERIFIED_LISTABLE"]),
    restricted: countOf(by, RESTRICTED),
    review: countOf(by, ["NEEDS_MANUAL_CHECK"]),
    all: j.progress.checks_total,
    brands: j.progress.brands_total,
    activity: null,
  };
  const searchable = tab !== "review" && tab !== "activity";

  return (
    <div className="space-y-5">
      <Header job={j} />

      <div className="grid gap-3 sm:grid-cols-3">
        {(["listable", "restricted", "review"] as const).map((id) => (
          <button
            key={id}
            onClick={() => update({ tab: id, page: null, q: null })}
            className="rounded-lg border border-slate-200 bg-white p-4 text-left shadow-sm hover:border-slate-400"
          >
            <div className="text-2xl font-semibold tabular-nums">{counts[id]}</div>
            <div className="text-sm text-slate-600">{TABS.find((t) => t.id === id)?.label}</div>
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="tablist" aria-label="Results" className="flex flex-wrap gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              id={`tab-${t.id}`}
              aria-selected={tab === t.id}
              aria-controls="results-panel"
              onClick={() => update({ tab: t.id, page: null, q: null })}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${tab === t.id ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"}`}
            >
              {t.label}
              {counts[t.id] !== null && (
                <span className="ml-1.5 tabular-nums opacity-70">{counts[t.id]}</span>
              )}
            </button>
          ))}
        </div>
        {searchable && (
          <input
            type="search"
            aria-label="Search brand, ASIN or product"
            placeholder="Search brand, ASIN or product"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className={`${inputClass} max-w-xs`}
          />
        )}
      </div>

      <div id="results-panel" role="tabpanel" aria-labelledby={`tab-${tab}`} className="space-y-2">
        {tab === "listable" && (
          <p className="text-xs text-slate-500">
            “Listable” means Amazon reported no restrictions for that specific ASIN and condition. It says
            nothing about the rest of the brand’s catalog.
          </p>
        )}
        {tab === "brands" ? (
          <BrandsTable jobId={j.id} q={q} page={page} onPage={(p) => update({ page: String(p) })} />
        ) : tab === "review" ? (
          <ReviewList
            jobId={j.id}
            page={page}
            onPage={(p) => update({ page: String(p) })}
            onOpen={(id) => update({ check: id })}
          />
        ) : tab === "activity" ? (
          <ActivityList jobId={j.id} />
        ) : (
          <ChecksTable
            jobId={j.id}
            statuses={STATUS_FILTER[tab] ?? []}
            q={q}
            page={page}
            onPage={(p) => update({ page: String(p) })}
            onOpen={(c) => update({ check: c.id })}
            emptyHint={HINT[tab] ?? ""}
          />
        )}
      </div>

      {checkId && (
        <EvidenceDrawer
          key={checkId}
          jobId={j.id}
          checkId={checkId}
          onClose={() => update({ check: null })}
        />
      )}
    </div>
  );
}

function Header({ job }: { job: Job }) {
  const pause = useJobAction(job.id, "pause");
  const resume = useJobAction(job.id, "resume");
  const cancel = useJobAction(job.id, "cancel");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const p = job.progress;
  const finished = job.status === "COMPLETED" || job.status === "CANCELLED";
  const ingest = job.ingestion_report;
  const skipped = ingest.duplicates.length + ingest.invalid.length;

  return (
    <Card className="space-y-4 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold">{job.name ?? `Job ${job.id.slice(0, 8)}`}</h1>
            <JobStatusBadge status={job.status} />
          </div>
          <p className="text-sm text-slate-500">
            Created {formatTime(job.created_at)}
            {job.finished_at && <> · finished {formatTime(job.finished_at)}</>}
          </p>
        </div>
        <div className="flex gap-2">
          {(job.status === "RUNNING" || job.status === "QUEUED") && (
            <Button onClick={() => pause.mutate()} disabled={pause.isPending}>
              Pause
            </Button>
          )}
          {job.status === "PAUSED" && (
            <Button variant="primary" onClick={() => resume.mutate()} disabled={resume.isPending}>
              Resume
            </Button>
          )}
          {!finished &&
            (confirmCancel ? (
              <>
                <Button
                  variant="danger"
                  onClick={() => {
                    cancel.mutate();
                    setConfirmCancel(false);
                  }}
                >
                  Confirm cancel
                </Button>
                <Button variant="ghost" onClick={() => setConfirmCancel(false)}>
                  Keep running
                </Button>
              </>
            ) : (
              <Button variant="danger" onClick={() => setConfirmCancel(true)}>
                Cancel job
              </Button>
            ))}
        </div>
      </div>

      {job.notice && <Notice tone="amber">{job.notice}</Notice>}
      {job.status === "PAUSED" && job.pause_reason && (
        <Notice tone="amber">
          Paused: {job.pause_reason}{" "}
          <Link to="/amazon" className="font-medium underline">
            Check the Amazon connection
          </Link>
        </Notice>
      )}
      {[pause, resume, cancel].map((m, i) => m.error && <ErrorNote key={i} error={m.error} />)}

      <div className="space-y-1">
        <ProgressBar percent={p.percent_complete} label="Job progress" />
        <p className="text-sm text-slate-600">
          {p.checks_done} of {p.checks_total} checks · {p.brands_completed} of {p.brands_total} brands
          complete
          {p.brands_failed > 0 && <span className="text-rose-700"> · {p.brands_failed} failed</span>}
        </p>
      </div>

      {skipped > 0 && (
        <details className="text-sm text-slate-600">
          <summary className="cursor-pointer">
            {ingest.accepted} of {ingest.total_rows} rows accepted · {skipped} skipped
          </summary>
          <ul className="mt-2 space-y-1 pl-4">
            {ingest.duplicates.map((d) => (
              <li key={`d${d.row}`}>
                Row {d.row} “{d.original_name}” duplicates row {d.duplicate_of_row}
              </li>
            ))}
            {ingest.invalid.map((d) => (
              <li key={`i${d.row}`}>
                Row {d.row} “{d.original_name}”: {d.reason}
              </li>
            ))}
          </ul>
        </details>
      )}

      <ExportPanel job={job} />
    </Card>
  );
}
