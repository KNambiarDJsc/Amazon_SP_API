import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

export function testClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
}

export function renderApp(ui: ReactElement, { route = "/", client = testClient() } = {}) {
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

type Handler = (request: Request) => Response | Promise<Response>;

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Replace fetch with a table of "METHOD /path" handlers. Anything unlisted fails the test. */
export function mockFetch(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; headers: Headers; body: string }[] = [];
  const stub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request =
      input instanceof Request ? input : new Request(new URL(String(input), "http://localhost"), init);
    const url = new URL(request.url);
    const key = `${request.method} ${url.pathname}`;
    calls.push({
      method: request.method,
      path: url.pathname + url.search,
      headers: request.headers,
      body: await request.clone().text(),
    });
    const handler = routes[key];
    if (!handler) throw new Error(`unexpected request: ${key}`);
    return handler(request);
  });
  vi.stubGlobal("fetch", stub);
  return { calls, stub };
}
