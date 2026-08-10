# Google Tools — End-to-End User Flow

> **Date:** 2026-03-24
> **Branch:** `mcp_tool`
> **Purpose:** Define the complete user journey from Google account connection → agent tool selection → campaign resource configuration, mapping every step to existing backend code and flagging what still needs to be built.

> ## ⚠️ 2026-06 Security Update — read this first
>
> This document predates two changes. Where it conflicts with the items below,
> **the items below win**:
>
> 1. **Resource binding model.** The per-resource config has moved to reusable
>    **`ToolDataSource`** records (`/api/sawtia/v1/tool-data-sources`) linked to
>    an agent's tools, in addition to the legacy `campaign_tool_params` path
>    described in Stage 3. A data source now also carries an **`accessRole`**
>    (`REFERENCE` = read-only, `DATA_SINK` = append-only).
> 2. **The runtime is now hard-gated.** An untrusted conversational user can no
>    longer steer the LLM into arbitrary Google operations. Resource ids are
>    force-injected from the owner's binding (never the LLM); reads are gated by
>    `accessRole`; sinks are append-only with formula injection neutralised;
>    calendar exposes availability (FreeBusy) + own-conversation cancel only;
>    Gmail is send-to-self with header-injection sanitised; `/mcp/google`
>    requires the internal Bearer secret.
>    The `ownerId`-injection gap this doc flags as "🔴 HIGH to build" is **done**
>    (forced server-side in `McpToolExecutor`).
>
> **Authoritative now:**
> - Security model & runtime gate → [../mcp/04_GOOGLE_HANDLERS.md](../mcp/04_GOOGLE_HANDLERS.md)
> - Frontend: attaching a data source + `accessRole` → [FRONTEND-GOOGLE-DATA-SOURCE-GUIDE.md](FRONTEND-GOOGLE-DATA-SOURCE-GUIDE.md)
>
> The connection journey (Stage 1) and the overall shape below remain accurate.

---

## Overview: The 3-Stage Journey

```
┌─────────────────────┐     ┌──────────────────────────┐     ┌────────────────────────────┐
│   STAGE 1           │     │   STAGE 2                │     │   STAGE 3                  │
│   Connect Google    │────▶│   Create Agent +         │────▶│   Create Campaign +        │
│   Account (OAuth)   │     │   Select Tools           │     │   Pick Specific Resource   │
│                     │     │   (Sheets, Calendar…)    │     │   (Sheet / Tab / File)     │
└─────────────────────┘     └──────────────────────────┘     └────────────────────────────┘
       Once per user                Per agent (ADVANCED/EXPERT)        Per campaign
```

---

## Stage 1 — Google Account Connection (OAuth2)

### User Action
User goes to their profile/settings page and clicks "Connect Google Account". They grant permissions (Drive, Sheets, Calendar, Gmail), and the system saves their token.

### Step-by-Step Flow

```
Frontend                          Backend                              Google
────────                          ───────                              ──────
1. User clicks "Connect Google"
   ↓
2. GET /api/sawtia/v1/google/oauth/authorize
   ?redirectUri=<frontend-url>
   &scopes=drive.readonly,spreadsheets,...

   [JWT required]
                                  3. GoogleOAuthService.buildAuthorizationUrl()
                                     - Generates HMAC-signed state:
                                       base64url({ownerId, frontendUri, nonce}) + hmac
                                     - Returns JSON: { "authorizationUrl": "<google-url>" }
   ↓
4. Frontend redirects user to authorizationUrl
                                                         5. Google consent screen
                                                            shown to user
                                                         ↓
                                                         6. User grants permissions
                                                         ↓
7. Google redirects to:
   GET /api/sawtia/v1/google/oauth/callback?code=...&state=...
   [PUBLIC endpoint — no JWT needed]
                                  8. GoogleOAuthService.exchangeCode(code, state)
                                     - HMAC-verify state → extract ownerId + frontendUri
                                     - POST to https://oauth2.googleapis.com/token
                                     - Receive: access_token, refresh_token, scope, expiry
                                     ↓
                                  9. GoogleOAuthTokenService.saveOrRefreshToken()
                                     - Persist GoogleOAuthToken entity:
                                       ownerId, email, accessToken, refreshToken,
                                       tokenExpiry, grantedScopes, status=ACTIVE
                                     - Reject if email already linked to different owner
                                     ↓
                                  10. Redirect browser to frontendUri?connected=true
   ↓
11. Frontend shows "Google Connected ✓"
```

### What Is Implemented ✅

| Component | File | Status |
|-----------|------|--------|
| Authorize endpoint | `GoogleOAuthController.authorize()` | ✅ Done |
| Callback endpoint (public) | `GoogleOAuthController.callback()` | ✅ Done |
| Token status endpoint | `GET /google/oauth/status` | ✅ Done |
| Token revoke | `DELETE /google/oauth/revoke` | ✅ Done |
| HMAC state signing | `GoogleOAuthService.encodeState()` | ✅ Done |
| Code exchange | `GoogleOAuthService.exchangeCode()` | ✅ Done |
| Token auto-refresh | `GoogleOAuthService.getValidAccessToken()` | ✅ Done — auto-refreshes if expiring within 5 min |
| Token persistence | `GoogleOAuthTokenService` + `GoogleOAuthToken` entity | ✅ Done |
| Scope validation | `GoogleOAuthTokenService.hasRequiredScopes()` | ✅ Done |
| Cascade disable on revoke | `AgentToolService.disableOAuthToolsForOwner()` | ✅ Done |

### Gaps / Frontend Contract Needed

| Gap | Description |
|-----|-------------|
| **Scope list** | Frontend must know which scopes to request. Define a fixed required-scope list per tool type. |
| **Scope mismatch UI** | If user already connected but missing a scope (e.g., connects Sheets-only, then picks a Calendar tool), backend returns scope error — frontend must show "re-authorize to grant Calendar access". |
| **Status polling** | Frontend needs to poll `GET /google/oauth/status` after redirect to confirm connection before enabling tool selection. |

---

## Stage 2 — Create ADVANCED / EXPERT Agent + Select Tools

### User Action
User creates a new agent, selects ADVANCED or EXPERT tier, then picks which tools the agent can use (e.g., "Google Sheets", "Google Calendar").

### Step-by-Step Flow

```
Frontend                          Backend
────────                          ───────

1. User starts "Create Agent" form, picks ADVANCED tier

2. Frontend loads tool catalog:
   GET /api/sawtia/v1/tools
   [JWT required]
                                  3. ToolService.listActiveTools()
                                     - Returns all active tools where minTier ≤ ADVANCED
                                     - Response: List<ToolCatalogResponse>
                                       { id, name, description, minTier, authMethod, inputSchema }
   ↓
4. Frontend shows tool picker:
   ☐ Google Sheets   (authMethod: OAUTH)
   ☐ Google Calendar (authMethod: OAUTH)
   ☐ REST Webhook    (authMethod: API_KEY)
   ...

   For OAUTH tools → frontend checks Google connection status:
   GET /api/sawtia/v1/google/oauth/status
   If not connected → show warning "Connect Google first"

5. User selects tools, fills agent name/instructions, submits:
   POST /api/sawtia/v1/agents/advanced
   {
     "name": "Sales Agent",
     "instructions": "...",
     "toolIds": ["<sheets-tool-id>", "<calendar-tool-id>"]
   }
                                  6. AgentWorkflowController.createAdvancedAgent()
                                     - Create Agent (tier=ADVANCED)
                                     - AgentToolService.replaceAgentTools(agentId, tools)
                                       → Creates AgentTool rows:
                                         { agentId, toolId, enabled=true,
                                           connectionConfig={},
                                           invokeOnConversationEnd=false }
                                     - For each OAUTH tool: validate owner has required scopes
                                       (GoogleOAuthTokenService.hasRequiredScopes())
                                       → Return warning if scope missing (tool disabled, not error)
   ↓
7. Agent created. Frontend shows tool list on agent detail page:
   ✅ Google Sheets — enabled
   ✅ Google Calendar — enabled

   User can later:
   PATCH /api/sawtia/v1/agents/{id}/tools/{agentToolId}
   { "enabled": false }   → disable a tool
   { "invokeOnConversationEnd": true }  → auto-call at end of call/chat
```

### Key Data Stored in `AgentTool`

```json
{
  "agentId": "<agent-uuid>",
  "toolId": "<sheets-tool-uuid>",
  "enabled": true,
  "connectionConfig": {},
  "invokeOnConversationEnd": false,
  "outputMapping": null
}
```

> `connectionConfig` is intentionally empty here — the specific sheet/file is NOT set at the agent level. It is set per campaign (Stage 3).

### What Is Implemented ✅

| Component | File | Status |
|-----------|------|--------|
| Tool catalog endpoint | `ToolController.listActiveTools()` | ✅ Done |
| Tier filtering | `ToolService.getAvailableToolsForTier()` | ✅ Done |
| Agent creation (advanced) | `AgentWorkflowController.createAdvancedAgent()` | ✅ Done |
| Tool attachment | `AgentToolService.replaceAgentTools()` | ✅ Done |
| `AgentTool` entity | `entities/AgentTool.java` | ✅ Done |
| OAUTH scope guard | `GoogleOAuthTokenService.hasRequiredScopes()` | ✅ Done |
| OAuth tool cascade disable | `AgentToolService.disableOAuthToolsForOwner()` | ✅ Done |

### Gaps

| Gap | File to Modify | Notes |
|-----|---------------|-------|
| **`toolIds` in agent creation DTO** | `dtos/agent/CreateAdvancedAgentRequest.java` | Need to confirm `toolIds: List<UUID>` field is in the request DTO and wired to `AgentToolService.replaceAgentTools()` |
| **Scope check on tool attachment** | `AgentToolService.attachTool()` | Add soft-warning (not hard error) if OAUTH tool attached and owner hasn't granted required scopes |
| **Tool list on agent detail** | — | `GET /agents/{id}` response should include `agentTools` summary — verify `AgentToolSummaryResponse` is returned |

---

## Stage 3 — Create Campaign + Configure Specific Google Resource

### User Action
User creates a campaign for the "Sales Agent". The agent uses Google Sheets. The user must now pick **which specific spreadsheet** (and which tab) this campaign will read/write.

### Step-by-Step Flow

```
Frontend                          Backend                         Google APIs
────────                          ───────                         ──────────

1. User starts "Create Campaign", selects agent "Sales Agent"

2. Frontend loads agent's tools:
   GET /api/sawtia/v1/agents/{agentId}/tools
                                  3. AgentToolService.listAgentTools(agentId, ownerId)
                                     → Returns List<AgentToolDetailResponse>:
                                       [{ toolId, toolName: "Google Sheets",
                                          authMethod: OAUTH,
                                          inputSchema: { ... },
                                          enabled: true }]
   ↓
4. Frontend sees "Google Sheets" tool is attached.
   For OAUTH tools, shows a resource picker.

   ── SPREADSHEET PICKER ──────────────────────────────────────
   5. GET /api/sawtia/v1/google/tools/sheets
      [JWT required — uses caller's OAuth token]
                                  6. GoogleToolDiscoveryService.listSpreadsheets(ownerId)
                                     - Calls Google Drive API v3:
                                       GET https://www.googleapis.com/drive/v3/files
                                       ?q=mimeType='application/vnd.google-apps.spreadsheet'
                                       &fields=files(id,name,modifiedTime)
                                     - Token auto-refreshed via GoogleOAuthService
                                     → Returns: List<SpreadsheetSummary>
                                       [{ id, name, modifiedTime }]
   ↓
   User sees:
   ○ Sales Leads Q1 2026    (id: 1ABC...)
   ○ Customer DB            (id: 2DEF...)
   ○ Campaign Results       (id: 3GHI...)

   7. User picks "Sales Leads Q1 2026"

   ── TAB PICKER ──────────────────────────────────────────────
   8. GET /api/sawtia/v1/google/tools/sheets/{spreadsheetId}/tabs
                                  9. GoogleToolDiscoveryService.listTabs(ownerId, spreadsheetId)
                                     - Calls Sheets API v4:
                                       GET https://sheets.googleapis.com/v4/spreadsheets/{id}
                                       ?fields=sheets.properties(sheetId,title,index)
                                     → Returns: List<SheetTabSummary>
                                       [{ sheetId, title, index }]
   ↓
   User sees:
   ○ Contacts    (index: 0)
   ○ Results     (index: 1)
   ○ Archive     (index: 2)

   10. User picks "Contacts"

   ── PREVIEW (OPTIONAL) ──────────────────────────────────────
   11. GET /api/sawtia/v1/google/tools/sheets/{spreadsheetId}/preview
       ?range=Contacts!A1:E5
                                  12. GoogleToolDiscoveryService.previewRange(ownerId, id, range)
                                      → Returns: List<List<String>> (cell values)
   ↓
   User sees first 5 rows of "Contacts" tab as preview table.

   ── SAVE CAMPAIGN TOOL PARAMS ───────────────────────────────
   13. User submits campaign form with all fields + tool params.

   POST /api/sawtia/v1/campaigns
   {
     "agentId": "<agent-uuid>",
     "name": "Q1 Sales Calls",
     "type": "OUTBOUND",
     ...
     "toolParams": [
       {
         "toolId": "<sheets-tool-uuid>",
         "params": {
           "spreadsheetId": "1ABC...",
           "sheetName": "Contacts",
           "range": "Contacts!A:F",
           "ownerId": "<owner-id>"
         }
       }
     ]
   }
                                  14. CampaignService creates Campaign
                                      ↓
                                  15. CampaignToolParamsService.saveAll(toolParams, campaignId, agentId)
                                      For each toolParam:
                                      a. Verify toolId belongs to agent (is in agent_tools)
                                      b. Load tool.inputSchema
                                      c. Validate params against inputSchema (JSON Schema validation)
                                      d. Persist CampaignToolParams:
                                         { campaignId, toolId, params: <JSONB> }
   ↓
   Campaign created ✅
```

### What Gets Stored in `CampaignToolParams`

```json
{
  "campaignId": "<campaign-uuid>",
  "toolId": "<sheets-tool-uuid>",
  "params": {
    "spreadsheetId": "1ABC...",
    "sheetName": "Contacts",
    "range": "Contacts!A:F",
    "ownerId": "<owner-id>"
  }
}
```

### How These Params Are Used at Runtime

When a call/chat turn fires and the LLM decides to use the Sheets tool:

```
LlmBrainService.think()
  │
  ├─ BrainRequest.campaignToolParams → finds CampaignToolParams for Google Sheets tool
  │
  ├─ LLM decides to call "google_sheets_read"
  │   Response: { "tool": "google_sheets_read", "args": { "specificLookup": "John Smith" } }
  │
  └─ McpToolExecutor.execute(tool, agentTool, llmArgs, campaignParams, ownerId)
       ├─ Merge: campaign params (spreadsheetId, range) + llm args (specificLookup)
       │         → { spreadsheetId: "1ABC...", range: "Contacts!A:F", specificLookup: "John Smith" }
       ├─ Auth: OAUTH → GoogleOAuthService.getValidAccessToken(ownerId) → Bearer <live-token>
       ├─ POST to tool.mcpUrl:
       │    POST /api/sawtia/v1/internal/tools/google-sheets
       │    {
       │      "jsonrpc": "2.0",
       │      "method": "tools/call",
       │      "params": {
       │        "name": "google_sheets_read",
       │        "arguments": { spreadsheetId, range, ownerId, specificLookup }
       │      }
       │    }
       └─ GoogleSheetsToolController handles → GoogleSheetsService.readRange()
            → Calls Google Sheets API v4 with owner's token
            → Returns cell data as JSON-RPC result
```

### What Is Implemented ✅

| Component | File | Status |
|-----------|------|--------|
| Spreadsheet list endpoint | `GoogleToolDiscoveryController.listSpreadsheets()` | ✅ Done |
| Tab list endpoint | `GoogleToolDiscoveryController.listTabs()` | ✅ Done |
| Preview endpoint | `GoogleToolDiscoveryController.previewRange()` | ✅ Done |
| Campaign tool params save | `CampaignToolParamsService.saveAll()` | ✅ Done |
| inputSchema validation | `CampaignToolParamsService` (schema check) | ✅ Done |
| `CampaignToolParams` entity | `entities/CampaignToolParams.java` | ✅ Done |
| Internal MCP endpoint (Sheets) | `GoogleSheetsToolController` | ✅ Done |
| Sheets read/append/update | `GoogleSheetsService` | ✅ Done |
| Param merge at runtime | `McpToolExecutor.execute()` | ✅ Done |

### Gaps

| Gap | File to Modify/Create | Notes |
|-----|----------------------|-------|
| **`toolParams` field in campaign create DTO** | `dtos/campaign/CreateCampaignRequest.java` | Confirm `toolParams: List<ToolParamRequest>` is in request DTO |
| **`ownerId` auto-injection into params** | `CampaignToolParamsService.saveAll()` | The `ownerId` in params should be injected by backend (from JWT), not sent by frontend, to prevent impersonation |
| **`agentTools` endpoint on agent detail** | `AgentWorkflowController` or `AgentToolController` | Need `GET /agents/{id}/tools` that returns tools with `inputSchema` so frontend knows what fields to ask per tool |
| **Tool param update after campaign creation** | `CampaignToolParamsController` (if exists) | Need `PUT /campaigns/{id}/tool-params` to replace params later |
| **Scope enforcement on campaign save** | `CampaignToolParamsService` | Before saving, verify owner has scopes required by each OAUTH tool (`tool.oauthScopes`) |

---

## Complete Data Model (All 3 Stages)

```
GoogleOAuthToken
  ownerId (FK → User)
  email
  accessToken
  refreshToken
  tokenExpiry
  grantedScopes     ← ["drive.readonly", "spreadsheets", ...]
  status            ← ACTIVE / REVOKED / ERROR

      │
      │ owner connects Google (Stage 1)
      ▼

Tool (catalog — admin-managed)
  id
  name              ← "Google Sheets"
  description
  mcpUrl            ← "http://localhost/api/sawtia/v1/internal/tools/google-sheets"
  minTier           ← ADVANCED
  authMethod        ← OAUTH
  oauthScopes       ← ["spreadsheets", "drive.readonly"]
  inputSchema       ← { "spreadsheetId": "string", "range": "string", ... }
  active

      │
      │ user selects tool when creating agent (Stage 2)
      ▼

AgentTool
  agentId           ← FK → Agent (ADVANCED/EXPERT)
  toolId            ← FK → Tool
  enabled           ← true
  connectionConfig  ← {} (empty — specific resource set per campaign)
  invokeOnConversationEnd ← false / true
  outputMapping     ← null / { "contactName": "$.result.name" }

      │
      │ user picks specific resource when creating campaign (Stage 3)
      ▼

CampaignToolParams
  campaignId        ← FK → Campaign
  toolId            ← FK → Tool
  params            ← {
                       "spreadsheetId": "1ABC...",
                       "sheetName": "Contacts",
                       "range": "Contacts!A:F",
                       "ownerId": "<injected-by-backend>"
                     }
```

---

## Scope Requirements Per Tool Type

| Tool | Required Google Scopes | Discovery Endpoints Used |
|------|----------------------|-------------------------|
| Google Sheets (read) | `spreadsheets.readonly`, `drive.readonly` | `/google/tools/sheets`, `/sheets/{id}/tabs` |
| Google Sheets (write) | `spreadsheets` (read+write), `drive.readonly` | `/google/tools/sheets`, `/sheets/{id}/tabs` |
| Google Calendar | `calendar.readonly` | `/google/tools/calendar/calendars` |
| Google Drive | `drive.readonly` | `/google/tools/drive/folders`, `/drive/files` |
| Gmail | `gmail.readonly` | `/google/tools/gmail/labels` |

---

## API Endpoints Reference (All 3 Stages)

### Stage 1 — OAuth

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/api/sawtia/v1/google/oauth/authorize` | JWT | Get Google consent URL |
| `GET` | `/api/sawtia/v1/google/oauth/callback` | Public | Exchange code → save token |
| `GET` | `/api/sawtia/v1/google/oauth/status` | JWT | Check connection status |
| `DELETE` | `/api/sawtia/v1/google/oauth/revoke` | JWT | Revoke + disable OAUTH tools |

### Stage 2 — Agent + Tool Selection

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/api/sawtia/v1/tools` | JWT | List available tools (catalog) |
| `POST` | `/api/sawtia/v1/agents/advanced` | JWT | Create agent with tools |
| `POST` | `/api/sawtia/v1/agents/expert` | JWT | Create expert agent with tools |
| `GET` | `/api/sawtia/v1/agents/{id}/tools` | JWT | List agent's attached tools |
| `PATCH` | `/api/sawtia/v1/agents/{id}/tools/{atId}` | JWT | Update tool attachment |

### Stage 3 — Campaign + Resource Picker

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/api/sawtia/v1/google/tools/sheets` | JWT | List user's spreadsheets |
| `GET` | `/api/sawtia/v1/google/tools/sheets/{id}/tabs` | JWT | List tabs in a spreadsheet |
| `GET` | `/api/sawtia/v1/google/tools/sheets/{id}/preview` | JWT | Preview cell data |
| `GET` | `/api/sawtia/v1/google/tools/calendar/calendars` | JWT | List user's calendars |
| `GET` | `/api/sawtia/v1/google/tools/drive/folders` | JWT | List Drive folders |
| `GET` | `/api/sawtia/v1/google/tools/drive/files` | JWT | List files in folder |
| `POST` | `/api/sawtia/v1/campaigns` | JWT | Create campaign with toolParams |
| `PUT` | `/api/sawtia/v1/campaigns/{id}/tool-params` | JWT | Replace campaign tool params |

### Runtime (Internal)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/api/sawtia/v1/internal/tools/google-sheets` | API_KEY | MCP JSON-RPC for Sheets |

---

## What Needs to Be Built / Verified (Summary)

| Priority | Item | Description |
|----------|------|-------------|
| 🔴 HIGH | Verify `toolIds` in `CreateAdvancedAgentRequest` | Confirm tool IDs are accepted at agent creation and wired to `AgentToolService.replaceAgentTools()` |
| 🔴 HIGH | Verify `toolParams` in `CreateCampaignRequest` | Confirm `toolParams: List<ToolParamRequest>` is accepted at campaign creation |
| 🔴 HIGH | `ownerId` injection in campaign params | Backend should inject `ownerId` from JWT into `params` JSONB before saving (not trust frontend) |
| 🟡 MEDIUM | `GET /agents/{id}/tools` endpoint | Frontend needs this to know which tools an agent has and their `inputSchema` to build dynamic pickers |
| 🟡 MEDIUM | Scope check before tool attachment | Soft-warn when OAUTH tool attached without required scopes connected |
| 🟡 MEDIUM | Scope check before campaign param save | Hard-reject or warn if OAUTH tool used but owner hasn't granted required scopes |
| 🟡 MEDIUM | `PUT /campaigns/{id}/tool-params` endpoint | Allow updating tool params after campaign creation |
| 🟢 LOW | Preview in campaign picker | Already implemented in `previewRange()` — just wire to frontend |
| 🟢 LOW | Drive/Calendar pickers | Same pattern as Sheets — discovery endpoints exist, just need frontend pickers |
