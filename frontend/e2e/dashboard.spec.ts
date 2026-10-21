import { expect, test } from "@playwright/test";
import { keys, sheetRowCounts, signIn, startJob, waitForCompleted } from "./helpers";

const FIVE_BRANDS = ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy", "essl"]; // "essl" duplicates "eSSL"

test.describe.configure({ mode: "serial" });

test("a wrong API key is refused and a real one signs in", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("API key").fill("spk_definitely-not-a-key");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("not accepted");
  await expect(page).toHaveURL(/\/login$/);

  await page.getByLabel("API key").fill(keys.a);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Eligibility jobs" })).toBeVisible();
  await expect(page.getByText("Demo data (mock)")).toBeVisible();
});

test("a seller submits brands, follows the job to the end and reads ASIN-level findings", async ({
  page,
}) => {
  await signIn(page, keys.a);
  const jobId = await startJob(page, FIVE_BRANDS, "Hardware brands");

  await waitForCompleted(page);
  await expect(page.getByRole("note")).toContainText("not live Amazon results");
  await expect(page.getByText("5 of 6 rows accepted · 1 skipped")).toBeVisible();
  await expect(page.getByText("12 of 12 checks")).toBeVisible();

  // The page opened on "All checks" while the job was still running. When the job finishes the table
  // must catch up with the header on its own, without a reload or a tab change.
  await expect(page.getByRole("tab", { name: /All checks/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("button", { name: /Open evidence for/ })).toHaveCount(12);

  // Verified listable: only ASINs Amazon reported no restrictions for.
  await page.getByRole("tab", { name: /Verified listable/ }).click();
  await expect(page.getByRole("button", { name: /Open evidence for B08PDL14VR/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Open evidence for B08Y9C5H38/ })).toHaveCount(0);
  await expect(page.getByText(/says nothing about the rest of the brand/)).toBeVisible();

  // Restricted: brand-gated and category-gated ASINs, with the reason on the row's badge.
  await page.getByRole("tab", { name: /Restricted/ }).click();
  await expect(page.getByRole("button", { name: /Open evidence for B08Y9C5H38/ })).toBeVisible();
  await expect(page.getByText("Brand approval required").first()).toBeVisible();
  await expect(page.getByText("Other approval required").first()).toBeVisible();

  // All checks: every ASIN checked, whatever its result.
  await page.getByRole("tab", { name: /All checks/ }).click();
  await expect(page.getByRole("button", { name: /Open evidence for/ })).toHaveCount(12);
  await page.getByRole("tab", { name: /Restricted/ }).click();

  // Search narrows the table (and is carried in the URL).
  await page.getByLabel("Search brand, ASIN or product").fill("hair dryer");
  await expect(page.getByRole("button", { name: /Open evidence for/ })).toHaveCount(1);
  await expect(page).toHaveURL(/q=hair/);
  await page.getByLabel("Search brand, ASIN or product").fill("");

  // Evidence drawer: Amazon's own words and the evidence history behind the status.
  await page.getByRole("button", { name: /Open evidence for B08Y9C5H38/ }).click();
  const drawer = page.getByRole("dialog", { name: "Check evidence" });
  await expect(drawer).toContainText("B08Y9C5H38");
  await expect(drawer).toContainText("getListingsRestrictions reported APPROVAL_REQUIRED");
  await expect(drawer).toContainText("Sp api restrictions");
  await expect(drawer).toContainText("You need approval to list in this brand.");
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();

  // Brands: a brand with no catalog match is reported as such, and offered for retry.
  await page.getByRole("tab", { name: /Brands/ }).click();
  await expect(page.getByRole("row", { name: /Oddy/ })).toContainText("No product found");
  await expect(page.getByRole("row", { name: /Havells/ })).toContainText("sampled ASIN");
  await expect(page.getByRole("button", { name: "Retry Oddy" })).toBeVisible();

  // The deep link survives a reload.
  await page.goto(`/jobs/${jobId}?tab=restricted`);
  await expect(page.getByRole("tab", { name: /Restricted/ })).toHaveAttribute("aria-selected", "true");

  // This job has 12 checks, above the direct-download limit: the report is built in the background.
  await page.getByRole("button", { name: "Download Excel report" }).click();
  await expect(page.getByText(/being built in the background/)).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: /Download export from/ }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^eligibility-[0-9a-f]{8}\.xlsx$/);
  const file = test.info().outputPath("report.xlsx");
  await download.saveAs(file);
  expect(sheetRowCounts(file)).toEqual({
    "Verified Listable Products": 6,
    "Restricted Products": 6,
    "Manual Review": 1, // Oddy has no catalog match: a person must look
    "Full Audit History": expect.any(Number),
  });
});

test("a person records what Seller Central showed, and the rules derive the status", async ({ page }) => {
  await signIn(page, keys.a);
  await startJob(page, ["Acme Audio"], "Needs a human");
  await waitForCompleted(page);

  // The restrictions API did not recognise the ASIN, so it is never treated as listable.
  await page.getByRole("tab", { name: /Needs review/ }).click();
  await expect(page.getByText("Needs manual check").first()).toBeVisible();
  await page.getByRole("button", { name: "Review B0MOCKAA01" }).click();

  const drawer = page.getByRole("dialog", { name: "Check evidence" });
  const form = drawer.getByRole("form", { name: "Record evidence from Seller Central" });
  await expect(form).toBeVisible();

  // The form has no status field: only observations.
  await expect(form.getByLabel(/status/i)).toHaveCount(0);

  // Looking at the wrong product proves nothing.
  await form.getByLabel("ASIN shown on the panel").fill("B0MOCKZZ99");
  await form.getByLabel("Condition selected in the panel").selectOption({ label: "New" });
  await form.getByLabel("Primary button label").fill("Sell this product");
  await form.getByLabel("Primary button state").selectOption({ label: "Enabled" });
  await form.getByRole("button", { name: "Record evidence" }).click();
  await expect(form.getByRole("status")).toContainText("Needs manual check");

  // The right product, a selected condition and an enabled primary button do.
  await form.getByLabel("ASIN shown on the panel").fill("b0mockaa01");
  await form.getByLabel("Condition selected in the panel").selectOption({ label: "New" });
  await form.getByLabel("Primary button label").fill("Sell this product");
  await form.getByLabel("Primary button state").selectOption({ label: "Enabled" });
  await form.getByRole("button", { name: "Record evidence" }).click();
  await expect(form.getByRole("status")).toContainText("Listable");

  // Both observations are kept; nothing was overwritten.
  await expect(drawer.getByText("Manual", { exact: true })).toHaveCount(2);

  await page.keyboard.press("Escape");
  await expect(page.getByText("Nothing needs review")).toBeVisible();
  await page.getByRole("tab", { name: /Verified listable/ }).click();
  await expect(page.getByRole("button", { name: /Open evidence for B0MOCKAA01/ })).toBeVisible();

  // A one-check job downloads directly, no background build.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download Excel report" }).click(),
  ]);
  const file = test.info().outputPath("report.xlsx");
  await download.saveAs(file);
  expect(sheetRowCounts(file)).toMatchObject({
    "Verified Listable Products": 1,
    "Restricted Products": 0,
    "Manual Review": 0,
  });
});

test("one seller cannot see another seller's job", async ({ browser }) => {
  const a = await browser.newPage();
  await signIn(a, keys.a);
  const jobId = await startJob(a, ["Kangaro"], "Private to A");
  await waitForCompleted(a);

  const b = await browser.newPage();
  await signIn(b, keys.b);
  await expect(b.getByText("Private to A")).toHaveCount(0);
  await b.goto(`/jobs/${jobId}`);
  await expect(b.getByRole("alert")).toContainText("does not exist, or it belongs to another account");
  await a.close();
  await b.close();
});

test("signing out ends the session", async ({ page }) => {
  await signIn(page, keys.a);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("a server-side refusal is shown to the user, not swallowed", async ({ page }) => {
  await signIn(page, keys.a);
  await page.getByRole("link", { name: "Amazon connection" }).click();
  await expect(page.getByText("Not connected").first()).toBeVisible();
  await page.getByRole("button", { name: "Continue to Seller Central" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
});
