import { useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useCreateJob, useUploadJob, type JobSettings } from "../api/queries";
import { Button, Card, ErrorNote, Field, inputClass } from "../components/ui";
import { CONDITIONS, type Condition } from "../lib/conditions";
import { parseBrands } from "../lib/format";

const MAX_CONDITIONS = 4;

export function NewJobPage() {
  const navigate = useNavigate();
  const create = useCreateJob();
  const upload = useUploadJob();

  const [mode, setMode] = useState<"paste" | "upload">("paste");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [conditions, setConditions] = useState<Condition[]>(["new_new"]);
  const [maxAsins, setMaxAsins] = useState(5);
  const [minSample, setMinSample] = useState(3);

  const brands = useMemo(() => parseBrands(text), [text]);
  const settings: JobSettings = {
    conditions,
    max_asins_per_brand: maxAsins,
    min_sample_size: Math.min(minSample, maxAsins),
    max_catalog_pages: 3,
  };
  const ready = conditions.length > 0 && (mode === "paste" ? brands.length > 0 : file !== null);
  const pending = create.isPending || upload.isPending;
  const error = create.error ?? upload.error;

  function toggle(c: Condition) {
    setConditions((cur) =>
      cur.includes(c) ? cur.filter((x) => x !== c) : cur.length < MAX_CONDITIONS ? [...cur, c] : cur,
    );
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready || pending) return;
    const label = name.trim() || null;
    const job =
      mode === "paste"
        ? await create.mutateAsync({ name: label, brands, settings }).catch(() => null)
        : await upload.mutateAsync({ file: file as File, name: label, settings }).catch(() => null);
    if (job) navigate(`/jobs/${job.id}`);
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <h1 className="text-lg font-semibold">New eligibility check</h1>
      <form onSubmit={submit} className="space-y-6">
        <Card className="space-y-4 p-5">
          <fieldset>
            <legend className="text-sm font-medium text-slate-800">Brands</legend>
            <div
              className="mt-2 inline-flex rounded-md bg-slate-100 p-0.5 text-sm"
              role="radiogroup"
              aria-label="Brand source"
            >
              {(["paste", "upload"] as const).map((m) => (
                <label
                  key={m}
                  className={`cursor-pointer rounded px-3 py-1 font-medium ${mode === m ? "bg-white shadow-sm" : "text-slate-600"}`}
                >
                  <input
                    type="radio"
                    name="mode"
                    value={m}
                    checked={mode === m}
                    onChange={() => setMode(m)}
                    className="sr-only"
                  />
                  {m === "paste" ? "Paste a list" : "Upload a file"}
                </label>
              ))}
            </div>
          </fieldset>

          {mode === "paste" ? (
            <Field
              label="Brand names"
              htmlFor="brands"
              hint="One per line. A single line separated by commas also works."
            >
              <textarea
                id="brands"
                rows={8}
                value={text}
                onChange={(e) => setText(e.target.value)}
                className={`${inputClass} font-mono`}
                placeholder={"eSSL\nHavells\nKangaro"}
              />
              <p className="mt-1 text-xs text-slate-500" aria-live="polite">
                {brands.length} brand{brands.length === 1 ? "" : "s"} detected. Duplicates and blanks are
                reported after upload.
              </p>
            </Field>
          ) : (
            <Field label="Spreadsheet" htmlFor="file" hint=".xlsx or .csv with a column named Brand.">
              <input
                id="file"
                type="file"
                accept=".xlsx,.csv"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="block w-full text-sm file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-1.5 file:text-sm file:font-medium hover:file:bg-slate-200"
              />
            </Field>
          )}

          <Field label="Job name (optional)" htmlFor="name">
            <input
              id="name"
              value={name}
              maxLength={200}
              onChange={(e) => setName(e.target.value)}
              className={inputClass}
            />
          </Field>
        </Card>

        <Card className="space-y-4 p-5">
          <fieldset>
            <legend className="text-sm font-medium text-slate-800">Conditions to check</legend>
            <p className="text-xs text-slate-500">
              Amazon answers per condition. Up to {MAX_CONDITIONS}; every extra condition multiplies the
              number of checks.
            </p>
            <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3">
              {CONDITIONS.map((c) => {
                const checked = conditions.includes(c.value);
                return (
                  <label key={c.value} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!checked && conditions.length >= MAX_CONDITIONS}
                      onChange={() => toggle(c.value)}
                    />
                    {c.label}
                  </label>
                );
              })}
            </div>
          </fieldset>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="ASINs to check per brand"
              htmlFor="max-asins"
              hint="A sample, spread across categories. 1–10."
            >
              <input
                id="max-asins"
                type="number"
                min={1}
                max={10}
                value={maxAsins}
                onChange={(e) => setMaxAsins(clamp(Number(e.target.value), 1, 10))}
                className={inputClass}
              />
            </Field>
            <Field
              label="Minimum sample for a brand result"
              htmlFor="min-sample"
              hint="Below this, a brand is sent to manual review."
            >
              <input
                id="min-sample"
                type="number"
                min={1}
                max={maxAsins}
                value={Math.min(minSample, maxAsins)}
                onChange={(e) => setMinSample(clamp(Number(e.target.value), 1, 10))}
                className={inputClass}
              />
            </Field>
          </div>
        </Card>

        {error && <ErrorNote error={error} />}

        <div className="flex justify-end gap-2">
          <Button type="submit" variant="primary" disabled={!ready || pending}>
            {pending ? "Starting…" : "Start check"}
          </Button>
        </div>
      </form>
    </div>
  );
}

function clamp(n: number, lo: number, hi: number) {
  if (!Number.isFinite(n)) return lo;
  return Math.max(lo, Math.min(hi, Math.round(n)));
}
