/**
 * Display names for Google scopes and tool families.
 *
 * Only the wording lives here — never the list itself. Which scopes a family
 * needs is a server decision (`GET /integrations/google/oauth/required-scopes`);
 * a table of scopes hardcoded in the browser is exactly the drift that endpoint
 * exists to prevent. An unknown scope still renders: it falls back to its own
 * last path segment rather than disappearing from a consent summary.
 */

export const FAMILY_ORDER = ["sheets", "calendar", "drive", "gmail", "docs"] as const;

export type GoogleFamily = (typeof FAMILY_ORDER)[number];

export const FAMILY_LABELS: Record<string, string> = {
    sheets: "Google Sheets",
    calendar: "Google Calendar",
    drive: "Google Drive",
    gmail: "Gmail",
    docs: "Google Docs",
    all: "All Google services",
};

const SCOPE_LABELS: Record<string, string> = {
    "https://www.googleapis.com/auth/spreadsheets": "Read and write spreadsheets",
    "https://www.googleapis.com/auth/spreadsheets.readonly": "Read spreadsheets",
    "https://www.googleapis.com/auth/drive": "Full Drive access",
    "https://www.googleapis.com/auth/drive.readonly": "Read Drive files",
    "https://www.googleapis.com/auth/drive.file": "Access only the files you pick",
    "https://www.googleapis.com/auth/drive.metadata.readonly": "Read Drive file names",
    "https://www.googleapis.com/auth/calendar": "Manage calendars",
    "https://www.googleapis.com/auth/calendar.readonly": "Read calendars",
    "https://www.googleapis.com/auth/calendar.events": "Manage calendar events",
    "https://www.googleapis.com/auth/calendar.events.readonly": "Read calendar events",
    "https://www.googleapis.com/auth/gmail.readonly": "Read Gmail",
    "https://www.googleapis.com/auth/gmail.send": "Send email",
    "https://www.googleapis.com/auth/gmail.compose": "Compose email",
    "https://www.googleapis.com/auth/gmail.modify": "Read and modify Gmail",
    "https://mail.google.com/": "Full Gmail access",
    "https://www.googleapis.com/auth/documents": "Read and write documents",
    "https://www.googleapis.com/auth/documents.readonly": "Read documents",
    "https://www.googleapis.com/auth/userinfo.email": "Your Google email address",
    "https://www.googleapis.com/auth/userinfo.profile": "Your basic Google profile",
    openid: "Sign-in identity",
};

const SENSITIVITY_LABELS: Record<string, string> = {
    non_sensitive: "Non-sensitive",
    sensitive: "Sensitive",
    restricted: "Restricted",
    unknown: "Unclassified",
};

export function familyLabel(family: string): string {
    return FAMILY_LABELS[family] ?? family;
}

export function scopeLabel(scope: string): string {
    const known = SCOPE_LABELS[scope];
    if (known) return known;
    const tail = scope.split("/auth/").pop() ?? scope;
    return tail.replace(/[._]/g, " ");
}

export function sensitivityLabel(sensitivity: string): string {
    return SENSITIVITY_LABELS[sensitivity] ?? sensitivity;
}

/** Short, absolute date — Google timestamps are ISO-8601 with a zone. */
export function formatTimestamp(value?: string | null): string | null {
    if (!value) return null;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return null;
    return parsed.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    });
}
