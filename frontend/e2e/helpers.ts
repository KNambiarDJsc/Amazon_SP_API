import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page } from "@playwright/test";

const ROOT = resolve(import.meta.dirname, "../..");

export const keys: { a: string; b: string } = JSON.parse(
  readFileSync(resolve(import.meta.dirname, ".state/keys.json"), "utf8"),
);

export async function signIn(page: Page, key: string) {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel("API key").fill(key);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Eligibility jobs" })).toBeVisible();
}

/** Paste brands into the New check form and start the job; resolves on the job page. */
export async function startJob(page: Page, brands: string[], name: string) {
  await page.getByRole("link", { name: "New check" }).first().click();
  await page.getByLabel("Brand names").fill(brands.join("\n"));
  await page.getByLabel("Job name (optional)").fill(name);
  await page.getByRole("button", { name: "Start check" }).click();
  await expect(page).toHaveURL(/\/jobs\/[0-9a-f-]{36}/);
  return page.url().split("/jobs/")[1]!.split("?")[0]!;
}

export async function waitForCompleted(page: Page) {
  await expect(page.getByText("Completed", { exact: true }).first()).toBeVisible({ timeout: 45_000 });
}

/** Read an .xlsx with openpyxl (the same library that wrote it): {sheet name: data rows}. */
export function sheetRowCounts(path: string): Record<string, number> {
  const python = process.env.E2E_PYTHON ?? resolve(ROOT, ".venv/bin/python");
  const out = execFileSync(
    python,
    [
      "-I",
      "-c",
      "import json,sys;from openpyxl import load_workbook;" +
        "wb=load_workbook(sys.argv[1]);" +
        "print(json.dumps({ws.title:ws.max_row-1 for ws in wb}))",
      path,
    ],
    { encoding: "utf8" },
  );
  return JSON.parse(out) as Record<string, number>;
}
