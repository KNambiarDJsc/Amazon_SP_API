import { RESTRICTED, STATUS_INFO, countOf, isActive } from "./status";

describe("status metadata", () => {
  it("describes every status the API can return", () => {
    expect(Object.keys(STATUS_INFO).sort()).toEqual(
      [
        "BRAND_APPROVAL_REQUIRED",
        "MIXED_RESULTS",
        "NEEDS_MANUAL_CHECK",
        "NOT_ELIGIBLE",
        "NO_PRODUCT_FOUND",
        "OTHER_APPROVAL_REQUIRED",
        "VERIFIED_LISTABLE",
      ].sort(),
    );
  });

  it("never presents uncertainty as a green result", () => {
    expect(STATUS_INFO.NEEDS_MANUAL_CHECK.tone).not.toBe("green");
    expect(STATUS_INFO.NO_PRODUCT_FOUND.tone).not.toBe("green");
    expect(STATUS_INFO.MIXED_RESULTS.tone).not.toBe("green");
    expect(STATUS_INFO.VERIFIED_LISTABLE.tone).toBe("green");
  });

  it("groups the three blocking statuses as restricted", () => {
    expect([...RESTRICTED].sort()).toEqual([
      "BRAND_APPROVAL_REQUIRED",
      "NOT_ELIGIBLE",
      "OTHER_APPROVAL_REQUIRED",
    ]);
  });

  it("sums counts and tolerates missing keys", () => {
    expect(countOf({ BRAND_APPROVAL_REQUIRED: 3, NOT_ELIGIBLE: 1, VERIFIED_LISTABLE: 9 }, RESTRICTED)).toBe(
      4,
    );
    expect(countOf(undefined, RESTRICTED)).toBe(0);
  });

  it("knows which jobs are moving", () => {
    expect(isActive("RUNNING")).toBe(true);
    expect(isActive("QUEUED")).toBe(true);
    expect(isActive("PAUSED")).toBe(false);
    expect(isActive("COMPLETED")).toBe(false);
  });
});
