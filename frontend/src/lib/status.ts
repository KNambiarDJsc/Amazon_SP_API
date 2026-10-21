import type { Schemas } from "../api/client";

export type EligibilityStatus = Schemas["EligibilityStatus"];
export type JobStatus = Schemas["JobStatus"];

type Tone = "green" | "red" | "amber" | "slate" | "blue" | "violet";

export const STATUS_INFO: Record<EligibilityStatus, { label: string; tone: Tone; help: string }> = {
  VERIFIED_LISTABLE: {
    label: "Listable",
    tone: "green",
    help: "Amazon reported no restrictions for this ASIN and condition.",
  },
  BRAND_APPROVAL_REQUIRED: {
    label: "Brand approval required",
    tone: "red",
    help: "Amazon requires approval to sell this ASIN.",
  },
  OTHER_APPROVAL_REQUIRED: {
    label: "Other approval required",
    tone: "red",
    help: "Amazon requires an approval that is not a brand approval (category or product).",
  },
  NOT_ELIGIBLE: {
    label: "Not eligible",
    tone: "red",
    help: "Amazon does not allow this seller to list this ASIN.",
  },
  NO_PRODUCT_FOUND: {
    label: "No product found",
    tone: "slate",
    help: "The catalog returned no product for this brand.",
  },
  NEEDS_MANUAL_CHECK: {
    label: "Needs manual check",
    tone: "amber",
    help: "The evidence is missing or ambiguous. This is never treated as listable.",
  },
  MIXED_RESULTS: {
    label: "Mixed results",
    tone: "violet",
    help: "Sampled ASINs of this brand gave different answers.",
  },
};

/** The statuses the dashboard groups as "restricted". */
export const RESTRICTED: EligibilityStatus[] = [
  "BRAND_APPROVAL_REQUIRED",
  "OTHER_APPROVAL_REQUIRED",
  "NOT_ELIGIBLE",
];

export const JOB_INFO: Record<JobStatus, { label: string; tone: Tone }> = {
  QUEUED: { label: "Queued", tone: "slate" },
  RUNNING: { label: "Running", tone: "blue" },
  PAUSED: { label: "Paused", tone: "amber" },
  CANCELLED: { label: "Cancelled", tone: "slate" },
  COMPLETED: { label: "Completed", tone: "green" },
};

export const isActive = (s: JobStatus | undefined) => s === "QUEUED" || s === "RUNNING";

export function countOf(by: Record<string, number> | undefined, statuses: EligibilityStatus[]) {
  return statuses.reduce((sum, s) => sum + (by?.[s] ?? 0), 0);
}
