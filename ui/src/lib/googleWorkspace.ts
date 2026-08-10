/**
 * Hand-written API wrapper for the Google connection and workspace discovery
 * endpoints (`api/routes/integrations_google.py`).
 *
 * Hand-written rather than generated: `npm run generate-client` reads the live
 * backend's OpenAPI document, and these routes ship in the same change as the
 * page below them — there is no running backend to read them from yet. Re-run
 * the generator once the backend is up and this module can be replaced by
 * `@/client/sdk.gen` imports; the shapes here mirror `api/schemas/
 * google_workspace.py` and `api/schemas/google_sheets.py` field for field.
 *
 * Same mechanics as `lib/platformAdminApi.ts`: requests go through the shared
 * generated `client`, inheriting its base URL and the auth interceptor
 * registered once auth is loaded. Callers must guard on `authLoading`/`user`
 * (see ui/AGENTS.md).
 *
 * Errors come back as `GoogleRequestError`, not bare strings, because the page
 * has to branch on *why* a call failed: a missing scope offers "grant this
 * permission", a dead grant offers "reconnect", and a 503 means the deployment
 * has no OAuth client at all. `message` is always a plain string — the
 * backend's `detail` is a dict here and must never reach JSX.
 */

import { client } from "@/client/client.gen";
import { detailFromError } from "@/lib/apiError";

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------

/** Browser-safe deployment config: no client secret has a field here. */
export interface GoogleOAuthPublicConfig {
    enabled: boolean;
    client_id?: string | null;
    picker_api_key?: string | null;
    picker_app_id?: string | null;
    scopes: string[];
}

export interface GoogleConnectionStatus {
    connected: boolean;
    /** 'connected' or 'revoked'; null when the organization never connected. */
    status?: string | null;
    account_email?: string | null;
    /** Access-token expiry, not an expiry of the connection — the backend refreshes it. */
    expires_at?: string | null;
    scopes: string[];
    requires_reconsent: boolean;
    revoked_reason?: string | null;
    oauth_config: GoogleOAuthPublicConfig;
}

export interface GoogleAuthorizeResponse {
    authorization_url: string;
    redirect_uri: string;
    expires_in_seconds: number;
}

export interface GoogleDisconnectResponse {
    disconnected: boolean;
}

// ---------------------------------------------------------------------------
// Resource summaries
// ---------------------------------------------------------------------------

export interface SpreadsheetSummary {
    id: string;
    name: string;
    web_url?: string | null;
    modified_time?: string | null;
}

export interface SheetTabSummary {
    sheet_id?: number | null;
    title: string;
    index?: number | null;
    row_count?: number | null;
    column_count?: number | null;
}

export interface CalendarSummary {
    id: string;
    summary?: string | null;
    description?: string | null;
    primary: boolean;
    /** 'owner' | 'writer' | 'reader' | 'freeBusyReader' — below 'writer' cannot host bookings. */
    access_role?: string | null;
    color_id?: string | null;
    time_zone?: string | null;
}

export interface DriveItemSummary {
    id: string;
    name: string;
    mime_type?: string | null;
    is_folder: boolean;
    web_url?: string | null;
    modified_time?: string | null;
}

export interface GmailLabelSummary {
    id: string;
    name: string;
    type?: string | null;
}

export interface GmailMessageSummary {
    id: string;
    thread_id?: string | null;
    subject?: string | null;
    /** The From header verbatim. Named `from_address` because `from` is a Python keyword. */
    from_address?: string | null;
    /** Kept as text: senders emit malformed dates and a parse failure must not drop the row. */
    date?: string | null;
    snippet?: string | null;
}

export interface DocsSummary {
    id: string;
    name: string;
    web_url?: string | null;
    modified_time?: string | null;
}

// ---------------------------------------------------------------------------
// Workspace overview
// ---------------------------------------------------------------------------

/**
 * Why a section looks the way it does. Read these before the items: an empty
 * section with `scope_missing` is a permission to ask for, one with `error` is
 * Google failing, and one with neither is an account that has nothing there.
 */
export interface WorkspaceSectionFlags {
    available: boolean;
    scope_missing: boolean;
    missing_scopes: string[];
    error?: string | null;
    /**
     * Set when the section worked but the grant narrows what it can see, so a
     * short or empty list is expected rather than wrong. Distinct from
     * `error`: nothing failed, and distinct from `scope_missing`: the scopes
     * this section declares WERE granted.
     */
    notice?: string | null;
}

export interface WorkspaceSectionResponse<ItemT> extends WorkspaceSectionFlags {
    items: ItemT[];
}

export interface WorkspaceDriveSection extends WorkspaceSectionFlags {
    recent_files: DriveItemSummary[];
    root_folders: DriveItemSummary[];
}

export interface WorkspaceGmailSection extends WorkspaceSectionFlags {
    labels: GmailLabelSummary[];
    recent: GmailMessageSummary[];
}

export interface WorkspaceOverview {
    connected: boolean;
    google_email?: string | null;
    status?: string | null;
    requires_reconsent: boolean;
    /** What this organization consented to. */
    granted_scopes: string[];
    /** What the deployment asks for at all — a scope absent here cannot be re-consented into. */
    requested_scopes: string[];
    sheets: WorkspaceSectionResponse<SpreadsheetSummary>;
    calendars: WorkspaceSectionResponse<CalendarSummary>;
    drive: WorkspaceDriveSection;
    gmail: WorkspaceGmailSection;
    docs: WorkspaceSectionResponse<DocsSummary>;
}

// ---------------------------------------------------------------------------
// Scope registry
// ---------------------------------------------------------------------------

export interface GoogleScopeInfo {
    scope: string;
    /** 'non_sensitive' | 'sensitive' | 'restricted' | 'unknown'. */
    sensitivity: string;
    requested: boolean;
    granted: boolean;
}

export interface GoogleRequiredScopes {
    /** Scopes per family — 'sheets', 'calendar', 'drive', 'gmail', 'docs', plus 'all'. */
    families: Record<string, string[]>;
    requested_scopes: string[];
    deployment_sensitivity: string;
    scope_details: GoogleScopeInfo[];
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** A failed Google call, with the fields the UI branches on. */
export class GoogleRequestError extends Error {
    readonly status: number | undefined;
    /** Backend error code, e.g. 'insufficient_scope', 'not_configured'. */
    readonly code: string | undefined;
    /** Scope to add to the consent, when the grant was too narrow. */
    readonly missingScope: string | undefined;
    /** True when only a new trip through the consent screen can fix it. */
    readonly requiresReconsent: boolean;

    constructor(
        message: string,
        fields: {
            status?: number;
            code?: string;
            missingScope?: string;
            requiresReconsent?: boolean;
        } = {},
    ) {
        super(message);
        this.name = "GoogleRequestError";
        this.status = fields.status;
        this.code = fields.code;
        this.missingScope = fields.missingScope;
        this.requiresReconsent = Boolean(fields.requiresReconsent);
    }
}

/** Message to show for any thrown value — never an object, never a raw `detail`. */
export function googleErrorMessage(err: unknown, fallback = "Google request failed"): string {
    if (err instanceof Error && err.message) return err.message;
    return detailFromError(err, fallback);
}

function toGoogleError(
    err: unknown,
    status: number | undefined,
    fallback: string,
): GoogleRequestError {
    // These routes answer with `detail` as a dict; `detailFromError` only knows
    // the string and 422-array shapes, so the dict is unpacked here and the
    // helper stays the fallback for everything else.
    const detail = (err as { detail?: unknown } | null | undefined)?.detail;
    const fields =
        detail && typeof detail === "object" && !Array.isArray(detail)
            ? (detail as Record<string, unknown>)
            : null;

    const message =
        fields && typeof fields.message === "string" && fields.message
            ? fields.message
            : detailFromError(err, fallback);

    return new GoogleRequestError(message, {
        status,
        code: typeof fields?.code === "string" ? fields.code : undefined,
        missingScope:
            typeof fields?.missing_scope === "string" ? fields.missing_scope : undefined,
        requiresReconsent: fields?.requires_reconsent === true,
    });
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

const BASE = "/api/v1/integrations/google";

async function request<T>(
    method: "get" | "delete",
    url: string,
    fallback: string,
    query?: Record<string, unknown>,
): Promise<T> {
    const res = await client[method]({ url, query });
    if (res.error !== undefined) {
        throw toGoogleError(res.error, res.response?.status, fallback);
    }
    return res.data as T;
}

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------

export async function fetchGoogleStatus(): Promise<GoogleConnectionStatus> {
    return request<GoogleConnectionStatus>(
        "get",
        `${BASE}/status`,
        "Failed to read the Google connection status",
    );
}

/**
 * Public: the scope registry this deployment was configured with. Fetched
 * before any connection exists so the page never hardcodes a scope list of its
 * own and drifts from the server's.
 */
export async function fetchGoogleRequiredScopes(): Promise<GoogleRequiredScopes> {
    return request<GoogleRequiredScopes>(
        "get",
        `${BASE}/oauth/required-scopes`,
        "Failed to read the Google permission list",
    );
}

/**
 * Mint a consent URL for the caller's organization. The scopes and the signed,
 * organization-bound state are chosen server-side — the browser only follows
 * the URL it gets back.
 */
export async function startGoogleAuthorization(): Promise<GoogleAuthorizeResponse> {
    return request<GoogleAuthorizeResponse>(
        "get",
        `${BASE}/authorize`,
        "Failed to start the Google connection",
    );
}

/** Idempotent: an organization with nothing connected gets `disconnected: false`. */
export async function disconnectGoogle(): Promise<GoogleDisconnectResponse> {
    return request<GoogleDisconnectResponse>(
        "delete",
        `${BASE}/connection`,
        "Failed to disconnect the Google account",
    );
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

export async function fetchWorkspaceOverview(): Promise<WorkspaceOverview> {
    return request<WorkspaceOverview>(
        "get",
        `${BASE}/workspace/overview`,
        "Failed to load your Google workspace",
    );
}

export async function listSpreadsheets(): Promise<SpreadsheetSummary[]> {
    return request<SpreadsheetSummary[]>(
        "get",
        `${BASE}/tools/sheets`,
        "Failed to list spreadsheets",
    );
}

export async function listSheetTabs(spreadsheetId: string): Promise<SheetTabSummary[]> {
    return request<SheetTabSummary[]>(
        "get",
        `${BASE}/tools/sheets/${encodeURIComponent(spreadsheetId)}/tabs`,
        "Failed to list the spreadsheet tabs",
    );
}

/**
 * First cells of a spreadsheet. Rows are ragged — Google trims trailing empty
 * cells — so pad against the longest row rather than assuming a rectangle.
 */
export async function previewSpreadsheet(
    spreadsheetId: string,
    range?: string,
): Promise<unknown[][]> {
    return request<unknown[][]>(
        "get",
        `${BASE}/tools/sheets/${encodeURIComponent(spreadsheetId)}/preview`,
        "Failed to preview the spreadsheet",
        range ? { range } : undefined,
    );
}

export async function listCalendars(): Promise<CalendarSummary[]> {
    return request<CalendarSummary[]>(
        "get",
        `${BASE}/tools/calendar/calendars`,
        "Failed to list calendars",
    );
}

export async function listDriveFolders(parentId?: string): Promise<DriveItemSummary[]> {
    return request<DriveItemSummary[]>(
        "get",
        `${BASE}/tools/drive/folders`,
        "Failed to list Drive folders",
        parentId ? { parent_id: parentId } : undefined,
    );
}

export async function listDriveFiles(params?: {
    folderId?: string;
    mimeType?: string;
}): Promise<DriveItemSummary[]> {
    const query: Record<string, unknown> = {};
    if (params?.folderId) query.folder_id = params.folderId;
    if (params?.mimeType) query.mime_type = params.mimeType;
    return request<DriveItemSummary[]>(
        "get",
        `${BASE}/tools/drive/files`,
        "Failed to list Drive files",
        query,
    );
}

export async function searchDriveFiles(
    query: string,
    mimeType?: string,
): Promise<DriveItemSummary[]> {
    const params: Record<string, unknown> = { query };
    if (mimeType) params.mime_type = mimeType;
    return request<DriveItemSummary[]>(
        "get",
        `${BASE}/tools/drive/search`,
        "Failed to search Drive",
        params,
    );
}

export async function listGmailLabels(): Promise<GmailLabelSummary[]> {
    return request<GmailLabelSummary[]>(
        "get",
        `${BASE}/tools/gmail/labels`,
        "Failed to list Gmail labels",
    );
}

export async function listGmailMessages(params?: {
    labelId?: string;
    maxResults?: number;
}): Promise<GmailMessageSummary[]> {
    const query: Record<string, unknown> = {};
    if (params?.labelId) query.label_id = params.labelId;
    if (params?.maxResults !== undefined) query.max_results = params.maxResults;
    return request<GmailMessageSummary[]>(
        "get",
        `${BASE}/tools/gmail/messages`,
        "Failed to list Gmail messages",
        query,
    );
}

export async function listGoogleDocs(): Promise<DocsSummary[]> {
    return request<DocsSummary[]>(
        "get",
        `${BASE}/tools/docs`,
        "Failed to list documents",
    );
}
