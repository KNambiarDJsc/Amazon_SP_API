import { useState, type FormEvent } from "react";
import { useManualEvidence, type ManualEvidence } from "../api/queries";
import { CONDITIONS, type Condition } from "../lib/conditions";
import { STATUS_INFO } from "../lib/status";
import { Button, ErrorNote, Field, inputClass } from "./ui";

type Enabled = "" | "true" | "false";

/**
 * Records what a person saw on the Seller Central product panel. These are observations only:
 * the status is derived by the server's rules, and the form has no way to choose one.
 * Nothing is prefilled, so the ASIN field is a real confirmation that the reviewer looked at the
 * right panel.
 */
export function ManualEvidenceForm({
  jobId,
  checkId,
  asin,
}: {
  jobId: string;
  checkId: string;
  asin: string;
}) {
  const record = useManualEvidence(jobId, checkId);
  const [pageAsin, setPageAsin] = useState("");
  const [condition, setCondition] = useState<Condition | "">("");
  const [label, setLabel] = useState("");
  const [enabled, setEnabled] = useState<Enabled>("");
  const [notices, setNotices] = useState("");
  const [unavailable, setUnavailable] = useState(false);
  const [note, setNote] = useState("");

  const asinOk = /^[A-Za-z0-9]{10}$/.test(pageAsin.trim());

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!asinOk) return;
    const body: ManualEvidence = {
      page_asin: pageAsin.trim().toUpperCase(),
      selected_condition: condition || null,
      action_label: label.trim() || null,
      action_enabled: enabled === "" ? null : enabled === "true",
      notices: notices
        .split("\n")
        .map((n) => n.trim())
        .filter(Boolean),
      product_unavailable: unavailable,
      note: note.trim() || null,
    };
    const result = await record.mutateAsync(body).catch(() => null);
    if (result) {
      setPageAsin("");
      setCondition("");
      setLabel("");
      setEnabled("");
      setNotices("");
      setUnavailable(false);
      setNote("");
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3" aria-label="Record evidence from Seller Central">
      <p className="text-sm text-slate-600">
        Open ASIN <span className="font-mono font-medium">{asin}</span> in Seller Central and record exactly
        what the product panel shows. Do not click Sell, Apply to sell or Copy listing.
      </p>
      <Field
        label="ASIN shown on the panel"
        htmlFor="ev-asin"
        hint="Type it from the page; the check only counts if it matches."
      >
        <input
          id="ev-asin"
          value={pageAsin}
          onChange={(e) => setPageAsin(e.target.value)}
          className={`${inputClass} font-mono`}
          maxLength={10}
          autoComplete="off"
        />
      </Field>
      <Field label="Condition selected in the panel" htmlFor="ev-condition">
        <select
          id="ev-condition"
          value={condition}
          onChange={(e) => setCondition(e.target.value as Condition | "")}
          className={inputClass}
        >
          <option value="">No condition shown</option>
          {CONDITIONS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Primary button label" htmlFor="ev-label" hint="e.g. Sell this product, Apply to sell">
          <input
            id="ev-label"
            value={label}
            maxLength={60}
            onChange={(e) => setLabel(e.target.value)}
            className={inputClass}
          />
        </Field>
        <Field label="Primary button state" htmlFor="ev-enabled">
          <select
            id="ev-enabled"
            value={enabled}
            onChange={(e) => setEnabled(e.target.value as Enabled)}
            className={inputClass}
          >
            <option value="">No button shown</option>
            <option value="true">Enabled</option>
            <option value="false">Disabled</option>
          </select>
        </Field>
      </div>
      <Field
        label="Notices on the panel"
        htmlFor="ev-notices"
        hint="Exact text, one notice per line. Leave empty if there are none."
      >
        <textarea
          id="ev-notices"
          rows={3}
          value={notices}
          onChange={(e) => setNotices(e.target.value)}
          className={inputClass}
        />
      </Field>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={unavailable} onChange={(e) => setUnavailable(e.target.checked)} />
        The product is unavailable or could not be found
      </label>
      <Field label="Note (optional)" htmlFor="ev-note">
        <input
          id="ev-note"
          value={note}
          maxLength={500}
          onChange={(e) => setNote(e.target.value)}
          className={inputClass}
        />
      </Field>
      {record.error && <ErrorNote error={record.error} />}
      {record.data && (
        <p role="status" className="text-sm text-slate-700">
          Evidence recorded. The rules now classify this check as{" "}
          <strong>{record.data.status ? STATUS_INFO[record.data.status].label : "pending"}</strong>.
        </p>
      )}
      <Button type="submit" variant="primary" disabled={!asinOk || record.isPending}>
        {record.isPending ? "Saving…" : "Record evidence"}
      </Button>
    </form>
  );
}
