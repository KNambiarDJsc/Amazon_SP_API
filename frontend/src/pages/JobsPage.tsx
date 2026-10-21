import { useState } from "react";
import { Link } from "react-router-dom";
import { PAGE_SIZE, useJobs } from "../api/queries";
import {
  Button,
  Card,
  Empty,
  ErrorNote,
  JobStatusBadge,
  Pagination,
  ProgressBar,
  Spinner,
} from "../components/ui";
import { formatTime } from "../lib/format";

export function JobsPage() {
  const [page, setPage] = useState(0);
  const jobs = useJobs(page);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Eligibility jobs</h1>
        <Link to="/jobs/new">
          <Button variant="primary">New check</Button>
        </Link>
      </div>

      {jobs.isPending && <Spinner label="Loading jobs" />}
      {jobs.isError && <ErrorNote error={jobs.error} onRetry={() => jobs.refetch()} />}

      {jobs.data && (
        <Card>
          {jobs.data.items.length === 0 ? (
            <Empty title="No jobs yet">
              Paste a list of brands or upload a spreadsheet to find out which ASINs you can list.
            </Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-4 py-2">
                      Job
                    </th>
                    <th scope="col" className="px-4 py-2">
                      Status
                    </th>
                    <th scope="col" className="w-56 px-4 py-2">
                      Progress
                    </th>
                    <th scope="col" className="px-4 py-2 text-right">
                      Listable
                    </th>
                    <th scope="col" className="px-4 py-2 text-right">
                      Restricted
                    </th>
                    <th scope="col" className="px-4 py-2 text-right">
                      Needs review
                    </th>
                    <th scope="col" className="px-4 py-2">
                      Created
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {jobs.data.items.map((job) => (
                    <tr key={job.id} className="hover:bg-slate-50">
                      <td className="px-4 py-2">
                        <Link to={`/jobs/${job.id}`} className="font-medium text-sky-800 hover:underline">
                          {job.name ?? job.id.slice(0, 8)}
                        </Link>
                        <div className="text-xs text-slate-500">
                          {job.progress.brands_total} brands
                          {job.data_source === "MOCK" && " · demo data"}
                        </div>
                      </td>
                      <td className="px-4 py-2">
                        <JobStatusBadge status={job.status} />
                      </td>
                      <td className="px-4 py-2">
                        <ProgressBar
                          percent={job.progress.percent_complete}
                          label={`Progress of ${job.name ?? job.id}`}
                        />
                        <div className="mt-1 text-xs text-slate-500">
                          {job.progress.checks_done}/{job.progress.checks_total} checks
                        </div>
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums">{job.progress.verified_listable}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{job.progress.restricted}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{job.progress.needs_manual_check}</td>
                      <td className="px-4 py-2 text-slate-500">{formatTime(job.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Pagination page={page} pageSize={PAGE_SIZE} total={jobs.data.total} onPage={setPage} />
        </Card>
      )}
    </div>
  );
}
