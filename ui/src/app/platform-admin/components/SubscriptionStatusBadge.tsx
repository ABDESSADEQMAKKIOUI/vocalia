"use client";

import { Badge } from '@/components/ui/badge';
import type { SubscriptionStatus } from '@/lib/platformAdminApi';
import { cn } from '@/lib/utils';

import { STATUS_LABELS } from './format';

const STATUS_CLASSES: Record<SubscriptionStatus, string> = {
    active: 'border-transparent bg-green-500 text-white hover:bg-green-600',
    trialing: 'border-transparent bg-blue-500 text-white hover:bg-blue-600',
    past_due: 'border-transparent bg-amber-500 text-white hover:bg-amber-600',
    suspended: 'border-transparent bg-red-500 text-white hover:bg-red-600',
    cancelled: 'border-transparent bg-muted text-muted-foreground hover:bg-muted',
};

type SubscriptionStatusBadgeProps = {
    /** null/undefined → the organization has no subscription row yet. */
    status?: SubscriptionStatus | null;
    className?: string;
};

export function SubscriptionStatusBadge({
    status,
    className,
}: SubscriptionStatusBadgeProps) {
    if (!status) {
        return (
            <Badge variant="outline" className={className}>
                sans abonnement
            </Badge>
        );
    }
    return (
        <Badge className={cn(STATUS_CLASSES[status], className)}>
            {STATUS_LABELS[status]}
        </Badge>
    );
}
