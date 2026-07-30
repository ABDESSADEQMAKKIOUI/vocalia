/**
 * Support, documentation and legal contact points for this deployment.
 *
 * Every customer-facing "contact us" / "learn more" / "terms" link in the UI
 * resolves through this module, so a deployment can point them at its own
 * resources through environment variables instead of patching components.
 *
 * WHY DOCS_URL (AND TERMS_URL / PRIVACY_URL) ARE EMPTY BY DEFAULT
 * ---------------------------------------------------------------
 * The Volira documentation and legal pages do not exist yet. A link that 404s
 * is worse than no link at all: it tells the customer the product is broken,
 * and pointing it at the upstream vendor's site instead would send our own
 * customers to a third party. So the default is "no destination", and every
 * call site treats an empty value as "hide the link" — falling back to the
 * support address where the sentence needs a destination. The day the docs go
 * live, set NEXT_PUBLIC_DOCS_URL and every link switches back on with no code
 * change.
 *
 * Next.js inlines NEXT_PUBLIC_* variables at build time, so they must be read
 * as literal `process.env.NEXT_PUBLIC_X` expressions — never through a
 * computed key.
 */

/** Address shown wherever the user needs to reach a human. */
export const SUPPORT_EMAIL =
  process.env.NEXT_PUBLIC_SUPPORT_EMAIL || "support@volira.cfpss.ma";

/** Ready-to-use `href` for the support address. */
export const SUPPORT_MAILTO = `mailto:${SUPPORT_EMAIL}`;

/** Base URL of the documentation site. Empty when there is no docs site. */
export const DOCS_URL = process.env.NEXT_PUBLIC_DOCS_URL || "";

/** Terms of service page. Empty when not published. */
export const TERMS_URL = process.env.NEXT_PUBLIC_TERMS_URL || "";

/** Privacy policy page. Empty when not published. */
export const PRIVACY_URL = process.env.NEXT_PUBLIC_PRIVACY_URL || "";

/**
 * Absolute URL of a documentation page, or `null` when no documentation site
 * is configured. Returning `null` rather than a broken URL lets call sites hide
 * the link with `{url && <a href={url}>…</a>}`.
 *
 * `path` is relative to DOCS_URL and may carry an anchor, e.g.
 * `docsUrl("configurations/api-keys#service-keys")`.
 */
export function docsUrl(path = ""): string | null {
  if (!DOCS_URL) return null;
  const base = DOCS_URL.replace(/\/+$/, "");
  const suffix = path.replace(/^\/+/, "");
  return suffix ? `${base}/${suffix}` : base;
}
