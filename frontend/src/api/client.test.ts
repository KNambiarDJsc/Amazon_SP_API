import { afterEach, describe, expect, it, vi } from "vitest";
import { json, mockFetch } from "../test-utils";
import { ApiError, api, call, credentials, describeError, downloadFile, verifyKey } from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("describeError", () => {
  it("uses the server's message", () => {
    expect(describeError({ detail: "This job is already finished." }, 409)).toBe(
      "This job is already finished.",
    );
  });

  it("flattens validation errors to field: message", () => {
    const body = {
      detail: [
        { loc: ["body", "settings", "max_asins_per_brand"], msg: "Input should be less than or equal to 10" },
        { loc: ["query", "limit"], msg: "Input should be greater than 0" },
        { msg: "Something general" },
      ],
    };
    expect(describeError(body, 422)).toBe(
      "settings.max_asins_per_brand: Input should be less than or equal to 10; limit: Input should be greater than 0; Something general",
    );
  });

  it.each([
    [401, /not accepted/],
    [429, /Too many requests/],
    [503, /server had a problem/],
    [418, /\(418\)/],
  ])("falls back sensibly for HTTP %i with no body", (status, pattern) => {
    expect(describeError(null, status)).toMatch(pattern);
  });
});

describe("the typed client", () => {
  it("sends the stored key as a bearer token", async () => {
    credentials.set("spk_test_key_value");
    const { calls } = mockFetch({ "GET /api/v1/me": () => json({ name: "A" }) });
    await call(api.GET("/api/v1/me"));
    expect(calls[0]?.headers.get("Authorization")).toBe("Bearer spk_test_key_value");
    credentials.clear();
  });

  it("throws an ApiError carrying the status and the server's words", async () => {
    mockFetch({ "GET /api/v1/jobs": () => json({ detail: "nope" }, 409) });
    const error = await call(api.GET("/api/v1/jobs")).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, message: "nope" });
  });

  it("ends the session when the server rejects the key", async () => {
    credentials.set("spk_expired_key_value");
    mockFetch({ "GET /api/v1/me": () => json({ detail: "Invalid or missing API key." }, 401) });
    await call(api.GET("/api/v1/me")).catch(() => null);
    expect(credentials.get()).toBeNull();
  });
});

describe("verifyKey", () => {
  it("uses the candidate key without storing it", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/me": () =>
        json({ name: "A", marketplace_id: "X", amazon_mode: "mock", connected_to_amazon: false }),
    });
    await verifyKey("spk_candidate_key_123");
    expect(calls[0]?.headers.get("Authorization")).toBe("Bearer spk_candidate_key_123");
    expect(credentials.get()).toBeNull();
  });

  it("does not let a stored key override the candidate", async () => {
    credentials.set("spk_old_key_value_000");
    const { calls } = mockFetch({ "GET /api/v1/me": () => json({}) });
    await verifyKey("spk_new_key_value_111");
    expect(calls[0]?.headers.get("Authorization")).toBe("Bearer spk_new_key_value_111");
    credentials.clear();
  });
});

describe("credentials", () => {
  it("notifies subscribers and survives storage failures", () => {
    const seen = vi.fn();
    const off = credentials.subscribe(seen);
    credentials.set("spk_a_key_of_some_length");
    credentials.clear();
    expect(seen).toHaveBeenCalledTimes(2);
    off();
    credentials.set("spk_a_key_of_some_length");
    expect(seen).toHaveBeenCalledTimes(2);
    credentials.clear();

    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(credentials.get()).toBeNull();
    vi.restoreAllMocks();
  });
});

describe("downloadFile", () => {
  it("authenticates the request and names the file from Content-Disposition", async () => {
    credentials.set("spk_download_key_value");
    const { calls } = mockFetch({
      "GET /api/v1/jobs/abc/export.xlsx": () =>
        new Response("xlsx-bytes", {
          headers: { "Content-Disposition": 'attachment; filename="eligibility-abc12345.xlsx"' },
        }),
    });
    URL.createObjectURL = vi.fn(() => "blob:test");
    URL.revokeObjectURL = vi.fn();
    const clicked: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.download);
    });
    await downloadFile("/api/v1/jobs/abc/export.xlsx", "fallback.xlsx");
    expect(calls[0]?.headers.get("Authorization")).toBe("Bearer spk_download_key_value");
    expect(clicked).toEqual(["eligibility-abc12345.xlsx"]);
    vi.restoreAllMocks();
    credentials.clear();
  });

  it("surfaces the server's refusal instead of saving an error page as a file", async () => {
    mockFetch({ "GET /api/v1/jobs/abc/export.xlsx": () => json({ detail: "too many checks" }, 409) });
    await expect(downloadFile("/api/v1/jobs/abc/export.xlsx", "x.xlsx")).rejects.toMatchObject({
      status: 409,
      message: "too many checks",
    });
  });
});
