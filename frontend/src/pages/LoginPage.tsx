import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError, credentials, verifyKey } from "../api/client";
import { useAuth } from "../auth";
import { Button, Card, Field, inputClass } from "../components/ui";

export function LoginPage() {
  const { key } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  if (key) return <Navigate to={from} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const candidate = value.trim();
    if (!candidate) return;
    setBusy(true);
    setError(null);
    try {
      await verifyKey(candidate); // the only way to know a key is good is to use it
      credentials.set(candidate);
      navigate(from, { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? "That API key was not accepted."
          : err instanceof Error
            ? err.message
            : "Could not sign in.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto mt-24 max-w-md px-4">
      <h1 className="mb-1 text-xl font-semibold">Eligibility Intelligence</h1>
      <p className="mb-6 text-sm text-slate-600">Sign in with your seller API key.</p>
      <Card className="p-5">
        <form onSubmit={submit} className="space-y-4">
          <Field
            label="API key"
            htmlFor="api-key"
            hint="Shown once when your account was created. It is kept only for this browser tab."
          >
            <input
              id="api-key"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              className={inputClass}
              placeholder="spk_…"
            />
          </Field>
          {error && (
            <p role="alert" className="text-sm text-rose-700">
              {error}
            </p>
          )}
          <Button type="submit" variant="primary" disabled={busy || !value.trim()} className="w-full">
            {busy ? "Checking…" : "Sign in"}
          </Button>
        </form>
      </Card>
      <p className="mt-4 text-xs text-slate-500">
        This tool reads Amazon's own eligibility answers for your account. It never asks for your Seller
        Central password.
      </p>
    </div>
  );
}
