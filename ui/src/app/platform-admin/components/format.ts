/**
 * Labels and display helpers shared by the platform admin pages.
 */

import { format } from 'date-fns';

import type {
    BillingInterval,
    EffectiveLimits,
    PlanFeatureKey,
    SubscriptionEventType,
    SubscriptionStatus,
} from '@/lib/platformAdminApi';

export type LimitKey = keyof EffectiveLimits;

/** Text values of the six limit inputs; "" means unlimited. */
export type LimitInputs = Record<LimitKey, string>;

export const STATUS_LABELS: Record<SubscriptionStatus, string> = {
    trialing: 'essai',
    active: 'actif',
    past_due: 'impayé',
    suspended: 'suspendu',
    cancelled: 'annulé',
};

export const BILLING_INTERVAL_LABELS: Record<BillingInterval, string> = {
    monthly: 'mensuel',
    yearly: 'annuel',
};

export const FEATURE_KEYS: PlanFeatureKey[] = [
    'voice',
    'whatsapp',
    'campaigns',
    'telephony',
    'api_access',
    'knowledge_base',
    'integrations',
];

export const FEATURE_LABELS: Record<PlanFeatureKey, string> = {
    voice: 'voix',
    whatsapp: 'whatsapp',
    campaigns: 'campagnes',
    telephony: 'téléphonie',
    api_access: 'accès api',
    knowledge_base: 'base de connaissances',
    integrations: 'intégrations',
};

export const EVENT_LABELS: Record<SubscriptionEventType, string> = {
    provisioned: 'client provisionné',
    plan_changed: 'plan modifié',
    suspended: 'abonnement suspendu',
    reactivated: 'abonnement réactivé',
    cancelled: 'abonnement annulé',
    renewed: 'période renouvelée',
    limits_updated: 'limites modifiées',
    quota_blocked: 'quota bloqué',
};

export const LIMIT_KEYS: LimitKey[] = [
    'max_voice_minutes',
    'max_whatsapp_messages',
    'max_workflows',
    'max_campaigns_per_month',
    'max_users',
    'max_concurrent_calls',
];

// The counter behind max_whatsapp_messages is the number of conversation
// windows opened in the period, not individual messages: Dograh persists no
// per-message row, and a window is also what Meta itself bills. The label says
// so, so an operator never sells a ceiling that means something else.
export const LIMIT_LABELS: Record<LimitKey, string> = {
    max_voice_minutes: 'Minutes de voix',
    max_whatsapp_messages: 'Conversations WhatsApp',
    max_workflows: 'Workflows',
    max_campaigns_per_month: 'Campagnes par mois',
    max_users: 'Utilisateurs',
    max_concurrent_calls: 'Appels simultanés',
};

// Shown under the field in the plan and limit dialogs.
export const LIMIT_HINTS: Partial<Record<LimitKey, string>> = {
    max_whatsapp_messages:
        'Fenêtres de conversation ouvertes sur la période, pas messages unitaires.',
    max_concurrent_calls: 'Informatif : non appliqué à l’exécution pour le moment.',
    max_workflows: 'Informatif : non appliqué à l’exécution pour le moment.',
    max_users: 'Informatif : non appliqué à l’exécution pour le moment.',
};

export function formatDate(value?: string | null): string {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : format(date, 'dd/MM/yyyy');
}

export function formatDateTime(value?: string | null): string {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : format(date, 'dd/MM/yyyy HH:mm');
}

/** ISO timestamp → value accepted by an `<input type="date">`. */
export function toDateInput(value?: string | null): string {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : format(date, 'yyyy-MM-dd');
}

export function formatNumber(value: number): string {
    return new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 0 }).format(value);
}

export function formatMinutes(value: number): string {
    return new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 }).format(value);
}

export function formatPrice(amount: number, currency: string): string {
    try {
        return new Intl.NumberFormat('fr-FR', {
            style: 'currency',
            currency,
            maximumFractionDigits: 2,
        }).format(amount);
    } catch {
        // Intl throws on a currency code that is not a valid ISO 4217 value.
        return `${formatNumber(amount)} ${currency}`;
    }
}

export function formatLimit(limit: number | null | undefined): string {
    return limit === null || limit === undefined ? 'illimité' : formatNumber(limit);
}

/** An empty input means "unlimited" (null), never zero. */
export function parseLimitInput(value: string): number | null {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) && parsed >= 0 ? Math.round(parsed) : null;
}

export function limitToInput(limit: number | null | undefined): string {
    return limit === null || limit === undefined ? '' : String(limit);
}

export function limitsToInputs(source?: Partial<EffectiveLimits> | null): LimitInputs {
    return {
        max_voice_minutes: limitToInput(source?.max_voice_minutes),
        max_whatsapp_messages: limitToInput(source?.max_whatsapp_messages),
        max_workflows: limitToInput(source?.max_workflows),
        max_campaigns_per_month: limitToInput(source?.max_campaigns_per_month),
        max_users: limitToInput(source?.max_users),
        max_concurrent_calls: limitToInput(source?.max_concurrent_calls),
    };
}

export function inputsToLimits(inputs: LimitInputs): EffectiveLimits {
    return {
        max_voice_minutes: parseLimitInput(inputs.max_voice_minutes),
        max_whatsapp_messages: parseLimitInput(inputs.max_whatsapp_messages),
        max_workflows: parseLimitInput(inputs.max_workflows),
        max_campaigns_per_month: parseLimitInput(inputs.max_campaigns_per_month),
        max_users: parseLimitInput(inputs.max_users),
        max_concurrent_calls: parseLimitInput(inputs.max_concurrent_calls),
    };
}
