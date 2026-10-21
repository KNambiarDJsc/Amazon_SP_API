import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, mockFetch, renderApp } from "../test-utils";
import { ManualEvidenceForm } from "./ManualEvidenceForm";

afterEach(() => vi.unstubAllGlobals());

const JOB = "11111111-1111-4111-8111-111111111111";
const CHECK = "22222222-2222-4222-8222-222222222222";
const detail = (status: string) => ({ id: CHECK, status, state: "DONE" });

function setup(route: (body: Record<string, unknown>) => Response = () => json(detail("VERIFIED_LISTABLE"))) {
  const fetchMock = mockFetch({
    [`POST /api/v1/jobs/${JOB}/checks/${CHECK}/manual-evidence`]: async (req) => route(await req.json()),
  });
  renderApp(<ManualEvidenceForm jobId={JOB} checkId={CHECK} asin="B0MOCKAA01" />);
  return fetchMock;
}

describe("ManualEvidenceForm", () => {
  it("offers observations only: there is no way to choose a status", () => {
    setup();
    expect(screen.queryByLabelText(/status/i)).toBeNull();
    expect(screen.queryByRole("combobox", { name: /result|verdict|eligib/i })).toBeNull();
  });

  it("does not prefill the ASIN, so typing it is a real confirmation", () => {
    setup();
    expect(screen.getByLabelText("ASIN shown on the panel")).toHaveValue("");
  });

  it("will not submit until the ASIN looks like an ASIN", async () => {
    const user = userEvent.setup();
    setup();
    const submit = screen.getByRole("button", { name: "Record evidence" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText("ASIN shown on the panel"), "B0MOCK");
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText("ASIN shown on the panel"), "AA01");
    expect(submit).toBeEnabled();
  });

  it("sends exactly what was observed, with an upper-cased ASIN and no status", async () => {
    const user = userEvent.setup();
    const { calls } = setup();
    await user.type(screen.getByLabelText("ASIN shown on the panel"), "b0mockaa01");
    await user.selectOptions(screen.getByLabelText("Condition selected in the panel"), "New");
    await user.type(screen.getByLabelText("Primary button label"), "Sell this product");
    await user.selectOptions(screen.getByLabelText("Primary button state"), "Enabled");
    await user.type(screen.getByLabelText(/Notices on the panel/), "First notice{Enter}{Enter}Second notice");
    await user.click(screen.getByRole("button", { name: "Record evidence" }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(JSON.parse(calls[0]!.body)).toEqual({
      page_asin: "B0MOCKAA01",
      selected_condition: "new_new",
      action_label: "Sell this product",
      action_enabled: true,
      notices: ["First notice", "Second notice"],
      product_unavailable: false,
      note: null,
    });
  });

  it("sends nulls, not guesses, for what was not on the panel", async () => {
    const user = userEvent.setup();
    const { calls } = setup();
    await user.type(screen.getByLabelText("ASIN shown on the panel"), "B0MOCKAA01");
    await user.click(screen.getByRole("checkbox", { name: /unavailable/ }));
    await user.click(screen.getByRole("button", { name: "Record evidence" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(JSON.parse(calls[0]!.body)).toMatchObject({
      selected_condition: null,
      action_label: null,
      action_enabled: null,
      notices: [],
      product_unavailable: true,
    });
  });

  it("reports the status the server derived, and resets the form", async () => {
    const user = userEvent.setup();
    setup(() => json(detail("NEEDS_MANUAL_CHECK")));
    await user.type(screen.getByLabelText("ASIN shown on the panel"), "B0MOCKZZ99");
    await user.click(screen.getByRole("button", { name: "Record evidence" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Needs manual check");
    expect(screen.getByLabelText("ASIN shown on the panel")).toHaveValue("");
  });

  it("shows the server's refusal and keeps what was typed", async () => {
    const user = userEvent.setup();
    setup(() => json({ detail: "This check has not completed yet; wait for the API result first." }, 409));
    await user.type(screen.getByLabelText("ASIN shown on the panel"), "B0MOCKAA01");
    await user.click(screen.getByRole("button", { name: "Record evidence" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/has not completed yet/)).toBeInTheDocument();
    expect(screen.getByLabelText("ASIN shown on the panel")).toHaveValue("B0MOCKAA01");
  });
});
