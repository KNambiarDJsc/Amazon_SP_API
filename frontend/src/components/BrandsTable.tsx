import { PAGE_SIZE, useBrands, useRetryBrand, type Brand } from "../api/queries";
import { humanize } from "../lib/format";
import { Badge, Button, Card, Empty, ErrorNote, Pagination, Spinner, StatusBadge } from "./ui";

function BrandRow({ jobId, brand }: { jobId: string; brand: Brand }) {
  const retry = useRetryBrand(jobId);
  const s = brand.brand_summary;
  // Offered where a retry could change the answer: failures, and brands nothing was found for or
  // that could not be settled. A settled brand does not need Amazon asked again.
  const canRetry =
    brand.status === "FAILED" ||
    (brand.status === "COMPLETED" &&
      (brand.brand_status === "NO_PRODUCT_FOUND" || brand.brand_status === "NEEDS_MANUAL_CHECK"));
  return (
    <tr className="align-top hover:bg-slate-50">
      <td className="px-4 py-2 font-medium">{brand.display_name}</td>
      <td className="px-4 py-2">
        <Badge tone={brand.status === "FAILED" ? "red" : brand.status === "COMPLETED" ? "green" : "slate"}>
          {humanize(brand.status)}
        </Badge>
      </td>
      <td className="px-4 py-2">{brand.brand_status ? <StatusBadge status={brand.brand_status} /> : "—"}</td>
      <td className="px-4 py-2 text-slate-600">
        {s ? (
          <>
            <p>{s.rationale}</p>
            <p className="mt-1 text-xs text-slate-500">
              Based on {s.asins_checked} sampled ASIN{s.asins_checked === 1 ? "" : "s"} of{" "}
              {s.candidates_brand_matched} brand-matched — not a claim about the whole brand.
            </p>
          </>
        ) : (
          (brand.last_error ?? "—")
        )}
        {brand.status === "FAILED" && brand.last_error && (
          <p className="mt-1 text-xs text-rose-700">{brand.last_error}</p>
        )}
      </td>
      <td className="px-4 py-2 text-right">
        {canRetry && (
          <Button
            onClick={() => retry.mutate({ brandId: brand.id, rediscover: true })}
            disabled={retry.isPending}
            aria-label={`Retry ${brand.display_name}`}
          >
            Retry
          </Button>
        )}
        {retry.error && <p className="mt-1 text-xs text-rose-700">{retry.error.message}</p>}
      </td>
    </tr>
  );
}

export function BrandsTable({
  jobId,
  q,
  page,
  onPage,
}: {
  jobId: string;
  q: string;
  page: number;
  onPage: (p: number) => void;
}) {
  const brands = useBrands(jobId, q, page);
  if (brands.isPending) return <Spinner label="Loading brands" />;
  if (brands.isError) return <ErrorNote error={brands.error} onRetry={() => brands.refetch()} />;
  const { items, total } = brands.data;
  return (
    <Card>
      {items.length === 0 ? (
        <Empty title={q ? `No brand matches “${q}”` : "No brands"} />
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th scope="col" className="px-4 py-2">
                  Brand
                </th>
                <th scope="col" className="px-4 py-2">
                  Progress
                </th>
                <th scope="col" className="px-4 py-2">
                  Result
                </th>
                <th scope="col" className="px-4 py-2">
                  Why
                </th>
                <th scope="col" className="px-4 py-2">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((b) => (
                <BrandRow key={b.id} jobId={jobId} brand={b} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pagination page={page} pageSize={PAGE_SIZE} total={total} onPage={onPage} />
    </Card>
  );
}
