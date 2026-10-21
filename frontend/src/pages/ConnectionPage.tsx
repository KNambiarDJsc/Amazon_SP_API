import { useState, type FormEvent } from "react";
import { useConnection, useConnectionActions } from "../api/queries";
import { Badge, Button, Card, ErrorNote, Field, Notice, Spinner, inputClass } from "../components/ui";
import { formatTime } from "../lib/format";

export function ConnectionPage() {
  const conn = useConnection();
  const actions = useConnectionActions();
  const [sellerId, setSellerId] = useState("");
  const [token, setToken] = useState("");

  const c = conn.data;

  async function connect(e: FormEvent) {
    e.preventDefault();
    await actions.connect
      .mutateAsync({ seller_id: sellerId.trim(), refresh_token: token.trim() })
      .catch(() => null);
    setToken(""); // the token is never kept in the page once it has been sent
  }

  async function authorize() {
    const r = await actions.authorize.mutateAsync().catch(() => null);
    if (r) window.location.assign(r.authorization_url);
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <h1 className="text-lg font-semibold">Amazon connection</h1>
      <Notice>
        Eligibility answers come from Amazon's Selling Partner API, authorised by you. This app never asks for
        your Seller Central password, and your refresh token is stored encrypted and is never shown again.
      </Notice>

      {conn.isPending && <Spinner />}
      {conn.isError && <ErrorNote error={conn.error} onRetry={() => conn.refetch()} />}

      {c && (
        <Card className="space-y-3 p-5">
          <div className="flex items-center gap-3">
            <h2 className="font-medium">Status</h2>
            {c.connected ? (
              <Badge tone="green">Connected</Badge>
            ) : c.needs_reconnect ? (
              <Badge tone="red">Reconnect needed</Badge>
            ) : (
              <Badge tone="slate">Not connected</Badge>
            )}
          </div>
          <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm">
            <dt className="text-slate-500">Seller ID</dt>
            <dd>{c.seller_id ?? "—"}</dd>
            <dt className="text-slate-500">Authorisation</dt>
            <dd>{c.authorization_type ?? "—"}</dd>
            <dt className="text-slate-500">Last verified</dt>
            <dd>{formatTime(c.last_verified_at)}</dd>
          </dl>
          {c.last_error && (
            <p role="alert" className="text-sm text-rose-700">
              {c.last_error}
            </p>
          )}
          <div className="flex gap-2">
            <Button onClick={() => actions.verify.mutate()} disabled={!c.status || actions.verify.isPending}>
              {actions.verify.isPending ? "Verifying…" : "Verify with Amazon"}
            </Button>
            <Button
              variant="danger"
              onClick={() => actions.disconnect.mutate()}
              disabled={!c.status || actions.disconnect.isPending}
            >
              Disconnect
            </Button>
          </div>
          {actions.verify.error && <ErrorNote error={actions.verify.error} />}
          {actions.disconnect.error && <ErrorNote error={actions.disconnect.error} />}
        </Card>
      )}

      <Card className="space-y-3 p-5">
        <h2 className="font-medium">Authorise with Amazon</h2>
        <p className="text-sm text-slate-600">
          For a public app: you approve access in Seller Central and Amazon sends you back here.
        </p>
        <div>
          <Button variant="primary" onClick={authorize} disabled={actions.authorize.isPending}>
            Continue to Seller Central
          </Button>
        </div>
        {actions.authorize.error && <ErrorNote error={actions.authorize.error} />}
      </Card>

      <Card className="p-5">
        <h2 className="font-medium">Use your own refresh token</h2>
        <p className="mb-3 text-sm text-slate-600">
          For a private app in your own seller account: generate a refresh token in Seller Central (Develop
          Apps → Authorise) and paste it here once.
        </p>
        <form onSubmit={connect} className="space-y-3">
          <Field label="Selling partner ID" htmlFor="seller-id">
            <input
              id="seller-id"
              value={sellerId}
              onChange={(e) => setSellerId(e.target.value)}
              className={inputClass}
              autoComplete="off"
            />
          </Field>
          <Field label="Refresh token" htmlFor="refresh-token">
            <input
              id="refresh-token"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              className={inputClass}
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Button
            type="submit"
            disabled={sellerId.trim().length < 8 || token.trim().length < 20 || actions.connect.isPending}
          >
            {actions.connect.isPending ? "Checking with Amazon…" : "Connect"}
          </Button>
          {actions.connect.error && <ErrorNote error={actions.connect.error} />}
          {actions.connect.isSuccess && (
            <p className="text-sm text-emerald-700">Connected and verified with Amazon.</p>
          )}
        </form>
      </Card>
    </div>
  );
}
