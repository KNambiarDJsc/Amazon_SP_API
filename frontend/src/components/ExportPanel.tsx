import { useState } from "react";
import { ApiError } from "../api/client";
import {
  downloadExport,
  downloadReport,
  useExports,
  useRequestExport,
  type ExportInfo,
  type Job,
} from "../api/queries";
import { formatBytes, formatTime } from "../lib/format";
import { Button, ErrorNote } from "./ui";

function ExportRow({ jobId, e }: { jobId: string; e: ExportInfo }) {
  const [error, setError] = useState<unknown>(null);
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 text-sm">
      <span className="text-slate-600">
        {formatTime(e.requested_at)} ·{" "}
        {e.status === "READY"
          ? `${formatBytes(e.size_bytes)}, ${e.checks_count ?? "?"} checks`
          : e.status === "FAILED"
            ? `failed: ${e.error ?? "unknown error"}`
            : "building…"}
      </span>
      {e.status === "READY" && e.download_url && (
        <Button
          onClick={() => downloadExport(jobId, e.download_url as string).catch(setError)}
          aria-label={`Download export from ${formatTime(e.requested_at)}`}
        >
          Download
        </Button>
      )}
      {error !== null && <ErrorNote error={error} />}
    </li>
  );
}

export function ExportPanel({ job }: { job: Job }) {
  const exports = useExports(job.id);
  const request = useRequestExport(job.id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [queued, setQueued] = useState(false);

  async function exportNow() {
    setBusy(true);
    setError(null);
    setQueued(false);
    try {
      await downloadReport(job.id);
    } catch (err) {
      // 409 means the job is too large to build inside one request: build it in the background.
      if (err instanceof ApiError && err.status === 409) {
        try {
          await request.mutateAsync();
          setQueued(true);
        } catch (e2) {
          setError(e2);
        }
      } else {
        setError(err);
      }
    } finally {
      setBusy(false);
    }
  }

  const rows = exports.data ?? [];
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3">
        <Button variant="primary" onClick={exportNow} disabled={busy || request.isPending}>
          {busy ? "Preparing…" : "Download Excel report"}
        </Button>
        {job.status !== "COMPLETED" && (
          <span className="text-xs text-slate-500">
            The job is not finished: the report covers only the checks completed so far.
          </span>
        )}
      </div>
      {queued && (
        <p role="status" className="text-sm text-slate-700">
          This job is large, so the report is being built in the background. It will appear below when it is
          ready.
        </p>
      )}
      {error !== null && <ErrorNote error={error} />}
      {rows.length > 0 && (
        <ul className="space-y-1 rounded-md border border-slate-200 p-3" aria-label="Background exports">
          {rows.slice(0, 5).map((e) => (
            <ExportRow key={e.id} jobId={job.id} e={e} />
          ))}
        </ul>
      )}
    </div>
  );
}
