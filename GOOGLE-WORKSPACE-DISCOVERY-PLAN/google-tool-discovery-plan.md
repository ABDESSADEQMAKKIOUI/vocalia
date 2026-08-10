# Google Tool Discovery Endpoints — Implementation Plan

> **Created:** 2026-03-15
> **Goal:** After a user links their Google account, provide endpoints that let the
> frontend list their available Google resources (spreadsheets, sheet tabs, calendars,
> Drive folders) so they can pick the exact resource to bind to an agent tool or campaign.

---

## 1. Problem Statement

When a user attaches a Google Sheets tool to a campaign they need to specify *which*
spreadsheet and *which tab* the tool will operate on. Today this value is typed manually
into `CampaignToolParams.params` as raw JSON — error-prone and poor UX.

These discovery endpoints solve that by letting the frontend render a picker:

```
Campaign > Tool Params > Google Sheets
  Spreadsheet: [ My Sales Pipeline ▼ ]   ← populated from GET /api/google/tools/sheets
  Tab:         [ Leads ▼ ]               ← populated from GET /api/google/tools/sheets/{id}/tabs
```

---

## 2. Architecture Overview

```
Frontend (campaign config UI)
    │
    │  1. User has already connected Google account
    │     (GoogleOAuthToken exists with status=ACTIVE)
    │
    │  2. GET /api/google/tools/sheets
    │     Authorization: Bearer <jwt>
    │     ← [ {id, name, url}, ... ]
    │
    │  3. User picks a spreadsheet; frontend fetches tabs:
    │     GET /api/google/tools/sheets/{spreadsheetId}/tabs
    │     ← [ {sheetId, title, index}, ... ]
    │
    │  4. PUT /api/sawtia/v1/campaigns/{id}/tool-params
    │     { toolId, params: { spreadsheetId, sheetName } }
    │
    ▼
Backend resolves live token → Google API → returns resource list
```

---

## 3. Google Cloud Console — Required API Services

Before any endpoint works, each Google API must be **enabled** in the Google Cloud Console
project that owns the OAuth2 credentials (`GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`).

### 3.1 — How to enable an API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Select your project (top-left dropdown)
3. Navigate to **APIs & Services → Library**
4. Search for the API name → click **Enable**

### 3.2 — APIs to enable

| API Name | Console Search Term | Used For | Enable URL |
|---|---|---|---|
| **Google Drive API** | `Google Drive API` | List spreadsheets, folders, files | `console.cloud.google.com/apis/library/drive.googleapis.com` |
| **Google Sheets API** | `Google Sheets API` | Read/write sheet data, list tabs | `console.cloud.google.com/apis/library/sheets.googleapis.com` |
| **Google Calendar API** | `Google Calendar API` | List calendars, create/read events | `console.cloud.google.com/apis/library/calendar-json.googleapis.com` |
| **Gmail API** | `Gmail API` | List labels, read/send emails | `console.cloud.google.com/apis/library/gmail.googleapis.com` |
| **People API** | `People API` | Fetch user profile / email after OAuth | `console.cloud.google.com/apis/library/people.googleapis.com` |

> **Important:** Enabling the API in the Cloud Console is separate from requesting OAuth scopes.
> Both must be done:
> - API enabled in Cloud Console → allows your app to call the API
> - Scope requested in the OAuth consent → allows the API to act on behalf of the user

### 3.3 — OAuth Consent Screen setup

Navigate to **APIs & Services → OAuth consent screen**:

| Setting | Value |
|---|---|
| App type | External (for production) / Internal (for testing) |
| App name | Sawtia |
| Authorized domains | `sawtia.ma` |
| Scopes to add | See table below |

**Scopes to add in the consent screen:**

| Scope | API | Sensitivity |
|---|---|---|
| `https://www.googleapis.com/auth/drive.readonly` | Drive | Restricted |
| `https://www.googleapis.com/auth/spreadsheets` | Sheets | Restricted |
| `https://www.googleapis.com/auth/calendar.readonly` | Calendar | Sensitive |
| `https://www.googleapis.com/auth/gmail.readonly` | Gmail | Restricted |
| `openid` | Identity | Non-sensitive |
| `email` | Identity | Non-sensitive |
| `profile` | Identity | Non-sensitive |

> Restricted scopes (`drive.readonly`, `spreadsheets`, `gmail.readonly`) require a **Google
> security review** before being usable by external users in production. During development,
> add test users under **OAuth consent screen → Test users**.

### 3.4 — Authorized Redirect URIs

In **APIs & Services → Credentials → OAuth 2.0 Client ID**, add:

| Environment | Redirect URI |
|---|---|
| Local dev | `http://localhost:8083/api/google/oauth/callback` |
| Production | `https://sawtia.ma/api/google/oauth/callback` |

---

## 4. Google APIs Used (by endpoint)

| Resource | Google API | Endpoint |
|---|---|---|
| List spreadsheets | Drive API v3 | `GET /drive/v3/files?q=mimeType='application/vnd.google-apps.spreadsheet'` |
| List tabs in sheet | Sheets API v4 | `GET /spreadsheets/v4/spreadsheets/{id}?fields=sheets.properties` |
| List calendars | Calendar API v3 | `GET /calendar/v3/users/me/calendarList` |
| List Drive folders | Drive API v3 | `GET /drive/v3/files?q=mimeType='application/vnd.google-apps.folder'` |
| List Drive files | Drive API v3 | `GET /drive/v3/files?q='{folderId}' in parents` |
| List Gmail labels | Gmail API | `GET /gmail/v1/users/me/labels` |

---

## 5. Required OAuth Scopes

| Scope | Used For |
|---|---|
| `https://www.googleapis.com/auth/drive.readonly` | List spreadsheets, folders, files |
| `https://www.googleapis.com/auth/spreadsheets.readonly` | Read sheet tabs and data |
| `https://www.googleapis.com/auth/calendar.readonly` | List calendars |
| `https://www.googleapis.com/auth/gmail.readonly` | List Gmail labels |

> The `/authorize` endpoint accepts a `scopes` query param — users authorize the scopes
> that match the tools they want to use.

---

## 6. Endpoint Design

### 5.1 — Google Sheets

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/google/tools/sheets` | List all spreadsheets in the user's Drive |
| `GET` | `/api/google/tools/sheets/{spreadsheetId}/tabs` | List tabs (worksheets) inside a spreadsheet |
| `GET` | `/api/google/tools/sheets/{spreadsheetId}/preview?range=Sheet1!A1:E5` | Preview data from a range |

### 5.2 — Google Calendar

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/google/tools/calendar/calendars` | List all calendars the user has access to |

### 5.3 — Google Drive

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/google/tools/drive/folders` | List Drive folders (optionally under a parent) |
| `GET` | `/api/google/tools/drive/files` | List files (optionally filtered by folder and/or mime type) |

### 5.4 — Gmail

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/google/tools/gmail/labels` | List Gmail labels for inbox automation tools |

---

## 7. Response DTOs

### 6.1 — `SpreadsheetSummary`

```java
public record SpreadsheetSummary(
    String id,          // Drive file ID (= spreadsheetId for Sheets API)
    String name,        // Document title
    String webUrl,      // https://docs.google.com/spreadsheets/d/{id}/edit
    String modifiedTime // ISO-8601 last-modified timestamp
) {}
```

### 6.2 — `SheetTabSummary`

```java
public record SheetTabSummary(
    int    sheetId,     // internal numeric ID (for reference)
    String title,       // tab name shown to user
    int    index,       // tab position (0-based)
    int    rowCount,    // grid dimensions
    int    columnCount
) {}
```

### 6.3 — `CalendarSummary`

```java
public record CalendarSummary(
    String  id,          // calendarId used in Calendar API calls
    String  summary,     // human-readable calendar name
    String  description,
    boolean primary,     // true for the user's primary calendar
    String  accessRole,  // "owner", "writer", "reader", "freeBusyReader"
    String  colorId
) {}
```

### 6.4 — `DriveItemSummary`

```java
public record DriveItemSummary(
    String id,           // Drive file/folder ID
    String name,
    String mimeType,     // "application/vnd.google-apps.folder" etc.
    String webUrl,
    String modifiedTime
) {}
```

### 6.5 — `GmailLabelSummary`

```java
public record GmailLabelSummary(
    String id,       // labelId used in Gmail API
    String name,     // "INBOX", "SENT", or custom label
    String type      // "system" or "user"
) {}
```

---

## 8. New Files — Complete List

### New files to create

| File | Purpose |
|---|---|
| `dtos/google/SpreadsheetSummary.java` | DTO for a spreadsheet entry |
| `dtos/google/SheetTabSummary.java` | DTO for a sheet tab |
| `dtos/google/CalendarSummary.java` | DTO for a calendar |
| `dtos/google/DriveItemSummary.java` | DTO for a Drive file or folder |
| `dtos/google/GmailLabelSummary.java` | DTO for a Gmail label |
| `services/GoogleToolDiscoveryService.java` | Calls Drive / Sheets / Calendar / Gmail APIs |
| `controllers/GoogleToolDiscoveryController.java` | REST endpoints for all discovery resources |

### Modified files

| File | Change |
|---|---|
| None | Discovery is purely additive |

---

## 9. Service Design — `GoogleToolDiscoveryService`

```java
@Service
public class GoogleToolDiscoveryService {

    // Sheets discovery
    public List<SpreadsheetSummary> listSpreadsheets(String ownerId) { ... }
    public List<SheetTabSummary>    listTabs(String ownerId, String spreadsheetId) { ... }
    public List<List<String>>       previewRange(String ownerId, String spreadsheetId, String range) { ... }

    // Calendar discovery
    public List<CalendarSummary> listCalendars(String ownerId) { ... }

    // Drive discovery
    public List<DriveItemSummary> listFolders(String ownerId, String parentId) { ... }
    public List<DriveItemSummary> listFiles(String ownerId, String folderId, String mimeType) { ... }

    // Gmail discovery
    public List<GmailLabelSummary> listGmailLabels(String ownerId) { ... }
}
```

All methods:
- Call `GoogleOAuthService.getValidAccessToken(ownerId)` for a live Bearer token
- Use OkHttp directly (no SDK) for lightweight calls
- Return empty list on error (never throw — frontend gets a graceful empty state)
- Log warnings on non-2xx responses

---

## 10. Google API Call Details

### 9.1 — List Spreadsheets (Drive API v3)

```
GET https://www.googleapis.com/drive/v3/files
  ?q=mimeType='application/vnd.google-apps.spreadsheet' and trashed=false
  &fields=files(id,name,webViewLink,modifiedTime)
  &orderBy=modifiedTime desc
  &pageSize=100
Authorization: Bearer <access_token>
```

Required scope: `https://www.googleapis.com/auth/drive.readonly`

### 9.2 — List Sheet Tabs (Sheets API v4)

```
GET https://sheets.googleapis.com/v4/spreadsheets/{spreadsheetId}
  ?fields=sheets.properties(sheetId,title,index,gridProperties)
Authorization: Bearer <access_token>
```

### 9.3 — Preview Range (Sheets API v4)

```
GET https://sheets.googleapis.com/v4/spreadsheets/{spreadsheetId}/values/{range}
Authorization: Bearer <access_token>
```

### 9.4 — List Calendars (Calendar API v3)

```
GET https://www.googleapis.com/calendar/v3/users/me/calendarList
  ?fields=items(id,summary,description,primary,accessRole,colorId)
Authorization: Bearer <access_token>
```

Required scope: `https://www.googleapis.com/auth/calendar.readonly`

### 9.5 — List Drive Folders

```
GET https://www.googleapis.com/drive/v3/files
  ?q=mimeType='application/vnd.google-apps.folder' and trashed=false [and '{parentId}' in parents]
  &fields=files(id,name,mimeType,webViewLink,modifiedTime)
  &pageSize=100
Authorization: Bearer <access_token>
```

### 9.6 — List Drive Files

```
GET https://www.googleapis.com/drive/v3/files
  ?q='{folderId}' in parents and trashed=false [and mimeType='{filter}']
  &fields=files(id,name,mimeType,webViewLink,modifiedTime)
  &pageSize=100
Authorization: Bearer <access_token>
```

### 9.7 — List Gmail Labels

```
GET https://gmail.googleapis.com/gmail/v1/users/me/labels
Authorization: Bearer <access_token>
```

Required scope: `https://www.googleapis.com/auth/gmail.readonly`

---

## 11. Error Handling Strategy

| Scenario | Behavior |
|---|---|
| Token not linked | Returns `ApiResponse.error("Google account not connected")` with HTTP 400 |
| Token expired / OAUTH error | `GoogleOAuthService.getValidAccessToken()` auto-refreshes inline |
| Google API returns 403 (insufficient scope) | Returns `ApiResponse.error("Missing required Google scope: <scope>")` with HTTP 403 |
| Google API returns 404 (sheet not found) | Returns empty list |
| Network timeout | Returns empty list, logs warning |

---

## 12. Frontend Integration Example

### Spreadsheet picker

```typescript
// 1. Load spreadsheets when user opens the tool param form
const sheets = await api.get('/api/google/tools/sheets');
// → [ { id: "1BxiMV...", name: "Sales Pipeline", webUrl: "..." }, ... ]

// 2. Load tabs when user selects a spreadsheet
const tabs = await api.get(`/api/google/tools/sheets/${selectedId}/tabs`);
// → [ { sheetId: 0, title: "Leads", index: 0, rowCount: 1000, columnCount: 26 }, ... ]

// 3. Save selection as campaign tool params
await api.put(`/campaigns/${campaignId}/tool-params`, {
  toolParams: [{
    toolId: googleSheetsToolId,
    params: JSON.stringify({
      spreadsheetId: selectedSpreadsheet.id,
      sheetName:     selectedTab.title,
      range:         `${selectedTab.title}!A:Z`
    })
  }]
});
```

### Calendar picker

```typescript
const calendars = await api.get('/api/google/tools/calendar/calendars');
// → [ { id: "primary", summary: "John's Calendar", primary: true }, ... ]
```

---

## 13. Verification Checklist

- [ ] `GET /api/google/tools/sheets` returns list of spreadsheets for connected user
- [ ] `GET /api/google/tools/sheets/{id}/tabs` returns tab list for a valid spreadsheet
- [ ] `GET /api/google/tools/sheets/{id}/preview?range=Sheet1!A1:C3` returns 2D cell values
- [ ] `GET /api/google/tools/calendar/calendars` returns calendar list
- [ ] `GET /api/google/tools/drive/folders` returns top-level Drive folders
- [ ] `GET /api/google/tools/drive/files?folderId={id}` returns files in that folder
- [ ] `GET /api/google/tools/gmail/labels` returns Gmail labels
- [ ] Unlinked account → 400 with clear error message
- [ ] Insufficient scope → 403 with scope name in message
- [ ] All endpoints require JWT authentication
