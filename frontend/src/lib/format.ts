export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function humanize(code: string): string {
  const text = code.replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * One brand per line. A single line with commas or semicolons is treated as a list, because that
 * is how people paste; when there are line breaks, commas stay part of the name.
 */
export function parseBrands(text: string): string[] {
  const separator = /[\r\n]/.test(text.trim()) ? /[\r\n]+/ : /[,;]+/;
  return text
    .split(separator)
    .map((b) => b.trim())
    .filter(Boolean);
}

/** Links come from Amazon's responses; only plain web links are ever rendered as links. */
export function safeHttpUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}
