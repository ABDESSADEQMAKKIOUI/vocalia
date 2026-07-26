/**
 * Hand-written API wrapper for the platform admin (SaaS back office) endpoints.
 *
 * Same mechanics as `lib/messagingApi.ts`: requests go through the shared
 * generated `client` instance, so they inherit the base URL resolved by
 * `createClientConfig` and the auth interceptor registered by OrgConfigProvider.
 * Callers must therefore only invoke these functions once auth is loaded (guard
 * with `authLoading` and `user` from `useAuth()` — see ui/AGENTS.md).
 *
 * Every function returns the parsed JSON payload directly and THROWS an `Error`
 * carrying the backend detail message on non-2xx responses. All routes require a
 * superuser; the backend answers 403 to anybody else.
 *
 * Source of truth for shapes:
 *   api/routes/platform_admin.py + api/services/subscription/
 */

import { client } from "@/client/client.gen";
import { detailFromError } from "@/lib/apiError";

// ---------------------------------------------------------------------------
// Shared value objects
// ---------------------------------------------------------------------------

export type BillingInterval = "monthly" | "yearly";

export type SubscriptionStatus =
    | "trialing"
    | "active"
    | "past_due"
    | "suspended"
    | "cancelled";

export type SubscriptionEventType =
    | "provisioned"
    | "plan_changed"
    | "suspended"
    | "reactivated"
    | "cancelled"
    | "renewed"
    | "limits_updated"
    | "quota_blocked";

export type PlanFeatureKey =
    | "voice"
    | "whatsapp"
    | "campaigns"
    | "telephony"
    | "api_access"
    | "knowledge_base"
    | "integrations";

export type PlanFeatures = Partial<Record<PlanFeatureKey, boolean>>;

/** Plan limits after applying the subscription overrides. null = unlimited. */
export interface EffectiveLimits {
    max_voice_minutes: number | null;
    max_whatsapp_messages: number | null;
    max_workflows: number | null;
    max_campaigns_per_month: number | null;
    max_users: number | null;
    max_concurrent_calls: number | null;
}

/** Consumption of the current subscription period. */
export interface UsageSnapshot {
    voice_minutes: number;
    whatsapp_messages: number;
    workflows: number;
    campaigns_this_period: number;
    users: number;
    period_start: string;
    period_end: string;
}

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

export interface PlanDto {
    id: number;
    code: string;
    name: string;
    description?: string | null;
    price_amount: number;
    currency: string;
    billing_interval: BillingInterval;
    trial_days: number;
    max_voice_minutes: number | null;
    max_whatsapp_messages: number | null;
    max_workflows: number | null;
    max_campaigns_per_month: number | null;
    max_users: number | null;
    max_concurrent_calls: number | null;
    features: PlanFeatures;
    is_active: boolean;
    is_public: boolean;
    sort_order: number;
    created_at: string;
    updated_at: string;
}

export interface SubscriptionDto {
    id: number;
    organization_id: number;
    plan_id: number;
    status: SubscriptionStatus;
    current_period_start: string;
    current_period_end: string;
    trial_end_at?: string | null;
    cancel_at_period_end: boolean;
    /** Only the keys the operator overrode; the rest falls back to the plan. */
    limit_overrides: Partial<EffectiveLimits>;
    external_reference?: string | null;
    notes?: string | null;
    created_at: string;
    updated_at: string;
}

export interface SubscriptionEventDto {
    id: number;
    organization_id: number;
    subscription_id?: number | null;
    event_type: SubscriptionEventType;
    actor_user_id?: number | null;
    payload: Record<string, unknown>;
    note?: string | null;
    created_at: string;
}

export interface OrganizationUserDto {
    id: number;
    email: string;
    is_superuser: boolean;
}

export interface OrganizationDto {
    id: number;
    name?: string | null;
    contact_email?: string | null;
    created_at: string;
}

/** One row of the customer table: subscription state flattened for listing. */
export interface OrganizationSummaryDto {
    id: number;
    name?: string | null;
    contact_email?: string | null;
    created_at: string;
    plan_code?: string | null;
    plan_name?: string | null;
    status?: SubscriptionStatus | null;
    current_period_end?: string | null;
    usage: {
        voice_minutes: number;
        whatsapp_messages: number;
    };
    limits: EffectiveLimits;
    users_count: number;
}

export interface OrganizationListDto {
    organizations: OrganizationSummaryDto[];
    total_count: number;
    page: number;
    limit: number;
    total_pages: number;
}

export interface OrganizationDetailDto {
    organization: OrganizationDto;
    subscription: SubscriptionDto | null;
    plan: PlanDto | null;
    limits: EffectiveLimits;
    usage: UsageSnapshot;
    users: OrganizationUserDto[];
    events: SubscriptionEventDto[];
}

export interface OrganizationUsageDto {
    usage: UsageSnapshot;
    /** null when the route answers the bare snapshot without the limits. */
    limits: EffectiveLimits | null;
}

export interface PlatformMetricsDto {
    total_organizations: number;
    active_subscriptions: number;
    trialing: number;
    suspended: number;
    past_due: number;
    mrr_amount: number;
    currency: string;
    voice_minutes_this_period: number;
    whatsapp_messages_this_period: number;
}

// ---------------------------------------------------------------------------
// Request payload types
// ---------------------------------------------------------------------------

export interface PlanWritePayload {
    code: string;
    name: string;
    description?: string | null;
    price_amount: number;
    currency: string;
    billing_interval: BillingInterval;
    trial_days: number;
    /** null on any limit means unlimited. */
    max_voice_minutes: number | null;
    max_whatsapp_messages: number | null;
    max_workflows: number | null;
    max_campaigns_per_month: number | null;
    max_users: number | null;
    max_concurrent_calls: number | null;
    features: PlanFeatures;
    is_active: boolean;
    is_public: boolean;
    sort_order: number;
}

export interface ProvisionOrganizationPayload {
    name: string;
    contact_email: string;
    owner_email: string;
    owner_password: string;
    plan_code: string;
    trial_days?: number;
    notes?: string;
}

export interface AssignPlanPayload {
    plan_code: string;
    status?: SubscriptionStatus;
    current_period_start?: string;
    current_period_end?: string;
    limit_overrides?: Partial<EffectiveLimits>;
    notes?: string;
}

export interface CancelSubscriptionPayload {
    at_period_end: boolean;
    reason?: string;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

async function request<T>(
    method: "get" | "post" | "patch" | "delete",
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

/**
 * Read a collection that may come back bare or wrapped in a `{ key: [...] }`
 * envelope, which is how the other repo routes shape their list responses.
 */
function unwrapList<T>(payload: unknown, key: string): T[] {
    if (Array.isArray(payload)) {
        return payload as T[];
    }
    const items = (payload as Record<string, unknown> | null)?.[key];
    return Array.isArray(items) ? (items as T[]) : [];
}

const BASE = "/api/v1/platform-admin";

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

export async function getPlatformMetrics(): Promise<PlatformMetricsDto> {
    return request<PlatformMetricsDto>(
        "get",
        `${BASE}/metrics`,
        "Échec du chargement des métriques",
    );
}

// ---------------------------------------------------------------------------
// Plans
// ---------------------------------------------------------------------------

export async function listPlans(params?: {
    includeInactive?: boolean;
}): Promise<PlanDto[]> {
    const query: Record<string, unknown> = {};
    if (params?.includeInactive) {
        query.include_inactive = true;
    }
    const data = await request<unknown>(
        "get",
        `${BASE}/plans`,
        "Échec du chargement des plans",
        { query },
    );
    return unwrapList<PlanDto>(data, "plans");
}

export async function createPlan(body: PlanWritePayload): Promise<PlanDto> {
    return request<PlanDto>("post", `${BASE}/plans`, "Échec de la création du plan", {
        body,
    });
}

export async function updatePlan(
    planId: number,
    body: Partial<PlanWritePayload>,
): Promise<PlanDto> {
    return request<PlanDto>(
        "patch",
        `${BASE}/plans/${planId}`,
        "Échec de la mise à jour du plan",
        { body },
    );
}

/** Soft deactivation: the backend answers 409 when organizations still use it. */
export async function deactivatePlan(planId: number): Promise<PlanDto> {
    return request<PlanDto>(
        "delete",
        `${BASE}/plans/${planId}`,
        "Échec de la désactivation du plan",
    );
}

export async function seedDefaultPlans(): Promise<PlanDto[]> {
    const data = await request<unknown>(
        "post",
        `${BASE}/plans/seed-defaults`,
        "Échec de la création des plans par défaut",
    );
    return unwrapList<PlanDto>(data, "plans");
}

// ---------------------------------------------------------------------------
// Organizations
// ---------------------------------------------------------------------------

export async function listOrganizations(params?: {
    search?: string;
    status?: SubscriptionStatus;
    page?: number;
    limit?: number;
}): Promise<OrganizationListDto> {
    const query: Record<string, unknown> = {};
    if (params?.search) query.search = params.search;
    if (params?.status) query.status = params.status;
    if (params?.page !== undefined) query.page = params.page;
    if (params?.limit !== undefined) query.limit = params.limit;
    return request<OrganizationListDto>(
        "get",
        `${BASE}/organizations`,
        "Échec du chargement des clients",
        { query },
    );
}

export async function provisionOrganization(
    body: ProvisionOrganizationPayload,
): Promise<OrganizationDetailDto> {
    return request<OrganizationDetailDto>(
        "post",
        `${BASE}/organizations`,
        "Échec de la création du client",
        { body },
    );
}

export async function getOrganization(orgId: number): Promise<OrganizationDetailDto> {
    return request<OrganizationDetailDto>(
        "get",
        `${BASE}/organizations/${orgId}`,
        "Échec du chargement du client",
    );
}

export async function assignPlan(
    orgId: number,
    body: AssignPlanPayload,
): Promise<OrganizationDetailDto> {
    return request<OrganizationDetailDto>(
        "post",
        `${BASE}/organizations/${orgId}/subscription`,
        "Échec du changement de plan",
        { body },
    );
}

export async function suspendOrganization(
    orgId: number,
    reason?: string,
): Promise<OrganizationDetailDto> {
    return request<OrganizationDetailDto>(
        "post",
        `${BASE}/organizations/${orgId}/suspend`,
        "Échec de la suspension",
        { body: reason ? { reason } : {} },
    );
}

export async function reactivateOrganization(
    orgId: number,
): Promise<OrganizationDetailDto> {
    return request<OrganizationDetailDto>(
        "post",
        `${BASE}/organizations/${orgId}/reactivate`,
        "Échec de la réactivation",
    );
}

export async function cancelOrganization(
    orgId: number,
    body: CancelSubscriptionPayload,
): Promise<OrganizationDetailDto> {
    return request<OrganizationDetailDto>(
        "post",
        `${BASE}/organizations/${orgId}/cancel`,
        "Échec de l'annulation",
        { body },
    );
}

export async function renewOrganizationPeriod(
    orgId: number,
): Promise<OrganizationDetailDto> {
    return request<OrganizationDetailDto>(
        "post",
        `${BASE}/organizations/${orgId}/renew`,
        "Échec du renouvellement de la période",
    );
}

export async function updateOrganizationLimits(
    orgId: number,
    limitOverrides: Partial<EffectiveLimits>,
): Promise<OrganizationDetailDto> {
    return request<OrganizationDetailDto>(
        "patch",
        `${BASE}/organizations/${orgId}/limits`,
        "Échec de la mise à jour des limites",
        { body: { limit_overrides: limitOverrides } },
    );
}

/**
 * Fetch the usage snapshot alone. The route answers either the bare snapshot or
 * a `{ usage, limits }` envelope; both are normalized to `OrganizationUsageDto`.
 */
export async function getOrganizationUsage(
    orgId: number,
): Promise<OrganizationUsageDto> {
    const data = await request<Record<string, unknown>>(
        "get",
        `${BASE}/organizations/${orgId}/usage`,
        "Échec du chargement de la consommation",
    );
    return {
        usage: (data.usage ?? data) as UsageSnapshot,
        limits: (data.limits ?? null) as EffectiveLimits | null,
    };
}

export async function listOrganizationEvents(
    orgId: number,
    limit = 50,
): Promise<SubscriptionEventDto[]> {
    const data = await request<unknown>(
        "get",
        `${BASE}/organizations/${orgId}/events`,
        "Échec du chargement du journal",
        { query: { limit } },
    );
    return unwrapList<SubscriptionEventDto>(data, "events");
}
