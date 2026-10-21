import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { credentials } from "../api/client";
import { AuthProvider } from "../auth";
import { json, mockFetch, renderApp } from "../test-utils";
import { LoginPage } from "./LoginPage";

afterEach(() => {
  vi.unstubAllGlobals();
  credentials.clear();
});

function page() {
  return renderApp(
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<p>Signed-in home</p>} />
      </Routes>
    </AuthProvider>,
    { route: "/login" },
  );
}

describe("LoginPage", () => {
  it("keeps the key out of storage until the server has accepted it", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    mockFetch({
      "GET /api/v1/me": async () => {
        await gate;
        return json({ name: "A", marketplace_id: "X", amazon_mode: "mock", connected_to_amazon: false });
      },
    });
    page();
    await user.type(screen.getByLabelText("API key"), "spk_a_real_looking_key");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(credentials.get()).toBeNull(); // request still in flight
    release();
    expect(await screen.findByText("Signed-in home")).toBeInTheDocument();
    expect(credentials.get()).toBe("spk_a_real_looking_key");
  });

  it("shows why a rejected key was rejected and stays on the form", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /api/v1/me": () => json({ detail: "Invalid or missing API key." }, 401) });
    page();
    await user.type(screen.getByLabelText("API key"), "spk_wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("not accepted");
    expect(credentials.get()).toBeNull();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
    expect(screen.queryByText("Signed-in home")).toBeNull();
  });

  it("explains an unreachable server instead of claiming the key is wrong", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /api/v1/me": () => json({ detail: "Database unavailable." }, 503) });
    page();
    await user.type(screen.getByLabelText("API key"), "spk_a_real_looking_key");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Database unavailable."));
    expect(screen.getByRole("alert")).not.toHaveTextContent("not accepted");
  });

  it("masks the key as it is typed", () => {
    page();
    expect(screen.getByLabelText("API key")).toHaveAttribute("type", "password");
  });
});
