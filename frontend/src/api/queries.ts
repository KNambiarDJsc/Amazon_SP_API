import { useEffect, useRef } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { api, call, downloadFile, uploadJob, type Schemas } from "./client";
import { isActive } from "../lib/status";

export type Job = Schemas["JobOut"];
export type JobSettings = Schemas["JobSettings"];
export type Check = Schemas["CheckOut"];
export type CheckDetail = Schemas["CheckDetailOut"];
export type Brand = Schemas["BrandOut"];
export type ReviewItem = Schemas["ReviewItem"];
export type ManualEvidence = Schemas["ManualEvidenceIn"];
export type ExportInfo = Schemas["ExportOut"];
export type Connection = Schemas["ConnectionStatus"];
export type EligibilityStatus = Schemas["EligibilityStatus"];

export const PAGE_SIZE = 25;
const FAST = 1500; // while a job is running
const SLOW = 6000; // paused: nothing moves on its own, but a resume from another tab should show up

function pollEvery(status: Job["status"] | undefined): number | false {
  if (isActive(status)) return FAST;
  return status === "PAUSED" ? SLOW : false;
}

// ---- reads ------------------------------------------------------------------------------------

export const useMe = (enabled = true) =>
  useQuery({
    queryKey: ["me"],
    queryFn: () => call(api.GET("/api/v1/me")),
    enabled,
    retry: false,
    staleTime: 30_000,
  });

export const useJobs = (page: number) =>
  useQuery({
    queryKey: ["jobs", page],
    queryFn: () =>
      call(api.GET("/api/v1/jobs", { params: { query: { limit: PAGE_SIZE, offset: page * PAGE_SIZE } } })),
    placeholderData: keepPreviousData,
    refetchInterval: (q) =>
      q.state.data?.items.some((j) => isActive(j.status) || j.status === "PAUSED") ? SLOW / 2 : false,
  });

export const useJob = (jobId: string) =>
  useQuery({
    queryKey: ["job", jobId],
    queryFn: () => call(api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: jobId } } })),
    refetchInterval: (q) => pollEvery(q.state.data?.status),
  });

/**
 * Everything under a job (checks, brands, review queue, activity) is refetched when the job's
 * progress changes, not on a timer of its own. A timer can fire just before the last results are
 * written and then stop for good once the job ends, leaving a table that disagrees with its header.
 */
const progressSignature = (j: Job) =>
  [
    j.status,
    j.progress.checks_total,
    j.progress.checks_done,
    j.progress.brands_completed,
    j.progress.brands_failed,
    j.progress.needs_manual_check,
  ].join(":");

export function useKeepListsInSync(job: Job | undefined) {
  const qc = useQueryClient();
  const jobId = job?.id;
  const signature = job ? progressSignature(job) : null;
  const previous = useRef<string | null>(null);
  useEffect(() => {
    if (!jobId || signature === null) return;
    if (previous.current !== null && previous.current !== signature) {
      void qc.invalidateQueries({
        predicate: (q) => q.queryKey[0] === "job" && q.queryKey[1] === jobId && q.queryKey.length > 2,
      });
    }
    previous.current = signature;
  }, [qc, jobId, signature]);
}

export type CheckFilters = {
  statuses: EligibilityStatus[];
  q: string;
  page: number;
  state?: "PENDING" | "DONE";
};

export function useChecks(jobId: string, f: CheckFilters) {
  return useQuery({
    queryKey: ["job", jobId, "checks", f],
    queryFn: () =>
      call(
        api.GET("/api/v1/jobs/{job_id}/checks", {
          params: {
            path: { job_id: jobId },
            query: {
              status: f.statuses.length ? f.statuses : undefined,
              state: f.state,
              q: f.q || undefined,
              limit: PAGE_SIZE,
              offset: f.page * PAGE_SIZE,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

export function useBrands(jobId: string, q: string, page: number) {
  return useQuery({
    queryKey: ["job", jobId, "brands", q, page],
    queryFn: () =>
      call(
        api.GET("/api/v1/jobs/{job_id}/brands", {
          params: {
            path: { job_id: jobId },
            query: { q: q || undefined, limit: PAGE_SIZE, offset: page * PAGE_SIZE },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

export function useReviewQueue(jobId: string, page: number) {
  return useQuery({
    queryKey: ["job", jobId, "review", page],
    queryFn: () =>
      call(
        api.GET("/api/v1/jobs/{job_id}/review-queue", {
          params: { path: { job_id: jobId }, query: { limit: PAGE_SIZE, offset: page * PAGE_SIZE } },
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

export function useEvents(jobId: string) {
  return useQuery({
    queryKey: ["job", jobId, "events"],
    queryFn: () =>
      call(
        api.GET("/api/v1/jobs/{job_id}/events", {
          params: { path: { job_id: jobId }, query: { limit: 100 } },
        }),
      ),
  });
}

export const useCheckDetail = (jobId: string, checkId: string | null) =>
  useQuery({
    queryKey: ["job", jobId, "check", checkId],
    queryFn: () =>
      call(
        api.GET("/api/v1/jobs/{job_id}/checks/{check_id}", {
          params: { path: { job_id: jobId, check_id: checkId as string } },
        }),
      ),
    enabled: checkId !== null,
  });

export const useCandidates = (jobId: string, brandId: string | null) =>
  useQuery({
    queryKey: ["job", jobId, "candidates", brandId],
    queryFn: () =>
      call(
        api.GET("/api/v1/jobs/{job_id}/brands/{brand_id}/candidates", {
          params: { path: { job_id: jobId, brand_id: brandId as string } },
        }),
      ),
    enabled: brandId !== null,
  });

export const useExports = (jobId: string) =>
  useQuery({
    queryKey: ["job", jobId, "exports"],
    queryFn: () => call(api.GET("/api/v1/jobs/{job_id}/exports", { params: { path: { job_id: jobId } } })),
    // Poll only while a build is outstanding.
    refetchInterval: (q) =>
      q.state.data?.some((e) => e.status === "QUEUED" || e.status === "RUNNING") ? 1500 : false,
  });

export const useConnection = () =>
  useQuery({
    queryKey: ["connection"],
    queryFn: () => call(api.GET("/api/v1/amazon/connection")),
  });

// ---- writes -----------------------------------------------------------------------------------

/** Anything that changes a job can change every list under it, so refresh them together. */
function refreshJob(qc: QueryClient, jobId: string) {
  return Promise.all([
    qc.invalidateQueries({ queryKey: ["job", jobId] }),
    qc.invalidateQueries({ queryKey: ["jobs"] }),
  ]);
}

export function useCreateJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { name: string | null; brands: string[]; settings: JobSettings }) =>
      call(
        api.POST("/api/v1/jobs", {
          body: { name: v.name, brands: v.brands, settings: v.settings },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

export function useUploadJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { file: File; name: string | null; settings: JobSettings }) =>
      uploadJob(v.file, v.name, v.settings),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

export function useJobAction(jobId: string, action: "pause" | "resume" | "cancel") {
  const qc = useQueryClient();
  const path = `/api/v1/jobs/{job_id}/${action}` as const;
  return useMutation({
    mutationFn: () => call(api.POST(path, { params: { path: { job_id: jobId } } })),
    onSuccess: () => refreshJob(qc, jobId),
  });
}

export function useRetryBrand(jobId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { brandId: string; rediscover: boolean }) =>
      call(
        api.POST("/api/v1/jobs/{job_id}/brands/{brand_id}/retry", {
          params: { path: { job_id: jobId, brand_id: v.brandId } },
          body: { rediscover: v.rediscover },
        }),
      ),
    onSuccess: () => refreshJob(qc, jobId),
  });
}

export function useRecheck(jobId: string, checkId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      call(
        api.POST("/api/v1/jobs/{job_id}/checks/{check_id}/recheck", {
          params: { path: { job_id: jobId, check_id: checkId } },
        }),
      ),
    onSuccess: () => refreshJob(qc, jobId),
  });
}

export function useManualEvidence(jobId: string, checkId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ManualEvidence) =>
      call(
        api.POST("/api/v1/jobs/{job_id}/checks/{check_id}/manual-evidence", {
          params: { path: { job_id: jobId, check_id: checkId } },
          body,
        }),
      ),
    onSuccess: () => refreshJob(qc, jobId),
  });
}

export function useRequestExport(jobId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      call(api.POST("/api/v1/jobs/{job_id}/exports", { params: { path: { job_id: jobId } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["job", jobId, "exports"] }),
  });
}

export const downloadExport = (jobId: string, url: string) =>
  downloadFile(url, `eligibility-${jobId.slice(0, 8)}.xlsx`);

export const downloadReport = (jobId: string) =>
  downloadFile(`/api/v1/jobs/${jobId}/export.xlsx`, `eligibility-${jobId.slice(0, 8)}.xlsx`);

export function useConnectionActions() {
  const qc = useQueryClient();
  const refresh = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ["connection"] }),
      qc.invalidateQueries({ queryKey: ["me"] }),
    ]);
  return {
    connect: useMutation({
      mutationFn: (v: { seller_id: string; refresh_token: string }) =>
        call(api.PUT("/api/v1/amazon/connection", { body: v })),
      onSuccess: refresh,
    }),
    verify: useMutation({
      mutationFn: () => call(api.POST("/api/v1/amazon/connection/verify")),
      onSuccess: refresh,
    }),
    authorize: useMutation({ mutationFn: () => call(api.POST("/api/v1/amazon/authorize")) }),
    disconnect: useMutation({
      mutationFn: () => call(api.DELETE("/api/v1/amazon/connection")),
      onSuccess: refresh,
    }),
  };
}
