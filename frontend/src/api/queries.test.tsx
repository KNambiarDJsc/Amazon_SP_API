import { renderHook } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { testClient } from "../test-utils";
import { useKeepListsInSync, type Job } from "./queries";

const job = (over: Partial<Job["progress"]> = {}, status: Job["status"] = "RUNNING") =>
  ({
    id: "job-1",
    status,
    progress: {
      checks_total: 12,
      checks_done: 3,
      brands_completed: 1,
      brands_failed: 0,
      needs_manual_check: 0,
      ...over,
    },
  }) as unknown as Job;

function setup(initial: Job) {
  const client = testClient();
  const spy = vi.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(({ j }) => useKeepListsInSync(j), { wrapper, initialProps: { j: initial } });
  return { client, spy, ...hook };
}

describe("useKeepListsInSync", () => {
  it("does nothing on first render", () => {
    const { spy } = setup(job());
    expect(spy).not.toHaveBeenCalled();
  });

  it("refreshes the job's lists when progress moves, and the final state is covered", () => {
    const { spy, rerender } = setup(job());
    rerender({ j: job({ checks_done: 7 }) });
    expect(spy).toHaveBeenCalledTimes(1);
    rerender({ j: job({ checks_done: 12, brands_completed: 5 }, "COMPLETED") });
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("does not refetch when nothing changed", () => {
    const { spy, rerender } = setup(job());
    rerender({ j: job() });
    rerender({ j: job() });
    expect(spy).not.toHaveBeenCalled();
  });

  it("targets this job's lists only, never the job itself or another job", () => {
    const { spy, client, rerender } = setup(job());
    rerender({ j: job({ checks_done: 4 }) });
    const { predicate } = spy.mock.calls[0]![0] as unknown as {
      predicate: (q: { queryKey: unknown[] }) => boolean;
    };
    expect(predicate({ queryKey: ["job", "job-1", "checks", {}] })).toBe(true);
    expect(predicate({ queryKey: ["job", "job-1", "review", 0] })).toBe(true);
    expect(predicate({ queryKey: ["job", "job-1"] })).toBe(false);
    expect(predicate({ queryKey: ["job", "job-2", "checks", {}] })).toBe(false);
    expect(predicate({ queryKey: ["jobs", 0] })).toBe(false);
    client.clear();
  });
});
