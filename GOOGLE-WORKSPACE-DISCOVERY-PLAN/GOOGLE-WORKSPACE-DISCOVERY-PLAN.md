# Google Workspace Discovery — Connection & Resource Display Plan

> **Date:** 2026-03-24
> **Goal:** After a user connects their Google account, show them all their resources (Sheets, Docs, Calendar, Drive, Gmail) in a single discoverable view.

---

## 1. What Is Already Working

### OAuth Connection Flow ✅

```
Frontend                        Backend                          Google
────────                        ───────                          ──────
GET /google/oauth/authorize
  ?redirectUri=<frontend>
  &scopes=<comma-list>
                                GoogleOAuthService
                                .buildAuthorizationUrl()
                                → returns { authorizationUrl }
    ↓ redirect user to URL
                                                         Google consent screen
                                                         User grants permissions
                                                                ↓
                                GET /google/oauth/callback
                                ?code=...&state=...  (PUBLIC)
                                ↓
                                GoogleOAuthService.exchangeCode()
                                → POST google token endpoint
                                → receive accessToken + refreshToken + scopes
                                ↓
                                GoogleOAuthTokenService.saveOrRefreshToken()
                                → persist GoogleOAuthToken entity:
                                  { ownerId, googleEmail, accessToken,
                                    refreshToken, tokenExpiry, scopes, status=ACTIVE }
                                ↓
                                redirect → frontendUri?connected=true
```

### Token Storage ✅ (`GoogleOAuthToken` entity)

| Field | Value |
|-------|-------|
| `ownerId` | FK to authenticated user |
| `googleEmail` | e.g. `user@gmail.com` |
| `accessToken` | Live access token |
| `refreshToken` | Used to auto-refresh |
| `tokenExpiry` | Auto-refresh if < 5 min away |
| `scopes` | JSONB list of granted scopes |
| `status` | `ACTIVE / REVOKED / ERROR` |

### Status Check ✅

```
GET /api/sawtia/v1/google/oauth/status
→ GoogleOAuthStatusResponse {
    linked: true,
    googleEmail: "user@gmail.com",
    status: "ACTIVE",
    grantedScopes: ["https://www.googleapis.com/auth/spreadsheets", ...],
    tokenExpiry: "2026-03-24T11:30:00"
  }
```

### Existing Discovery Endpoints ✅ (`/api/sawtia/v1/google/tools/...`)

| Endpoint | Returns | Scope needed |
|----------|---------|-------------|
| `GET /sheets` | List of spreadsheets | `drive.readonly` |
| `GET /sheets/{id}/tabs` | Tabs in a sheet | `spreadsheets.readonly` |
| `GET /sheets/{id}/preview` | Cell preview | `spreadsheets.readonly` |
| `GET /calendar/calendars` | List of calendars | `calendar.readonly` |
| `GET /drive/folders` | Drive folders | `drive.readonly` |
| `GET /drive/files` | Files in a folder | `drive.readonly` |
| `GET /gmail/labels` | Gmail labels | `gmail.readonly` |

All endpoints:
- Require JWT (`Authorization: Bearer <jwt>`)
- Check `GoogleOAuthToken.status = ACTIVE` before calling Google
- Return `400 + "Google account not connected"` if not linked

---

## 2. What Is Missing

### 2.1 No Google Docs Discovery Endpoint ❌
`GoogleToolDiscoveryController` has no `/docs` endpoint.
`GoogleDocsService.listDocuments()` is implemented — just not exposed.

### 2.2 No Drive Search Endpoint ❌
Can list files in a folder, but can't search by name/content.
`GoogleDriveService.searchFiles()` is implemented — not exposed.

### 2.3 No Gmail Messages Preview ❌
Only labels are listed. No recent messages shown.
`GoogleGmailService.listMessages()` is implemented — not exposed.

### 2.4 No Unified "Workspace Overview" Endpoint ❌
After connecting, the frontend must call 5+ separate endpoints in parallel.
There is no single `GET /google/workspace/overview` that returns everything at once.

### 2.5 No Scope-Aware Section Flags ❌
If user grants only Sheets scope but not Gmail, the discovery endpoints silently return empty lists.
The frontend has no way to know *why* a section is empty (`scopeMissing` vs `genuinelyEmpty`).

### 2.6 Scopes Passed by Frontend — No Canonical List ❌
`GET /authorize?scopes=...` requires the frontend to know which scopes to request.
There is no server-defined canonical scope list per tool type.

---

## 3. Implementation Plan

### Phase 1 — Add Missing Discovery Endpoints (Small, no new logic)

**3.1 Add `/docs` endpoint to `GoogleToolDiscoveryController`**

```java
GET /api/sawtia/v1/google/tools/docs
→ List<DocsSummary>   // id, name, webViewLink, modifiedTime
// calls GoogleDocsService.listDocuments()
```

Add DTO:

```java
// dtos/google/DocsSummary.java
public record DocsSummary(String id, String name, String webViewLink, String modifiedTime) {}
```

**3.2 Add `/drive/search` endpoint**

```java
GET /api/sawtia/v1/google/tools/drive/search?query=invoice&mimeType=
→ List<DriveItemSummary>
// calls GoogleDriveService.searchFiles()
```

**3.3 Add `/gmail/messages` endpoint**

```java
GET /api/sawtia/v1/google/tools/gmail/messages?labelId=INBOX&maxResults=10
→ List<GmailMessageSummary>   // id, subject, from, date, snippet
// calls GoogleGmailService.listMessages()
```

Add DTO:

```java
// dtos/google/GmailMessageSummary.java
public record GmailMessageSummary(
    String id, String threadId, String subject,
    String from, String date, String snippet) {}
```

---

### Phase 2 — Workspace Overview Endpoint (New endpoint, core of the plan)

**Single call after OAuth connection → returns everything the user has.**

```
GET /api/sawtia/v1/google/workspace/overview
Authorization: Bearer <jwt>

Response: WorkspaceOverviewResponse {
  connected:    true,
  googleEmail:  "user@gmail.com",
  grantedScopes: [...],

  sheets: {
    available: true,          // scope granted
    items: [ SpreadsheetSummary... ]
  },
  calendars: {
    available: true,
    items: [ CalendarSummary... ]
  },
  drive: {
    available: true,
    recentFiles:   [ DriveItemSummary... ],   // last 10 modified
    rootFolders:   [ DriveItemSummary... ]    // top-level folders
  },
  gmail: {
    available: true,
    labels:   [ GmailLabelSummary... ],
    recent:   [ GmailMessageSummary... ]      // last 5 inbox messages
  },
  docs: {
    available: true,
    items: [ DocsSummary... ]
  }
}
```

**Each section has `available: true/false`** — `false` means the required scope was not granted when connecting.

**New files to create:**

```
controllers/GoogleWorkspaceController.java
  GET /api/sawtia/v1/google/workspace/overview

services/GoogleWorkspaceOverviewService.java
  buildOverview(ownerId) → WorkspaceOverviewResponse
  (calls all 5 discovery services in parallel with CompletableFuture)

dtos/google/WorkspaceOverviewResponse.java
dtos/google/WorkspaceSectionResponse.java   // generic wrapper with available + items
dtos/google/DocsSummary.java
dtos/google/GmailMessageSummary.java
```

**Parallel fetch (CompletableFuture):**
```java
// All 5 service calls run concurrently — total latency ≈ slowest single call (~300-800ms)
CompletableFuture<List<SpreadsheetSummary>> sheets    = async(() -> discoveryService.listSpreadsheets(ownerId));
CompletableFuture<List<CalendarSummary>>    calendars = async(() -> discoveryService.listCalendars(ownerId));
CompletableFuture<List<DriveItemSummary>>   files     = async(() -> driveService.listFiles(ownerId, "root", null));
CompletableFuture<List<Map>>                gmail     = async(() -> gmailService.listMessages(ownerId, "INBOX", null, 5));
CompletableFuture<List<Map>>                docs      = async(() -> docsService.listDocuments(ownerId));
```

---

### Phase 3 — Canonical Scope List (Server-Defined)

Instead of forcing the frontend to know which scopes to request,
add a server-side endpoint that returns the required scopes by category:

```
GET /api/sawtia/v1/google/oauth/required-scopes
→ {
    "sheets":   ["https://www.googleapis.com/auth/spreadsheets",
                 "https://www.googleapis.com/auth/drive.readonly"],
    "calendar": ["https://www.googleapis.com/auth/calendar.readonly",
                 "https://www.googleapis.com/auth/calendar.events"],
    "drive":    ["https://www.googleapis.com/auth/drive.readonly"],
    "gmail":    ["https://www.googleapis.com/auth/gmail.readonly",
                 "https://www.googleapis.com/auth/gmail.send"],
    "docs":     ["https://www.googleapis.com/auth/documents",
                 "https://www.googleapis.com/auth/drive.readonly"],
    "all":      [ ...union of all above... ]
  }
```

Frontend calls this first → then redirects to `/authorize?scopes=<all>`.

**New file:**
```
controllers/GoogleOAuthController.java  (add one endpoint)
  GET /api/sawtia/v1/google/oauth/required-scopes
  → Map<String, List<String>>   (no auth needed — public endpoint)
```

---

## 4. Complete Flow After Phase 1-3

```
USER ACTION: "Connect Google Account"

1. Frontend:  GET /google/oauth/required-scopes
              → gets full scope list for all tools

2. Frontend:  GET /google/oauth/authorize
                ?redirectUri=<frontend/settings>
                &scopes=<all-scopes>
              → receives { authorizationUrl }

3. Frontend:  redirect user to authorizationUrl

4. Google:    consent screen → user grants all permissions

5. Google:    redirect → GET /google/oauth/callback?code=...&state=...
              (backend exchanges code, saves token)
              → redirect → frontend/settings?connected=true

6. Frontend:  sees ?connected=true in URL
              → GET /google/oauth/status  (confirm ACTIVE)
              → GET /google/workspace/overview  (load all resources)

7. Frontend shows:
   ┌─────────────────────────────────────────────────┐
   │  ✅ Connected as user@gmail.com                 │
   │                                                 │
   │  📊 Google Sheets (12 spreadsheets)             │
   │     • Sales Leads Q1 2026                       │
   │     • Customer DB                               │
   │     • Campaign Results   ...                    │
   │                                                 │
   │  📅 Google Calendar (3 calendars)               │
   │     • My Calendar (primary)                     │
   │     • Team Calendar                             │
   │     • Holidays                                  │
   │                                                 │
   │  📁 Google Drive (8 files, 4 folders)           │
   │     Folders: Projects / Archive / Reports...    │
   │     Recent: contract.docx, invoice.pdf...       │
   │                                                 │
   │  📄 Google Docs (5 documents)                   │
   │     • Meeting Notes 2026                        │
   │     • Sales Script Template                     │
   │                                                 │
   │  📧 Gmail (INBOX: 3 recent)                     │
   │     • Re: Proposal — alice@company.com          │
   │     • Invoice #1042 — billing@vendor.com        │
   └─────────────────────────────────────────────────┘
```

---

## 4b. Frontend Analysis — `sawtia-dashboard/src/app/features/connexions`

### Current Page Structure

The connexions page is a tabbed shell (`connexions.component.ts`) with 6 tabs:
`overview | telephone | whatsapp | meta | website | google`

The **Google tab** (`google.component.ts` + `google.component.html`) currently shows:

```
┌──────────────────────────────────────────────────────────────────┐
│  Left col (xl:col-span-4)           Right col (xl:col-span-8)   │
│                                                                  │
│  Google logo + title                ┌─ card ──────────────────┐ │
│  Description text                   │                         │ │
│  Scope list:                        │  [loading spinner]      │ │
│    ☐ Google Sheets    ← hardcoded   │                         │ │
│    ☐ Google Drive     ← hardcoded   │  OR (not connected):    │ │
│                                     │  [Google logo]          │ │
│                                     │  "Not connected"        │ │
│                                     │  [Connect button]       │ │
│                                     │                         │ │
│                                     │  OR (connected):        │ │
│                                     │  email + status badge   │ │
│                                     │  token expiry           │ │
│                                     │  granted scopes (pills) │ │
│                                     │  [Disconnect button]    │ │
│                                     └─────────────────────────┘ │
│                                     [Security notice]           │
└──────────────────────────────────────────────────────────────────┘
```

### What Already Exists in Frontend

| File | What's inside | Status |
|------|--------------|--------|
| `google.component.ts` | `loadStatus()`, `connect()`, `disconnect()`, signals for status/loading/error | ✅ Done |
| `google.component.html` | OAuth callback banner, not-connected state, connected state with scopes | ✅ Done |
| `GoogleOAuthService` | `getStatus()`, `revoke()`, `startOAuthFlow()` | ✅ Done |
| `GoogleToolDiscoveryService` | `listSpreadsheets()`, `listSheetTabs()`, `previewSheet()`, `listCalendars()`, `listDriveFolders()`, `listDriveFiles()`, `listGmailLabels()` | ✅ Done |
| `connexions.model.ts` | `SpreadsheetSummary`, `SheetTabSummary`, `CalendarSummary`, `DriveItemSummary`, `GmailLabelSummary`, `GoogleOAuthStatusResponse` | ✅ Done |

### What Is Missing in Frontend

| Gap | Description |
|-----|-------------|
| **No resource display** | After connecting, `google.component` only shows token info. It never calls `GoogleToolDiscoveryService` to show actual data. |
| **Only 2 scopes shown** | Left panel hardcodes only Sheets + Drive. Calendar, Gmail, Docs missing. |
| **No `DocsSummary` model** | Missing interface for Docs discovery response. |
| **No `GmailMessageSummary` model** | Missing interface for Gmail messages. |
| **No `listDocs()` in service** | `GoogleToolDiscoveryService` has no method for the new `/docs` endpoint. |
| **No `searchDrive()` in service** | No method for the new `/drive/search` endpoint. |
| **No `listGmailMessages()` in service** | No method for the new `/gmail/messages` endpoint. |
| **Model field mismatch** | Backend `GoogleOAuthStatusResponse` uses `grantedScopes`, frontend model uses `scopes`. |

### Frontend Changes Required

#### A. `connexions.model.ts` — Add 2 interfaces + fix field name

```typescript
// ADD
export interface DocsSummary {
  id: string;
  name: string;
  webViewLink: string;
  modifiedTime: string;
}

export interface GmailMessageSummary {
  id: string;
  threadId: string;
  subject: string;
  from: string;
  date: string;
  snippet: string;
}

// FIX — rename scopes → grantedScopes to match backend
export interface GoogleOAuthStatusResponse {
  connected: boolean;
  googleEmail: string | null;
  status: GoogleOAuthTokenStatus | null;
  grantedScopes: string[] | null;   // was: scopes
  tokenExpiry: string | null;
}
```

#### B. `GoogleToolDiscoveryService` — Add 3 new methods

```typescript
// ADD to existing service

/** GET /google/tools/docs */
listDocs(): Observable<DocsSummary[]>

/** GET /google/tools/drive/search?query=&mimeType= */
searchDrive(query: string, mimeType?: string): Observable<DriveItemSummary[]>

/** GET /google/tools/gmail/messages?labelId=&maxResults= */
listGmailMessages(labelId?: string, maxResults?: number): Observable<GmailMessageSummary[]>
```

#### C. `google.component.ts` — Add workspace loading + tab state

```typescript
// ADD signals
activeTab = signal<'sheets' | 'calendar' | 'drive' | 'gmail' | 'docs'>('sheets');
isLoadingWorkspace = signal(false);
sheets     = signal<SpreadsheetSummary[]>([]);
calendars  = signal<CalendarSummary[]>([]);
driveFolders = signal<DriveItemSummary[]>([]);
driveFiles   = signal<DriveItemSummary[]>([]);
gmailLabels  = signal<GmailLabelSummary[]>([]);
gmailMessages = signal<GmailMessageSummary[]>([]);
docs       = signal<DocsSummary[]>([]);

// MODIFY loadStatus(): after status loads and status.connected = true, call:
loadWorkspace(): void  // parallel fetch of all 5 categories using forkJoin
```

#### D. `google.component.html` — Add workspace resource panel

Below the existing connected status card, add a **Workspace Resources** section visible only when `status().connected && isActive()`:

```
When CONNECTED + ACTIVE, show below status card:

┌─ Workspace Resources ───────────────────────────────────────────┐
│                                                                  │
│  [📊 Sheets (12)] [📅 Calendar (3)] [📁 Drive] [📧 Gmail] [📄 Docs]  │
│  ───────────────────────────────────────────────────────────     │
│                                                                  │
│  @if activeTab() === 'sheets'                                    │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐   │
│  │ Sales Leads Q1  │ │ Customer DB     │ │ Results 2026    │   │
│  │ Modified: 2h ago│ │ Modified: 1d ago│ │ Modified: 3d ago│   │
│  │ [Open ↗]        │ │ [Open ↗]        │ │ [Open ↗]        │   │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘   │
│                                                                  │
│  @if activeTab() === 'calendar'                                  │
│  • My Calendar (primary)                                        │
│  • Team Calendar                                                │
│                                                                  │
│  @if activeTab() === 'drive'                                     │
│  Folders: [Projects] [Archive] [Reports]                        │
│  Recent files: contract.docx, invoice.pdf ...                   │
│                                                                  │
│  @if activeTab() === 'gmail'                                     │
│  Labels: INBOX SENT SPAM [Lead-Follow-Up]                       │
│  Recent: Re: Proposal · alice@company.com · 2h ago              │
│                                                                  │
│  @if activeTab() === 'docs'                                      │
│  • Meeting Notes 2026                                           │
│  • Sales Script Template                                        │
└──────────────────────────────────────────────────────────────────┘
```

---

## 5. Files to Create / Modify

### Backend — New Files

| File | Type | Purpose |
|------|------|---------|
| `dtos/google/DocsSummary.java` | DTO record | `{id, name, webViewLink, modifiedTime}` |
| `dtos/google/GmailMessageSummary.java` | DTO record | `{id, threadId, subject, from, date, snippet}` |
| `dtos/google/WorkspaceSectionResponse.java` | DTO | `{available, scopeMissing, items}` |
| `dtos/google/WorkspaceOverviewResponse.java` | DTO | Full overview with all sections |
| `services/GoogleWorkspaceOverviewService.java` | Service | Parallel fetch of all 5 sections |
| `controllers/GoogleWorkspaceController.java` | Controller | `GET /google/workspace/overview` |

### Backend — Modified Files

| File | Change |
|------|--------|
| `controllers/GoogleToolDiscoveryController.java` | Add `/docs`, `/drive/search`, `/gmail/messages` endpoints |
| `services/GoogleToolDiscoveryService.java` | Add `listDocuments()` delegating to `GoogleDocsService` |
| `controllers/GoogleOAuthController.java` | Add `GET /required-scopes` endpoint (public, no auth) |

### Frontend — Modified Files (`sawtia-dashboard`)

| File | Change |
|------|--------|
| `connexions/models/connexions.model.ts` | Add `DocsSummary`, `GmailMessageSummary`; rename `scopes→grantedScopes` |
| `connexions/services/google-tool-discovery.service.ts` | Add `listDocs()`, `searchDrive()`, `listGmailMessages()` |
| `connexions/components/google/google.component.ts` | Add workspace signals, `loadWorkspace()` with `forkJoin`, `activeTab` |
| `connexions/components/google/google.component.html` | Update left panel scopes list; add workspace resources panel with 5 tabs |

---

## 6. Endpoint Summary After Implementation

### OAuth
| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/google/oauth/required-scopes` | Public | Get canonical scope list |
| `GET` | `/google/oauth/authorize` | JWT | Start OAuth, get consent URL |
| `GET` | `/google/oauth/callback` | Public | Exchange code, save token |
| `GET` | `/google/oauth/status` | JWT | Check connection status |
| `DELETE` | `/google/oauth/revoke` | JWT | Disconnect account |

### Workspace Overview
| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/google/workspace/overview` | JWT | **All resources in one call** |

### Individual Discovery (for pickers)
| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/google/tools/sheets` | JWT | List spreadsheets |
| `GET` | `/google/tools/sheets/{id}/tabs` | JWT | List sheet tabs |
| `GET` | `/google/tools/sheets/{id}/preview` | JWT | Preview cells |
| `GET` | `/google/tools/calendar/calendars` | JWT | List calendars |
| `GET` | `/google/tools/drive/folders` | JWT | List folders |
| `GET` | `/google/tools/drive/files` | JWT | List files in folder |
| `GET` | `/google/tools/drive/search` | JWT | Search Drive *(new)* |
| `GET` | `/google/tools/gmail/labels` | JWT | List Gmail labels |
| `GET` | `/google/tools/gmail/messages` | JWT | List recent messages *(new)* |
| `GET` | `/google/tools/docs` | JWT | List Google Docs *(new)* |
