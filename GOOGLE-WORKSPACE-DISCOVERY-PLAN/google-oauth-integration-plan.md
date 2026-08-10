# Google OAuth Integration Plan

> **Last updated:** 2026-03-15
> **Goal:** Allow users to link their Google account to the platform so their
> OAuth tokens are automatically used when agent tools require Google APIs
> (Google Sheets, Google Drive, Gmail, Calendar, etc.).

---

## 1. Current State

### Already built

| Component | File | Status |
|---|---|---|
| `GoogleOAuthToken` entity | `entities/GoogleOAuthToken.java` | ✅ Complete |
| `GoogleOAuthTokenService` | `services/GoogleOAuthTokenService.java` | ✅ CRUD complete |
| `GoogleOAuthTokenRepository` | `repositories/GoogleOAuthTokenRepository.java` | ✅ Complete |
| `GoogleOAuthTokenController` | `controllers/GoogleOAuthTokenController.java` | ✅ Manual token save |
| `ToolAuthMethod.OAUTH` enum value | `enumeration/ToolAuthMethod.java` | ✅ Defined |
| `Tool.oauthScopes` field | `entities/Tool.java` | ✅ JSONB field |
| `McpToolExecutor` — OAUTH branch | `brain/McpToolExecutor.java` | ⚠️ Uses connectionConfig only |

### Does NOT exist yet — must be built

| Component | Purpose |
|---|---|
| `GoogleOAuthController` — authorize + callback | Full OAuth2 Authorization Code Flow (redirect → Google → callback → save tokens) |
| `GoogleOAuthService` — code exchange + refresh | Exchange auth code for tokens; refresh expired access tokens via refresh token |
| `GoogleTokenRefreshScheduler` | `@Scheduled` job that auto-refreshes tokens expiring soon |
| `GoogleSheetsService` | Native Google Sheets API v4 client (read / append / update) |
| `McpToolExecutor` OAUTH fix | Fetch owner's live access token instead of reading from static connectionConfig |

---

## 2. Architecture Overview

```
  User (browser / mobile)
         │
         │  1. Click "Connect Google Account"
         ▼
  Frontend redirects to:
  GET /api/google/oauth/authorize?redirect_uri=<frontend_callback_page>
         │
         │  2. Backend generates Google OAuth2 URL with state
         ▼
  Google OAuth2 consent screen (accounts.google.com)
         │
         │  3. User grants scopes
         ▼
  Google redirects to:
  GET /api/google/oauth/callback?code=...&state=...
         │
         │  4. Backend exchanges code → access_token + refresh_token
         │  5. Saves to GoogleOAuthToken (ownerId → tokens)
         │  6. Redirects frontend to success page
         ▼
  Token stored in DB
         │
         │  7. Agent tool runs (McpToolExecutor)
         │     authMethod = OAUTH
         │     → load GoogleOAuthToken for this ownerId
         │     → auto-refresh if expiring
         │     → inject access_token as Bearer header
         ▼
  Google API (Sheets / Drive / Calendar / etc.)
```

---

## 3. Google OAuth2 App Setup (prerequisites)

Before any code runs, the following must be configured in **Google Cloud Console**:

| Setting | Value |
|---|---|
| OAuth 2.0 Client ID | `${GOOGLE_OAUTH_CLIENT_ID}` |
| OAuth 2.0 Client Secret | `${GOOGLE_OAUTH_CLIENT_SECRET}` |
| Authorised Redirect URI | `https://sawtia.ma/api/google/oauth/callback` |
| Scopes | `openid email profile` + per-tool scopes (e.g. `https://www.googleapis.com/auth/spreadsheets`) |

Properties in `application-dev.properties`:

```properties
# Google OAuth2
google.oauth.client-id=${GOOGLE_OAUTH_CLIENT_ID:}
google.oauth.client-secret=${GOOGLE_OAUTH_CLIENT_SECRET:}
google.oauth.redirect-uri=${GOOGLE_OAUTH_REDIRECT_URI:http://localhost:4200/google/callback}
google.oauth.token-url=https://oauth2.googleapis.com/token
google.oauth.auth-url=https://accounts.google.com/o/oauth2/v2/auth
```

---

## 4. Phase A — OAuth2 Authorization Code Flow

### A.1 — `GoogleOAuthProperties` (NEW — config bean)

**File:** `src/main/java/senseiprod/sawtia/phoneAgent/config/GoogleOAuthProperties.java`

```java
@ConfigurationProperties(prefix = "google.oauth")
@Component
@Getter @Setter
public class GoogleOAuthProperties {
    private String clientId;
    private String clientSecret;
    private String redirectUri;
    private String tokenUrl   = "https://oauth2.googleapis.com/token";
    private String authUrl    = "https://accounts.google.com/o/oauth2/v2/auth";
}
```

---

### A.2 — `GoogleOAuthService` (NEW)

**File:** `src/main/java/senseiprod/sawtia/phoneAgent/services/GoogleOAuthService.java`

Handles all communication with Google's OAuth2 endpoints.

```java
@Service
@RequiredArgsConstructor
public class GoogleOAuthService {

    private final GoogleOAuthProperties props;
    private final GoogleOAuthTokenService tokenService;
    private final ObjectMapper objectMapper;

    /**
     * Builds the Google consent-screen URL for a given owner.
     * The ownerId is encoded in the state parameter (HMAC-signed) so the
     * callback can identify whose token to save without needing a session.
     *
     * @param ownerId      the authenticated user's owner ID
     * @param frontendUri  where to redirect the user after successful auth
     * @param scopes       OAuth2 scopes to request (space-separated)
     * @return full Google OAuth2 authorization URL
     */
    public String buildAuthorizationUrl(String ownerId,
                                        String frontendUri,
                                        List<String> scopes) { ... }

    /**
     * Exchanges the authorization code for access + refresh tokens.
     * Saves the result to GoogleOAuthToken via GoogleOAuthTokenService.
     *
     * @param code    authorization code from Google callback
     * @param state   signed state param (contains ownerId + frontendUri)
     * @return the saved GoogleOAuthToken
     */
    public GoogleOAuthToken exchangeCode(String code, String state) { ... }

    /**
     * Refreshes an expired access token using the stored refresh token.
     * Updates the GoogleOAuthToken record with the new access_token + expiry.
     *
     * @param ownerId owner whose token needs refresh
     * @return updated access token string
     */
    public String refreshAccessToken(String ownerId) { ... }

    /**
     * Returns a valid access token for ownerId.
     * Auto-refreshes if expiring within 5 minutes.
     *
     * @param ownerId owner whose token is needed
     * @return live access token
     * @throws IllegalStateException if no ACTIVE token exists
     */
    public String getValidAccessToken(String ownerId) { ... }

    /**
     * Decodes the HMAC-signed state parameter and extracts ownerId.
     */
    public StatePayload decodeState(String state) { ... }

    /** Builds the state token: base64(json({ownerId, frontendUri})) + HMAC. */
    private String encodeState(String ownerId, String frontendUri) { ... }

    public record StatePayload(String ownerId, String frontendUri) {}
}
```

**Token exchange — HTTP call to `https://oauth2.googleapis.com/token`:**

```
POST https://oauth2.googleapis.com/token
Content-Type: application/x-www-form-urlencoded

code=<auth_code>
&client_id=<clientId>
&client_secret=<clientSecret>
&redirect_uri=<redirectUri>
&grant_type=authorization_code

Response:
{
  "access_token": "ya29...",
  "refresh_token": "1//0...",
  "expires_in": 3599,
  "scope": "https://www.googleapis.com/auth/spreadsheets ...",
  "token_type": "Bearer"
}
```

**Token refresh — HTTP call:**

```
POST https://oauth2.googleapis.com/token
Content-Type: application/x-www-form-urlencoded

refresh_token=<storedRefreshToken>
&client_id=<clientId>
&client_secret=<clientSecret>
&grant_type=refresh_token

Response: same as above (no new refresh_token — keep the original)
```

---

### A.3 — `GoogleOAuthController` (NEW)

**File:** `src/main/java/senseiprod/sawtia/phoneAgent/controllers/GoogleOAuthController.java`

```java
@RestController
@RequestMapping("/api/google/oauth")
@RequiredArgsConstructor
public class GoogleOAuthController {

    private final GoogleOAuthService googleOAuthService;

    /**
     * Step 1 — Frontend calls this to get the Google consent URL.
     * Returns a redirect (302) to Google's consent screen.
     *
     * Query params:
     *   redirect_uri  — where to send the user after success (frontend page)
     *   scopes        — comma-separated scopes (optional; defaults to sheets)
     *
     * Auth: JWT required — ownerId extracted from token.
     */
    @GetMapping("/authorize")
    public ResponseEntity<Void> authorize(
            @RequestParam String redirectUri,
            @RequestParam(defaultValue = "https://www.googleapis.com/auth/spreadsheets") String scopes,
            Authentication authentication) {

        String ownerId = (String) authentication.getCredentials();
        List<String> scopeList = Arrays.asList(scopes.split(","));
        String url = googleOAuthService.buildAuthorizationUrl(ownerId, redirectUri, scopeList);
        return ResponseEntity.status(HttpStatus.FOUND)
                .location(URI.create(url))
                .build();
    }

    /**
     * Step 2 — Google redirects here after user grants consent.
     * Exchanges the code, saves tokens, then redirects to the frontend URI
     * embedded in the state parameter.
     *
     * No JWT required — this is a public callback endpoint (whitelisted in SecurityConfig).
     * Security is enforced via HMAC-signed state parameter.
     */
    @GetMapping("/callback")
    public ResponseEntity<Void> callback(
            @RequestParam String code,
            @RequestParam String state,
            @RequestParam(required = false) String error) {

        if (error != null) {
            // User denied consent or error occurred
            String frontendUri = googleOAuthService.decodeState(state).frontendUri();
            return ResponseEntity.status(HttpStatus.FOUND)
                    .location(URI.create(frontendUri + "?google_oauth=error&reason=" + error))
                    .build();
        }

        GoogleOAuthService.StatePayload payload = googleOAuthService.decodeState(state);
        googleOAuthService.exchangeCode(code, state);
        return ResponseEntity.status(HttpStatus.FOUND)
                .location(URI.create(payload.frontendUri() + "?google_oauth=success"))
                .build();
    }

    /**
     * GET /status — Returns the current OAuth link status for the owner.
     * Used by the frontend to show "Connected as user@gmail.com" or "Not connected".
     */
    @GetMapping("/status")
    public ResponseEntity<ApiResponse<GoogleOAuthStatusResponse>> status(
            Authentication authentication) { ... }
}
```

**Security config — add `/api/google/oauth/callback` to whitelist:**

```java
// In SecurityConfig.WHITE_LIST_URL:
"/api/google/oauth/callback",
```

---

### A.4 — `GoogleOAuthStatusResponse` DTO (NEW)

**File:** `src/main/java/senseiprod/sawtia/phoneAgent/dtos/google/GoogleOAuthStatusResponse.java`

```java
public record GoogleOAuthStatusResponse(
    boolean linked,
    String googleEmail,        // null if not linked
    GoogleOAuthTokenStatus status,
    List<String> grantedScopes,
    LocalDateTime tokenExpiry
) {}
```

---

## 5. Phase B — Token Auto-Refresh Scheduler

### B.1 — `GoogleTokenRefreshScheduler` (NEW)

**File:** `src/main/java/senseiprod/sawtia/phoneAgent/services/GoogleTokenRefreshScheduler.java`

```java
@Component
@RequiredArgsConstructor
@Slf4j
public class GoogleTokenRefreshScheduler {

    private final GoogleOAuthTokenService tokenService;
    private final GoogleOAuthService      googleOAuthService;

    /**
     * Runs every 5 minutes. Finds all ACTIVE tokens expiring within
     * 10 minutes and refreshes them proactively.
     * This prevents live tool calls from failing due to an expired token.
     */
    @Scheduled(fixedDelay = 300_000) // 5 minutes
    public void refreshExpiringTokens() {
        LocalDateTime threshold = LocalDateTime.now().plusMinutes(10);
        List<GoogleOAuthToken> expiring = tokenService.getTokensExpiringBefore(threshold);

        for (GoogleOAuthToken token : expiring) {
            try {
                googleOAuthService.refreshAccessToken(token.getOwnerId());
                log.info("[GoogleTokenRefresh] Refreshed token for owner={}",
                        token.getOwnerId());
            } catch (Exception ex) {
                log.error("[GoogleTokenRefresh] Failed to refresh for owner={}: {}",
                        token.getOwnerId(), ex.getMessage());
                // Mark as ERROR so the owner knows re-auth is needed
                tokenService.markError(token.getOwnerId());
            }
        }
    }
}
```

---

## 6. Phase C — Token Injection in McpToolExecutor

### C.1 — Problem

Currently `McpToolExecutor.execute()` reads the Bearer token from
`agentTool.connectionConfig` for `OAUTH` auth method. This is a static value set
at tool-agent binding time and does not reflect the live Google access token.

### C.2 — Fix: add `ownerId` parameter + fetch live token

**File:** `src/main/java/senseiprod/sawtia/phoneAgent/brain/McpToolExecutor.java`

Change the method signature to accept `ownerId`:

```java
// Before:
public String execute(Tool tool, AgentTool agentTool,
                      Map<String, Object> llmArguments,
                      String campaignToolParams)

// After:
public String execute(Tool tool, AgentTool agentTool,
                      Map<String, Object> llmArguments,
                      String campaignToolParams,
                      String ownerId)               // ← ADD
```

In the `OAUTH` branch of auth header assembly:

```java
case OAUTH -> {
    // Fetch live token for this owner (auto-refreshes if expiring)
    String accessToken = googleOAuthService.getValidAccessToken(ownerId);
    request.header("Authorization", "Bearer " + accessToken);
}
```

### C.3 — Scope check before execution

Before making the HTTP call, validate that the owner has the required scopes:

```java
if (tool.getAuthMethod() == ToolAuthMethod.OAUTH) {
    List<String> required = parseScopes(tool.getOauthScopes());
    if (!required.isEmpty() && !googleOAuthTokenService.hasRequiredScopes(ownerId, required)) {
        return "Error: Google account not connected or missing required scopes: "
                + String.join(", ", required)
                + ". Ask the user to reconnect their Google account.";
    }
}
```

### C.4 — Update `LlmBrainService` to pass `ownerId`

`LlmBrainService.think()` already has access to `agent.getOwnerId()`. Pass it to
the tool executor call:

```java
// In the tool-calling section of think():
String toolResult = mcpToolExecutor.execute(
        tool, agentTool, arguments, campaignToolParamsJson,
        request.agent().getOwnerId());   // ← ADD ownerId
```

---

## 7. Phase D — Google Sheets Service

### D.1 — `GoogleSheetsService` (NEW)

**File:** `src/main/java/senseiprod/sawtia/phoneAgent/services/GoogleSheetsService.java`

Wraps the Google Sheets API v4 REST endpoints directly via HTTP (no SDK required —
the `google-api-services-sheets` SDK is large; plain HTTP with the owner's OAuth
token keeps the build lightweight).

```java
@Service
@RequiredArgsConstructor
public class GoogleSheetsService {

    private final GoogleOAuthService googleOAuthService;

    private static final String SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets";

    /**
     * Reads a range from a spreadsheet.
     *
     * @param ownerId       the owner whose Google token is used
     * @param spreadsheetId Google Sheets document ID (from the URL)
     * @param range         A1-notation range, e.g. "Sheet1!A1:D10"
     * @return list of rows, each row is a list of cell values (as strings)
     */
    public List<List<String>> readRange(String ownerId,
                                        String spreadsheetId,
                                        String range) { ... }

    /**
     * Appends a row to a sheet.
     *
     * @param ownerId       owner's Google token
     * @param spreadsheetId sheet document ID
     * @param range         target range (e.g. "Sheet1!A1") — Google appends after last row
     * @param values        row values to append
     */
    public void appendRow(String ownerId, String spreadsheetId,
                          String range, List<Object> values) { ... }

    /**
     * Updates a specific cell or range.
     *
     * @param ownerId       owner's Google token
     * @param spreadsheetId sheet document ID
     * @param range         A1-notation target range
     * @param values        2D array of values [[row1col1, row1col2], [row2col1, ...]]
     */
    public void updateRange(String ownerId, String spreadsheetId,
                            String range, List<List<Object>> values) { ... }

    /**
     * Returns spreadsheet metadata (sheet names, dimensions).
     */
    public Map<String, Object> getMetadata(String ownerId, String spreadsheetId) { ... }
}
```

**API calls use the owner's live access token:**

```
GET  https://sheets.googleapis.com/v4/spreadsheets/{id}/values/{range}
     Authorization: Bearer <owner_access_token>

POST https://sheets.googleapis.com/v4/spreadsheets/{id}/values/{range}:append
     ?valueInputOption=USER_ENTERED
     Authorization: Bearer <owner_access_token>
     { "values": [[...]] }

PUT  https://sheets.googleapis.com/v4/spreadsheets/{id}/values/{range}
     ?valueInputOption=USER_ENTERED
     Authorization: Bearer <owner_access_token>
     { "values": [[...]] }
```

Required OAuth scope: `https://www.googleapis.com/auth/spreadsheets`

---

## 8. Phase E — Tool Definition for Google Sheets (MCP Tool record)

A `Tool` record in the DB links the Google Sheets service to agents.
The tool can either point to an external MCP server **or** to a built-in
endpoint on this service.

### Option 1 — Built-in pseudo-MCP endpoint (recommended)

Add an internal controller that exposes Google Sheets operations as a
JSON-RPC 2.0 endpoint that `McpToolExecutor` can call:

**File:** `src/main/java/senseiprod/sawtia/phoneAgent/controllers/GoogleSheetsToolController.java`

```
POST /api/internal/tools/google-sheets
Authorization: Bearer <internal-tool-secret>

{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "google_sheets_read",
    "arguments": {
      "spreadsheetId": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
      "range": "Sheet1!A1:D10",
      "ownerId": "uuid-of-owner"
    }
  }
}
```

Tool record in DB:

```sql
INSERT INTO tools (name, description, mcp_url, auth_method, oauth_scopes, min_tier, active)
VALUES (
  'Google Sheets Reader',
  'Read data from a Google Sheets spreadsheet owned by the user',
  'http://localhost:8083/api/internal/tools/google-sheets',
  'OAUTH',
  '["https://www.googleapis.com/auth/spreadsheets.readonly"]',
  'STANDARD',
  true
);
```

### Option 2 — External MCP server (future)

For production, `mcpUrl` points to a dedicated Node.js MCP server that wraps
Google Sheets. The `OAUTH` token from `GoogleOAuthService.getValidAccessToken()`
is still passed as Bearer.

---

## 9. Security — State Parameter Protection

The OAuth callback endpoint is public (no JWT). The state parameter must be
HMAC-signed to prevent CSRF and token-injection attacks.

```
state = base64url(JSON({ownerId, frontendUri, nonce})) + "." + HMAC_SHA256(payload, secretKey)
```

- `secretKey` = `${GOOGLE_OAUTH_STATE_SECRET}` (env var, ≥32 bytes)
- On callback: verify HMAC before processing; reject if invalid

**Property:**
```properties
google.oauth.state-secret=${GOOGLE_OAUTH_STATE_SECRET:dev-state-secret-change-in-prod}
```

---

## 10. New Files — Complete List

### New files to create

| File | Phase | Purpose |
|---|---|---|
| `config/GoogleOAuthProperties.java` | A | Config bean for client-id, secret, redirect-uri |
| `services/GoogleOAuthService.java` | A | Code exchange, token refresh, state signing |
| `controllers/GoogleOAuthController.java` | A | `/authorize` redirect + `/callback` handler |
| `dtos/google/GoogleOAuthStatusResponse.java` | A | Status DTO for frontend |
| `services/GoogleTokenRefreshScheduler.java` | B | `@Scheduled` proactive token refresh |
| `services/GoogleSheetsService.java` | D | Google Sheets API v4 HTTP client |
| `controllers/GoogleSheetsToolController.java` | E | Internal MCP-compatible tool endpoint |

### Modified files

| File | Phase | Change |
|---|---|---|
| `brain/McpToolExecutor.java` | C | Add `ownerId` param; fetch live token for OAUTH; scope check |
| `brain/LlmBrainService.java` | C | Pass `agent.getOwnerId()` to `mcpToolExecutor.execute()` |
| `services/GoogleOAuthTokenService.java` | B | Add `markError(ownerId)` method |
| `config/SecurityConfig.java` | A | Whitelist `/api/google/oauth/callback` |
| `resources/application-dev.properties` | A | Add `google.oauth.*` properties |

---

## 11. Data Flow — Full OAuth + Tool Execution Sequence

```
[User in frontend]
        │
        │  1. GET /api/google/oauth/authorize
        │     ?redirect_uri=https://app.sawtia.ma/settings/google
        │     ?scopes=https://www.googleapis.com/auth/spreadsheets
        │     Authorization: Bearer <jwt>
        ▼
[GoogleOAuthController.authorize()]
        │  builds Google consent URL with signed state
        │  302 redirect →
        ▼
[Google accounts.google.com]
        │  User logs in + grants consent
        │  302 redirect →
        ▼
[GET /api/google/oauth/callback?code=...&state=...]
        │
[GoogleOAuthController.callback()]
        ├── verify HMAC on state
        ├── GoogleOAuthService.exchangeCode(code, state)
        │     POST https://oauth2.googleapis.com/token
        │     ← {access_token, refresh_token, expires_in, scope}
        │     GoogleOAuthTokenService.saveOrRefreshToken(ownerId, ...)
        │     DB: GoogleOAuthToken {ownerId, accessToken, refreshToken,
        │                           tokenExpiry, scopes=["...sheets..."],
        │                           status=ACTIVE}
        └── 302 redirect → https://app.sawtia.ma/settings/google?google_oauth=success

[Later — Agent tool call during a phone/WA/widget conversation]
        │
[LlmBrainService.think()]
        │  LLM responds with tool_use: google_sheets_read
        │
[McpToolExecutor.execute(tool, agentTool, args, params, ownerId)]
        ├── authMethod == OAUTH
        ├── hasRequiredScopes(ownerId, ["...spreadsheets..."]) → true
        ├── GoogleOAuthService.getValidAccessToken(ownerId)
        │     → tokenExpiry < now+5min?
        │          YES → POST /token with refresh_token → new accessToken
        │          NO  → return stored accessToken
        └── HTTP POST to tool.mcpUrl
            Authorization: Bearer <live_access_token>
            → JSON-RPC 2.0 call
            → Google Sheets API executes
            ← result returned to LLM
```

---

## 12. Verification Checklist

### Phase A — Authorization Flow
- [ ] `GET /api/google/oauth/authorize` returns 302 to Google consent screen
- [ ] State parameter contains ownerId encoded and HMAC-signed
- [ ] `GET /api/google/oauth/callback` exchanges code successfully
- [ ] `GoogleOAuthToken` record created in DB with correct scopes + expiry
- [ ] Callback endpoint is public (no JWT required) in SecurityConfig
- [ ] User denied consent → redirected to frontend with `?google_oauth=error`
- [ ] `GET /api/google/oauth/status` returns `linked=true` with email after auth

### Phase B — Token Refresh
- [ ] `GoogleTokenRefreshScheduler` runs every 5 minutes
- [ ] Tokens expiring within 10 minutes are refreshed proactively
- [ ] Failed refresh marks token as `ERROR` status
- [ ] `GoogleOAuthService.getValidAccessToken()` auto-refreshes inline as fallback

### Phase C — McpToolExecutor
- [ ] `execute()` accepts `ownerId` parameter
- [ ] OAUTH branch fetches live token from `GoogleOAuthService`
- [ ] Missing scopes return a human-readable error (not an exception)
- [ ] `LlmBrainService.think()` passes `agent.getOwnerId()` to executor

### Phase D — Google Sheets Service
- [ ] `readRange()` returns correct cell values from a real spreadsheet
- [ ] `appendRow()` adds a row to the sheet
- [ ] Uses the owner's access token (not a service account)
- [ ] Returns a friendly error string when token is invalid

### Phase E — Tool Record
- [ ] Google Sheets `Tool` record exists in DB with `authMethod=OAUTH`
- [ ] `oauthScopes` contains the correct Sheets scope
- [ ] Agent with `AgentTool` linked to the Sheets tool can call it from a conversation
