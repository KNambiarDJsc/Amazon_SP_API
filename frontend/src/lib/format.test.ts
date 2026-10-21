import { formatBytes, humanize, parseBrands, safeHttpUrl } from "./format";

describe("parseBrands", () => {
  it("takes one brand per line and drops blanks", () => {
    expect(parseBrands("eSSL\n\n  Havells  \r\nKangaro\n")).toEqual(["eSSL", "Havells", "Kangaro"]);
  });

  it("treats a single line with commas or semicolons as a list", () => {
    expect(parseBrands("eSSL, Havells; Kangaro")).toEqual(["eSSL", "Havells", "Kangaro"]);
  });

  it("keeps commas that are part of a name when there are line breaks", () => {
    expect(parseBrands("Johnson, Smith & Co\nHavells")).toEqual(["Johnson, Smith & Co", "Havells"]);
  });

  it("returns nothing for empty input", () => {
    expect(parseBrands("  \n ")).toEqual([]);
  });
});

describe("safeHttpUrl", () => {
  it("accepts web links", () => {
    expect(safeHttpUrl("https://sellercentral.amazon.in/approvals")).toBe(
      "https://sellercentral.amazon.in/approvals",
    );
    expect(safeHttpUrl("http://example.com/")).toBe("http://example.com/");
  });

  it.each([
    "javascript:alert(1)",
    "data:text/html,<script>1</script>",
    "vbscript:x",
    "//evil.example",
    "not a url",
    "",
    null,
    undefined,
  ])("refuses %s, because links come from Amazon's responses", (value) => {
    expect(safeHttpUrl(value as string | null | undefined)).toBeNull();
  });
});

describe("formatting helpers", () => {
  it("formats sizes", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });

  it("humanizes codes", () => {
    expect(humanize("SP_API_RESTRICTIONS")).toBe("Sp api restrictions");
  });
});
