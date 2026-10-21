import { useEvents } from "../api/queries";
import { formatTime, humanize } from "../lib/format";
import { Card, Empty, ErrorNote, Spinner } from "./ui";

export function ActivityList({ jobId }: { jobId: string }) {
  const events = useEvents(jobId);
  if (events.isPending) return <Spinner label="Loading activity" />;
  if (events.isError) return <ErrorNote error={events.error} onRetry={() => events.refetch()} />;
  return (
    <Card>
      {events.data.length === 0 ? (
        <Empty title="No activity yet" />
      ) : (
        <ol className="divide-y divide-slate-100 text-sm">
          {events.data.map((e) => (
            <li key={e.id} className="flex flex-wrap items-baseline gap-x-3 px-4 py-2">
              <span className="w-40 shrink-0 text-slate-500">{formatTime(e.created_at)}</span>
              <span className="font-medium">{humanize(e.event_type)}</span>
              <span className="min-w-0 truncate text-slate-500">{summary(e.payload)}</span>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

function summary(payload: Record<string, unknown>): string {
  return Object.entries(payload)
    .filter(([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean")
    .slice(0, 4)
    .map(([k, v]) => `${k}: ${String(v)}`)
    .join(" · ");
}
