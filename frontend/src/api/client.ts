import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

// ---- credentials ------------------------------------------------------------------------------
// The seller's API key lives in sessionStorage: it dies with the tab and is not shared across tabs
// or restarts. localStorage would survive on shared machines; nothing here needs that.

const KEY = "eligibility.apiKey";
const listeners = new Set<() => void>();

function read(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export const credentials = {
  get: read,
  set(key: string) {
    try {
      sessionStorage.setItem(KEY, key);
    } catch {
      /* private mode: the key stays unset and login will say so */
    }
    listeners.forEach((fn) => fn());
  },
  clear() {
    try {
      sessionStorage.removeItem(KEY);
    } catch {
      /* nothing to clear */
    }
    listeners.forEach((fn) => fn());
  },
  subscribe(fn: () => void) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
};

// ---- errors -----------------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type ValidationItem = { loc?: (string | number)[]; msg?: string };

/** FastAPI errors are `{detail: string}` or, for validation, `{detail: [{loc, msg}]}`. */
export function describeError(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return (detail as ValidationItem[])
      .map((d) => {
        const where = (d.loc ?? []).filter((p) => p !== "body" && p !== "query").join(".");
        return where ? `${where}: ${d.msg ?? "invalid"}` : (d.msg ?? "invalid");
      })
      .join("; ");
  }
  if (status === 401) return "The API key was not accepted.";
  if (status === 429) return "Too many requests. Wait a moment and try again.";
  if (status >= 500) return "The server had a problem. Try again shortly.";
  return `Request failed (${status}).`;
}

// ---- typed client -----------------------------------------------------------------------------

const auth: Middleware = {
  onRequest({ request }) {
    const key = read();
    if (key && !request.headers.has("Authorization")) {
      request.headers.set("Authorization", `Bearer ${key}`);
    }
    return request;
  },
  onResponse({ response }) {
    // A rejected key means the session is over, wherever in the app it happened.
    if (response.status === 401) credentials.clear();
    return response;
  },
};

// `fetch` is looked up per call (not captured at import) so tests can replace it; the origin keeps
// URLs absolute, which non-browser runtimes require and browsers accept.
export const api = createClient<paths>({
  baseUrl: globalThis.location?.origin ?? "",
  fetch: (request) => globalThis.fetch(request),
});
api.use(auth);

type Result<T> = { data?: T; error?: unknown; response: Response };

/** Unwrap an openapi-fetch result: return the data or throw an ApiError. */
export async function call<T>(result: Promise<Result<T>>): Promise<T> {
  const { data, error, response } = await result;
  if (error !== undefined || !response.ok) {
    throw new ApiError(describeError(error, response.status), response.status);
  }
  return data as T;
}

/**
 * Check a key by using it, without storing it: the app only treats the user as signed in once the
 * server has accepted the key.
 */
export async function verifyKey(candidate: string): Promise<Schemas["MeOut"]> {
  return call(api.GET("/api/v1/me", { headers: { Authorization: `Bearer ${candidate}` } }));
}

// ---- plain fetches (multipart upload and file download) --------------------------------------

async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const key = read();
  if (key) headers.set("Authorization", `Bearer ${key}`);
  const response = await fetch(path, { ...init, headers });
  if (response.status === 401) credentials.clear();
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      /* not JSON */
    }
    throw new ApiError(describeError(body, response.status), response.status);
  }
  return response;
}

export async function uploadJob(
  file: File,
  name: string | null,
  settings: Schemas["JobSettings"],
): Promise<Schemas["JobOut"]> {
  const form = new FormData();
  form.set("file", file);
  if (name) form.set("name", name);
  form.set("settings", JSON.stringify(settings));
  const response = await authFetch("/api/v1/jobs/upload", { method: "POST", body: form });
  return (await response.json()) as Schemas["JobOut"];
}

/** Fetch an authenticated file and hand it to the browser as a download. */
export async function downloadFile(path: string, fallbackName: string): Promise<void> {
  const response = await authFetch(path);
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const name = /filename="?([^";]+)"?/.exec(disposition)?.[1] ?? fallbackName;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
