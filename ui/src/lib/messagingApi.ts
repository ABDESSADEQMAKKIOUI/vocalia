/**
 * Hand-written API wrapper for the messaging (WhatsApp) endpoints.
 *
 * These routes are not in the generated SDK yet, so this module issues
 * requests through the shared generated `client` instance. That gives the
 * exact same behavior as SDK calls: the base URL resolved by
 * `createClientConfig` (and upgraded by AppConfigProvider once /health
 * resolves), and the auth interceptor registered by OrgConfigProvider that
 * attaches a fresh Bearer token to every request. Callers must therefore
 * only invoke these functions once auth is loaded (guard with `authLoading`
 * and `user` from `useAuth()` — see ui/AGENTS.md).
 *
 * Unlike the generated SDK (which resolves `{ data, error }`), every
 * function here returns the parsed JSON payload directly and THROWS an
 * `Error` with the backend detail message on non-2xx responses.
 *
 * Source of truth for shapes:
 *   api/routes/messaging_config.py + api/schemas/messaging_config.py
 *   api/routes/messaging_templates.py + api/schemas/messaging_templates.py
 */

import { client } from "@/client/client.gen";
import { detailFromError } from "@/lib/apiError";

// ---------------------------------------------------------------------------
// Response types (mirror api/schemas/messaging_config.py)
// ---------------------------------------------------------------------------

export interface MessagingAddress {
    id: number;
    /** Display phone number, e.g. "+14155551234". */
    address: string;
    /** Meta's phone_number_id — the inbound routing key. */
    external_id: string;
    account_id?: string | null;
    inbound_workflow_id?: number | null;
    is_active: boolean;
    created_at: string;
    updated_at: string;
}

export interface MessagingConfiguration {
    id: number;
    name: string;
    provider: string;
    is_active: boolean;
    /**
     * Display-safe credentials: secret values (access_token, app_secret,
     * verify_token) are masked placeholders; waba_id is returned in full.
     */
    credentials: Record<string, string>;
    addresses: MessagingAddress[];
    created_at: string;
    updated_at: string;
}

export interface WebhookInfo {
    webhook_url: string;
    verify_token_set: boolean;
    app_secret_set: boolean;
}

export interface EmbeddedSignupConfig {
    /** True only when both the app id and login-config id env vars are set. */
    enabled: boolean;
    app_id: string | null;
    config_id: string | null;
    graph_version: string;
}

export interface MessagingSuppression {
    id: number;
    address: string;
    scope: string;
    reason: string;
    expires_at?: string | null;
    created_at: string;
    updated_at: string;
}

// Mirrors api/schemas/messaging_templates.py::WhatsAppTemplateResponse
export interface WhatsAppTemplate {
    id: number;
    messaging_configuration_id: number;
    meta_template_id?: string | null;
    name: string;
    language: string;
    category: string;
    parameter_format: string;
    components: Array<Record<string, unknown>>;
    status: string;
    quality_score?: string | null;
    rejection_reason?: string | null;
    category_pending_change?: string | null;
    /** Placeholder keys a sender must fill (["1", "2"] or named params). */
    placeholders: string[];
    last_synced_at?: string | null;
    created_at: string;
    updated_at?: string | null;
}

export interface WhatsAppTemplateSyncResult {
    fetched: number;
    created: number;
    updated: number;
    unchanged: number;
}

export interface WhatsAppTemplateDeleteResult {
    deleted: boolean;
    /** Best-effort Meta deletion; null when never submitted to Meta. */
    meta_deleted?: boolean | null;
}

// ---------------------------------------------------------------------------
// Request payload types
// ---------------------------------------------------------------------------

export interface MessagingCredentialsPayload {
    /** Required on create; on update a masked/omitted value keeps the stored secret. */
    access_token?: string;
    app_secret?: string;
    verify_token?: string;
    /** Required on create. */
    waba_id?: string;
}

export interface MessagingAddressCreatePayload {
    address: string;
    external_id: string;
    account_id?: string | null;
    inbound_workflow_id?: number | null;
}

export interface MessagingConfigurationCreatePayload {
    name: string;
    credentials: MessagingCredentialsPayload;
    addresses?: MessagingAddressCreatePayload[];
}

export interface MessagingConfigurationUpdatePayload {
    name?: string;
    is_active?: boolean;
    credentials?: MessagingCredentialsPayload;
}

export interface EmbeddedSignupCompletePayload {
    /** Short-lived auth code from Meta's Embedded Signup popup. */
    code: string;
    waba_id: string;
    phone_number_id: string;
    business_id?: string | null;
    name?: string | null;
}

export interface MessagingAddressUpdatePayload {
    inbound_workflow_id?: number | null;
    /** True detaches the inbound workflow (null id alone leaves it unchanged). */
    clear_inbound_workflow?: boolean;
    is_active?: boolean;
}

export interface MessagingSuppressionCreatePayload {
    address: string;
    scope?: "marketing" | "all";
    reason?: "manual";
    expires_at?: string | null;
}

export interface WhatsAppTemplateCreatePayload {
    messaging_configuration_id: number;
    name: string;
    language: string;
    category: string;
    components?: Array<Record<string, unknown>>;
    parameter_format?: string;
}

export interface WhatsAppTemplateUpdatePayload {
    name?: string;
    language?: string;
    category?: string;
    components?: Array<Record<string, unknown>>;
    parameter_format?: string;
}

// ---------------------------------------------------------------------------
// Internal request helper
// ---------------------------------------------------------------------------

async function request<T>(
    method: "get" | "post" | "put" | "delete",
    url: string,
    fallback: string,
    options: { body?: unknown; query?: Record<string, unknown> } = {},
): Promise<T> {
    const res = await client[method]({ url, body: options.body, query: options.query });
    if (res.error !== undefined) {
        throw new Error(detailFromError(res.error, fallback));
    }
    return res.data as T;
}

// ---------------------------------------------------------------------------
// Configurations
// ---------------------------------------------------------------------------

export async function listMessagingConfigurations(): Promise<MessagingConfiguration[]> {
    const data = await request<{ configurations: MessagingConfiguration[] }>(
        "get",
        "/api/v1/messaging/configurations/",
        "Failed to load messaging configurations",
    );
    return data.configurations ?? [];
}

export async function createMessagingConfiguration(
    body: MessagingConfigurationCreatePayload,
): Promise<MessagingConfiguration> {
    return request<MessagingConfiguration>(
        "post",
        "/api/v1/messaging/configurations/",
        "Failed to create messaging configuration",
        { body },
    );
}

export async function updateMessagingConfiguration(
    id: number,
    body: MessagingConfigurationUpdatePayload,
): Promise<MessagingConfiguration> {
    return request<MessagingConfiguration>(
        "put",
        `/api/v1/messaging/configurations/${id}`,
        "Failed to update messaging configuration",
        { body },
    );
}

export async function deleteMessagingConfiguration(
    id: number,
): Promise<{ message: string }> {
    return request<{ message: string }>(
        "delete",
        `/api/v1/messaging/configurations/${id}`,
        "Failed to delete messaging configuration",
    );
}

// ---------------------------------------------------------------------------
// Embedded Signup (Meta "Connect a WhatsApp account" flow)
// ---------------------------------------------------------------------------

export async function getEmbeddedSignupConfig(): Promise<EmbeddedSignupConfig> {
    return request<EmbeddedSignupConfig>(
        "get",
        "/api/v1/messaging/configurations/embedded-signup/config",
        "Failed to load embedded signup configuration",
    );
}

export async function completeEmbeddedSignup(
    body: EmbeddedSignupCompletePayload,
): Promise<MessagingConfiguration> {
    return request<MessagingConfiguration>(
        "post",
        "/api/v1/messaging/configurations/embedded-signup",
        "Failed to complete embedded signup",
        { body },
    );
}

// ---------------------------------------------------------------------------
// Addresses (business phone numbers)
// ---------------------------------------------------------------------------

export async function createMessagingAddress(
    configId: number,
    body: MessagingAddressCreatePayload,
): Promise<MessagingAddress> {
    return request<MessagingAddress>(
        "post",
        `/api/v1/messaging/configurations/${configId}/addresses`,
        "Failed to add phone number",
        { body },
    );
}

export async function updateMessagingAddress(
    addressId: number,
    body: MessagingAddressUpdatePayload,
): Promise<MessagingAddress> {
    return request<MessagingAddress>(
        "put",
        `/api/v1/messaging/configurations/addresses/${addressId}`,
        "Failed to update phone number",
        { body },
    );
}

export async function deleteMessagingAddress(
    addressId: number,
): Promise<{ message: string }> {
    return request<{ message: string }>(
        "delete",
        `/api/v1/messaging/configurations/addresses/${addressId}`,
        "Failed to delete phone number",
    );
}

// ---------------------------------------------------------------------------
// Webhook info + suppressions
// ---------------------------------------------------------------------------

export async function getWebhookInfo(): Promise<WebhookInfo> {
    return request<WebhookInfo>(
        "get",
        "/api/v1/messaging/configurations/webhook-info",
        "Failed to load webhook info",
    );
}

export async function listSuppressions(): Promise<MessagingSuppression[]> {
    const data = await request<{ suppressions: MessagingSuppression[] }>(
        "get",
        "/api/v1/messaging/configurations/suppressions",
        "Failed to load suppression list",
    );
    return data.suppressions ?? [];
}

export async function addSuppression(
    body: MessagingSuppressionCreatePayload,
): Promise<MessagingSuppression> {
    return request<MessagingSuppression>(
        "post",
        "/api/v1/messaging/configurations/suppressions",
        "Failed to add suppression",
        { body },
    );
}

export async function deleteSuppression(id: number): Promise<{ message: string }> {
    return request<{ message: string }>(
        "delete",
        `/api/v1/messaging/configurations/suppressions/${id}`,
        "Failed to delete suppression",
    );
}

// ---------------------------------------------------------------------------
// WhatsApp templates
// ---------------------------------------------------------------------------

export async function listWhatsAppTemplates(params?: {
    configurationId?: number;
    status?: string;
}): Promise<WhatsAppTemplate[]> {
    const query: Record<string, unknown> = {};
    if (params?.configurationId !== undefined) {
        query.configuration_id = params.configurationId;
    }
    if (params?.status !== undefined) {
        query.status = params.status;
    }
    const data = await request<{ templates: WhatsAppTemplate[] }>(
        "get",
        "/api/v1/messaging/whatsapp/templates",
        "Failed to load templates",
        { query },
    );
    return data.templates ?? [];
}

export async function createWhatsAppTemplate(
    body: WhatsAppTemplateCreatePayload,
): Promise<WhatsAppTemplate> {
    return request<WhatsAppTemplate>(
        "post",
        "/api/v1/messaging/whatsapp/templates",
        "Failed to create template",
        { body },
    );
}

export async function updateWhatsAppTemplate(
    id: number,
    body: WhatsAppTemplateUpdatePayload,
): Promise<WhatsAppTemplate> {
    return request<WhatsAppTemplate>(
        "put",
        `/api/v1/messaging/whatsapp/templates/${id}`,
        "Failed to update template",
        { body },
    );
}

export async function deleteWhatsAppTemplate(
    id: number,
): Promise<WhatsAppTemplateDeleteResult> {
    return request<WhatsAppTemplateDeleteResult>(
        "delete",
        `/api/v1/messaging/whatsapp/templates/${id}`,
        "Failed to delete template",
    );
}

export async function submitWhatsAppTemplate(id: number): Promise<WhatsAppTemplate> {
    return request<WhatsAppTemplate>(
        "post",
        `/api/v1/messaging/whatsapp/templates/${id}/submit`,
        "Failed to submit template",
    );
}

export async function syncWhatsAppTemplates(
    configurationId: number,
): Promise<WhatsAppTemplateSyncResult> {
    return request<WhatsAppTemplateSyncResult>(
        "post",
        "/api/v1/messaging/whatsapp/templates/sync",
        "Failed to sync templates",
        { query: { configuration_id: configurationId } },
    );
}

// ---------------------------------------------------------------------------
// WhatsApp inbox (conversations + thread)
// Mirrors the /api/v1/messaging/whatsapp/conversations routes.
// ---------------------------------------------------------------------------

export interface WhatsAppInboxConversation {
    id: number;
    /** Contact's WhatsApp id (phone number without the leading "+"). */
    wa_id: string;
    profile_name?: string | null;
    /** Meta's phone_number_id of the business number. */
    phone_number_id: string;
    /** Display number of the business address, e.g. "+14155551234". */
    display_number?: string | null;
    state: string;
    /** True when the AI agent is paused and a human answers manually. */
    agent_paused: boolean;
    /** End of the 24h customer-service window (ISO), null when never opened. */
    service_window_expires_at?: string | null;
    last_inbound_at?: string | null;
    last_outbound_at?: string | null;
    workflow_run_id?: number | null;
    campaign_id?: number | null;
    /** Name of the workflow driving the run, null when unavailable. */
    agent_name?: string | null;
    updated_at: string;
    /** Last message text truncated to 80 chars, null when empty. */
    last_message_preview?: string | null;
}

export interface WhatsAppInboxMessage {
    direction: "in" | "out";
    origin: "user" | "agent" | "human" | "template";
    text: string;
    /** ISO timestamp, null when unknown. */
    timestamp?: string | null;
}

export interface WhatsAppInboxDetail {
    conversation: WhatsAppInboxConversation;
    /** Chronological order (oldest first). */
    messages: WhatsAppInboxMessage[];
}

export async function listWhatsAppConversations(params?: {
    state?: "open" | "closed" | "all";
}): Promise<WhatsAppInboxConversation[]> {
    const query: Record<string, unknown> = {};
    if (params?.state !== undefined) {
        query.state = params.state;
    }
    const data = await request<{ conversations: WhatsAppInboxConversation[] }>(
        "get",
        "/api/v1/messaging/whatsapp/conversations",
        "Failed to load conversations",
        { query },
    );
    return data.conversations ?? [];
}

export async function getWhatsAppConversation(
    id: number,
): Promise<WhatsAppInboxDetail> {
    return request<WhatsAppInboxDetail>(
        "get",
        `/api/v1/messaging/whatsapp/conversations/${id}`,
        "Failed to load conversation",
    );
}

export async function sendWhatsAppReply(
    id: number,
    text: string,
): Promise<WhatsAppInboxDetail> {
    return request<WhatsAppInboxDetail>(
        "post",
        `/api/v1/messaging/whatsapp/conversations/${id}/send`,
        "Failed to send message",
        { body: { text } },
    );
}

export async function setWhatsAppAgentPaused(
    id: number,
    paused: boolean,
): Promise<{ id: number; agent_paused: boolean }> {
    return request<{ id: number; agent_paused: boolean }>(
        "post",
        `/api/v1/messaging/whatsapp/conversations/${id}/agent`,
        "Failed to update agent state",
        { body: { paused } },
    );
}
