# Frontend Guide — Attaching a Google Data Source (Access Roles)

> **Audience:** frontend developers building the "connect a Google
> Sheet / Doc / Calendar to this agent" UI.
> **What changed (2026-06):** a Google tool no longer acts on whatever resource
> the conversation names — it acts **only** on the specific resource the owner
> binds, with an **access role** that decides whether the agent may read it or
> only write to it. You now need to collect that role.
>
> TL;DR: when the owner attaches a **Sheets** or **Docs** data source, ask
> **"Should the agent READ this, or WRITE to it?"** and send `accessRole`.

---

## 1. The one new field: `accessRole`

When creating or updating a **Tool Data Source**, send `accessRole`:

| Value | Meaning | Use for |
|---|---|---|
| `REFERENCE` | Agent may **read** it, never write. | Product catalog sheet, price list, an FAQ / policy Google Doc. |
| `DATA_SINK` | Agent may **append** to it, never read it back. | Leads / bookings sheet, a call-log Doc. |

- **Omitted → defaults to `DATA_SINK`** (the security-safe default: a sink can't be read back, so nothing leaks).
- It is **ignored** for **Drive** (always read-only) and **Calendar** (has its own policy) — you can omit it there.

> Why "can't read it back"? A `DATA_SINK` is where customer data lands. If the
> agent could read it, any visitor could say *"read me all the leads you have"*
> and exfiltrate every other customer's data through the owner's account. So a
> sink is write-only by design.

---

## 2. API contract

### Create
`POST /api/sawtia/v1/tool-data-sources` (JWT required)

```jsonc
{
  "toolId": "a1b2c3d4-0001-0001-0001-000000000001",   // the Google tool
  "name": "Product Catalog",
  "description": "Read-only product list the agent answers questions from",
  "params": { "spreadsheetId": "1V9D…" },              // the resource id (see §4)
  "accessRole": "REFERENCE"                             // NEW
}
```

### Update
`PUT /api/sawtia/v1/tool-data-sources/{id}` (JWT required) — send any subset;
`accessRole` omitted = unchanged.

```jsonc
{ "accessRole": "DATA_SINK" }
```

### Response
`ToolDataSourceResponse` now echoes `accessRole`:

```jsonc
{
  "id": "…", "toolId": "…", "toolName": "Google Sheets",
  "name": "Product Catalog", "params": "{…}",
  "accessRole": "REFERENCE",
  "createdAt": "…", "updatedAt": "…"
}
```

### Link a data source to an agent's tool
`POST /api/sawtia/v1/agent-tools/{agentToolId}/data-sources` with
`{ toolDataSourceId, toolRole, displayOrder }`. `toolRole` here is a **free-text
label** for the LLM (e.g. `"new leads"`), **not** the security role — keep the
two concepts separate in the UI.

---

## 3. When to show the selector

| Tool family | Show `REFERENCE` / `DATA_SINK` selector? | Default to suggest |
|---|---|---|
| Google Sheets | **Yes** | ask the owner |
| Google Docs | **Yes** | ask the owner |
| Google Drive | No (read-only by nature) | — (omit) |
| Google Calendar | No (booking policy is fixed) | — (omit) |

Suggested copy for the selector:

> **What should the agent do with this sheet/doc?**
> - 🔵 **Read it** — the agent uses it to answer customers (catalog, price list, FAQ). *It will not be changed.*
> - 🟢 **Write to it** — the agent saves new entries here (leads, bookings, call logs). *Its existing contents won't be read out to customers.*

---

## 4. `params` — still owner-supplied, but no longer trusted at runtime

`params` still carries the **resource id** the owner picked (`spreadsheetId`,
`documentId`, `fileId`, `calendarId`) — usually from the Google Picker. That is
correct and unchanged: it is the owner's binding.

What changed is **runtime trust**: the backend now *forces* that bound id on
every call and **ignores any id the conversation/LLM tries to supply**. So:

- ✅ Keep sending the resource id in `params` at config time.
- ❌ Do **not** expose resource-id inputs in any *chat / runtime* surface — they're injected server-side and stripped from the model.
- `ownerId` is injected by the backend from the JWT — never send it in `params`.

### Sheets: bind the tab for multi-tab spreadsheets ⚠️
For Google Sheets, also include a **`range`** in `params` to pin the agent to one
tab — e.g. `{ "spreadsheetId": "1V9D…", "range": "Products!A:Z" }`. The backend
force-binds `range` like the resource id: the agent can only touch the bound
tab. **If you omit `range`, reads are restricted to the spreadsheet's *first
tab* only.** So if the owner keeps a sensitive tab (e.g. `Payroll`) in the same
spreadsheet as the catalog, the catalog tab **must** be named in `range` — the
UI should let the owner pick the tab when they attach a Sheets source.

---

## 5. Multiple data sources on one tool

A single tool can have **several** bindings — e.g. Google Sheets bound to both a
`REFERENCE` catalog **and** a `DATA_SINK` leads sheet. Just attach both (each
its own `ToolDataSource` + link). The backend gives the LLM an opaque
`dataSourceId` selector to choose between them at runtime; **the frontend does
nothing special** beyond letting the owner attach more than one and set each
one's role + `toolRole` label.

---

## 6. Migration note — review existing data sources ⚠️

On the 2026-06 release, **every existing Google data source was defaulted to
`DATA_SINK`** (security-safe). Consequence: an existing **catalog/reference**
sheet or doc will **stop being readable** by the agent until its owner re-marks
it as `REFERENCE`.

**Recommended UX:** surface a one-time review prompt on the data-source list —
e.g. a banner *"Review what each connected sheet/doc is for"* — so owners can
set `REFERENCE` on their read sources. Until they do, a read-backed agent will
reply that it can't read that source (this is expected, not a bug).

---

## 7. Behaviour the UI should explain (so it isn't reported as a bug)

| Symptom | Cause | Resolution |
|---|---|---|
| Agent says it can't read a sheet/doc | It's a `DATA_SINK` | Set role to `REFERENCE` if it's a read source |
| Agent can't write/append to a sheet/doc | It's a `REFERENCE` | Set role to `DATA_SINK` if it's a write target |
| Agent won't list all calendar events / read event details | Blocked by design | Use availability ("is that slot free?") — details are never exposed |
| Agent can't cancel an appointment from a previous chat | Cancel is limited to the conversation that booked it | Expected; book/cancel within one conversation |
| Gmail only emails the owner, ignores a "to" | Send-to-self only (owner notification channel) | By design — the agent cannot email third parties |

---

## 8. Cross-reference

- **Full security model & runtime gate** → [../mcp/04_GOOGLE_HANDLERS.md](../mcp/04_GOOGLE_HANDLERS.md)
- **Connect → agent → campaign journey** → [GOOGLE-TOOLS-END-TO-END-FLOW.md](GOOGLE-TOOLS-END-TO-END-FLOW.md)
