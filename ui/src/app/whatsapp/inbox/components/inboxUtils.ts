import { type WhatsAppInboxConversation } from '@/lib/messagingApi';

/** Contact name shown everywhere: profile name, else "+<wa_id>". */
export function conversationDisplayName(
    conversation: WhatsAppInboxConversation,
): string {
    const name = conversation.profile_name?.trim();
    return name && name.length > 0 ? name : `+${conversation.wa_id}`;
}

/** Initials for the avatar circle; falls back to the last wa_id digits. */
export function conversationInitials(
    conversation: WhatsAppInboxConversation,
): string {
    const name = conversation.profile_name?.trim();
    if (name) {
        const parts = name.split(/\s+/).filter(Boolean);
        const first = parts[0]?.charAt(0) ?? '';
        const last = parts.length > 1 ? parts[parts.length - 1].charAt(0) : '';
        const initials = `${first}${last}`.toUpperCase();
        if (initials) return initials;
    }
    return conversation.wa_id.slice(-2) || '?';
}

/** Short relative time in French ("il y a 5 min", "hier", …). */
export function formatRelativeTime(iso?: string | null): string {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    const diffMinutes = Math.floor((Date.now() - date.getTime()) / 60_000);
    if (diffMinutes < 1) return "à l'instant";
    if (diffMinutes < 60) return `il y a ${diffMinutes} min`;
    const diffHours = Math.floor(diffMinutes / 60);
    if (diffHours < 24) return `il y a ${diffHours} h`;
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays === 1) return 'hier';
    if (diffDays < 7) return `il y a ${diffDays} j`;
    return date.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
}

/** "HH:MM" in French locale, empty when the timestamp is missing. */
export function formatHourMinute(iso?: string | null): string {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleTimeString('fr-FR', {
        hour: '2-digit',
        minute: '2-digit',
    });
}

/** Stable key identifying the calendar day of a timestamp (local time). */
export function dayKey(iso?: string | null): string | null {
    if (!iso) return null;
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return null;
    return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function isSameDay(a: Date, b: Date): boolean {
    return (
        a.getFullYear() === b.getFullYear() &&
        a.getMonth() === b.getMonth() &&
        a.getDate() === b.getDate()
    );
}

/** Day-separator label: "Aujourd'hui", "Hier" or a full French date. */
export function dayLabel(iso: string): string {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    const today = new Date();
    const yesterday = new Date(
        today.getFullYear(),
        today.getMonth(),
        today.getDate() - 1,
    );
    if (isSameDay(date, today)) return "Aujourd'hui";
    if (isSameDay(date, yesterday)) return 'Hier';
    return date.toLocaleDateString('fr-FR', {
        day: 'numeric',
        month: 'long',
        year: 'numeric',
    });
}

/** True while the 24h customer-service window is still open. */
export function isServiceWindowOpen(expiresAt?: string | null): boolean {
    if (!expiresAt) return false;
    const expires = new Date(expiresAt);
    if (Number.isNaN(expires.getTime())) return false;
    return expires.getTime() > Date.now();
}
