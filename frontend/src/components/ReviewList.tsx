import { PAGE_SIZE, useRetryBrand, useReviewQueue, type ReviewItem } from "../api/queries";
import { conditionLabel } from "../lib/conditions";
import { safeHttpUrl } from "../lib/format";
import { Button, Card, Empty, ErrorNote, Pagination, Spinner, StatusBadge } from "./ui";

function Item({
  jobId,
  item,
  onOpen,
}: {
  jobId: string;
  item: ReviewItem;
  onOpen: (checkId: string) => void;
}) {
  const retry = useRetryBrand(jobId);
  const url = safeHttpUrl(item.seller_central_url);
  return (
    <li className="space-y-2 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{item.brand}</span>
        {item.asin && <span className="font-mono text-sm text-slate-600">{item.asin}</span>}
        {item.condition && <span className="text-sm text-slate-500">· {conditionLabel(item.condition)}</span>}
        <StatusBadge status={item.status} />
      </div>
      {item.title && <p className="text-sm text-slate-600">{item.title}</p>}
      {item.rationale && <p className="text-sm text-slate-700">{item.rationale}</p>}
      <details className="text-sm text-slate-600">
        <summary className="cursor-pointer font-medium text-slate-700">How to resolve this</summary>
        <ol className="mt-1 list-decimal space-y-1 pl-5">
          {item.how_to.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      </details>
      <div className="flex flex-wrap items-center gap-2">
        {item.kind === "CHECK" && item.check_id && (
          <Button
            variant="primary"
            onClick={() => onOpen(item.check_id as string)}
            aria-label={`Review ${item.asin}`}
          >
            Review &amp; record evidence
          </Button>
        )}
        {item.kind === "BRAND" && (
          <Button
            onClick={() => retry.mutate({ brandId: item.brand_id, rediscover: true })}
            disabled={retry.isPending}
          >
            Retry search
          </Button>
        )}
        {url && (
          <a href={url} target="_blank" rel="noopener noreferrer" className="text-sm text-sky-800 underline">
            Open Seller Central
          </a>
        )}
      </div>
      {retry.error && <p className="text-xs text-rose-700">{retry.error.message}</p>}
    </li>
  );
}

export function ReviewList({
  jobId,
  page,
  onPage,
  onOpen,
}: {
  jobId: string;
  page: number;
  onPage: (p: number) => void;
  onOpen: (checkId: string) => void;
}) {
  const queue = useReviewQueue(jobId, page);
  if (queue.isPending) return <Spinner label="Loading review queue" />;
  if (queue.isError) return <ErrorNote error={queue.error} onRetry={() => queue.refetch()} />;
  const { items, total } = queue.data;
  return (
    <Card>
      {items.length === 0 ? (
        <Empty title="Nothing needs review">Everything Amazon could answer has been answered.</Empty>
      ) : (
        <ul className="divide-y divide-slate-100">
          {items.map((item) => (
            <Item
              key={`${item.kind}-${item.check_id ?? item.brand_id}`}
              jobId={jobId}
              item={item}
              onOpen={onOpen}
            />
          ))}
        </ul>
      )}
      <Pagination page={page} pageSize={PAGE_SIZE} total={total} onPage={onPage} />
    </Card>
  );
}
