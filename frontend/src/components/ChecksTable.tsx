import { PAGE_SIZE, useChecks, type Check, type EligibilityStatus } from "../api/queries";
import { conditionLabel } from "../lib/conditions";
import { formatTime } from "../lib/format";
import { Card, Empty, ErrorNote, Pagination, Spinner, StatusBadge } from "./ui";

export function ChecksTable({
  jobId,
  statuses,
  q,
  page,
  onPage,
  onOpen,
  emptyHint,
}: {
  jobId: string;
  statuses: EligibilityStatus[];
  q: string;
  page: number;
  onPage: (p: number) => void;
  onOpen: (check: Check) => void;
  emptyHint: string;
}) {
  const checks = useChecks(jobId, { statuses, q, page });

  if (checks.isPending) return <Spinner label="Loading checks" />;
  if (checks.isError) return <ErrorNote error={checks.error} onRetry={() => checks.refetch()} />;

  const { items, total } = checks.data;
  return (
    <Card>
      {items.length === 0 ? (
        <Empty title={q ? `Nothing matches “${q}”` : "Nothing here yet"}>
          {q ? "Try a different search." : emptyHint}
        </Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th scope="col" className="px-4 py-2">
                  Brand
                </th>
                <th scope="col" className="px-4 py-2">
                  ASIN
                </th>
                <th scope="col" className="px-4 py-2">
                  Product
                </th>
                <th scope="col" className="px-4 py-2">
                  Condition
                </th>
                <th scope="col" className="px-4 py-2">
                  Result
                </th>
                <th scope="col" className="px-4 py-2">
                  Checked
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((c) => (
                <tr key={c.id} className="hover:bg-slate-50">
                  <td className="px-4 py-2 font-medium">{c.brand}</td>
                  <td className="px-4 py-2">
                    <button
                      className="font-mono text-sky-800 underline-offset-2 hover:underline"
                      onClick={() => onOpen(c)}
                      aria-label={`Open evidence for ${c.asin}, ${conditionLabel(c.condition)}`}
                    >
                      {c.asin}
                    </button>
                  </td>
                  <td className="max-w-xs truncate px-4 py-2 text-slate-600" title={c.title ?? undefined}>
                    {c.title ?? "—"}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2 text-slate-600">
                    {conditionLabel(c.condition)}
                  </td>
                  <td className="px-4 py-2">
                    <StatusBadge status={c.state === "DONE" ? c.status : null} />
                  </td>
                  <td className="whitespace-nowrap px-4 py-2 text-slate-500">{formatTime(c.checked_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pagination page={page} pageSize={PAGE_SIZE} total={total} onPage={onPage} />
    </Card>
  );
}
