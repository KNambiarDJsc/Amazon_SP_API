import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job } from "../api/queries";
import { credentials } from "../api/client";
import { json, mockFetch, renderApp } from "../test-utils";
import { ExportPanel } from "./ExportPanel";

const JOB_ID = "33333333-3333-4333-8333-333333333333";
const job = (status: Job["status"] = "COMPLETED") => ({ id: JOB_ID, status }) as unknown as Job;
const EXPORT = {
  id: "44444444-4444-4444-8444-444444444444",
  status: "QUEUED",
  requested_at: "2026-10-21T10:00:00Z",
  finished_at: null,
  expires_at: null,
  size_bytes: null,
  checks_count: null,
  error: null,
  download_url: null,
};

beforeEach(() => {
  credentials.set("spk_export_key_value");
  URL.createObjectURL = vi.fn(() => "blob:test");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  credentials.clear();
});

const downloads = () => {
  const names: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    names.push(this.download);
  });
  return names;
};

describe("ExportPanel", () => {
  it("downloads directly when the job is small enough", async () => {
    const user = userEvent.setup();
    const saved = downloads();
    const { calls } = mockFetch({
      [`GET /api/v1/jobs/${JOB_ID}/exports`]: () => json([]),
      [`GET /api/v1/jobs/${JOB_ID}/export.xlsx`]: () =>
        new Response("bytes", {
          headers: { "Content-Disposition": 'attachment; filename="eligibility-33333333.xlsx"' },
        }),
    });
    renderApp(<ExportPanel job={job()} />);
    await user.click(screen.getByRole("button", { name: "Download Excel report" }));
    await waitFor(() => expect(saved).toEqual(["eligibility-33333333.xlsx"]));
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("falls back to a background build when the job is too large, and says so", async () => {
    const user = userEvent.setup();
    downloads();
    const { calls } = mockFetch({
      [`GET /api/v1/jobs/${JOB_ID}/exports`]: () => json([EXPORT]),
      [`GET /api/v1/jobs/${JOB_ID}/export.xlsx`]: () =>
        json({ detail: "This job has 20000 checks, too many to export in one request" }, 409),
      [`POST /api/v1/jobs/${JOB_ID}/exports`]: () => json(EXPORT, 202),
    });
    renderApp(<ExportPanel job={job()} />);
    await user.click(screen.getByRole("button", { name: "Download Excel report" }));
    expect(await screen.findByText(/being built in the background/)).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("does not hide other failures behind the background fallback", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      [`GET /api/v1/jobs/${JOB_ID}/exports`]: () => json([]),
      [`GET /api/v1/jobs/${JOB_ID}/export.xlsx`]: () => json({ detail: "Job not found." }, 404),
    });
    renderApp(<ExportPanel job={job()} />);
    await user.click(screen.getByRole("button", { name: "Download Excel report" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Job not found.");
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("offers a download only for exports that are ready", async () => {
    mockFetch({
      [`GET /api/v1/jobs/${JOB_ID}/exports`]: () =>
        json([
          {
            ...EXPORT,
            id: "a",
            status: "READY",
            size_bytes: 2048,
            checks_count: 12,
            download_url: `/api/v1/jobs/${JOB_ID}/exports/a/download`,
          },
          { ...EXPORT, id: "b", status: "RUNNING" },
          { ...EXPORT, id: "c", status: "FAILED", error: "disk full" },
        ]),
    });
    renderApp(<ExportPanel job={job()} />);
    expect(await screen.findAllByRole("button", { name: /Download export from/ })).toHaveLength(1);
    expect(screen.getByText(/building/)).toBeInTheDocument();
    expect(screen.getByText(/failed: disk full/)).toBeInTheDocument();
  });

  it("warns that a report of an unfinished job is partial", async () => {
    mockFetch({ [`GET /api/v1/jobs/${JOB_ID}/exports`]: () => json([]) });
    renderApp(<ExportPanel job={job("RUNNING")} />);
    expect(await screen.findByText(/only the checks completed so far/)).toBeInTheDocument();
  });
});
