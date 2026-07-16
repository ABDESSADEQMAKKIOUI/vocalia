"use client";

import { Badge } from '@/components/ui/badge';
import {
    Tooltip,
    TooltipContent,
    TooltipTrigger,
} from '@/components/ui/tooltip';

// Meta review statuses → badge styling. Anything unmapped (IN_APPEAL, …)
// falls back to the plain secondary badge.
const STATUS_CLASSES: Record<string, string> = {
    PENDING: 'border-transparent bg-amber-500 text-white hover:bg-amber-500',
    APPROVED: 'border-transparent bg-green-500 text-white hover:bg-green-500',
    PAUSED: 'border-transparent bg-orange-500 text-white hover:bg-orange-500',
    DISABLED:
        'border-transparent bg-gray-800 text-white hover:bg-gray-800 dark:bg-gray-600 dark:hover:bg-gray-600',
};

export function TemplateStatusBadge({
    status,
    rejectionReason,
}: {
    status: string;
    rejectionReason?: string | null;
}) {
    const upper = (status || '').toUpperCase();

    if (upper === 'REJECTED') {
        const badge = <Badge variant="destructive">REJECTED</Badge>;
        if (!rejectionReason) return badge;
        return (
            <Tooltip>
                <TooltipTrigger asChild>
                    <span className="inline-flex cursor-help">{badge}</span>
                </TooltipTrigger>
                <TooltipContent className="max-w-xs">{rejectionReason}</TooltipContent>
            </Tooltip>
        );
    }

    return (
        <Badge variant="secondary" className={STATUS_CLASSES[upper] ?? ''}>
            {upper || 'DRAFT'}
        </Badge>
    );
}

const QUALITY_CLASSES: Record<string, string> = {
    GREEN: 'bg-green-500',
    YELLOW: 'bg-yellow-400',
    RED: 'bg-red-500',
};

const QUALITY_LABELS: Record<string, string> = {
    GREEN: 'Qualité : bonne',
    YELLOW: 'Qualité : moyenne',
    RED: 'Qualité : faible',
};

export function QualityDot({ quality }: { quality?: string | null }) {
    const upper = (quality || '').toUpperCase();
    return (
        <span
            title={QUALITY_LABELS[upper] ?? 'Qualité inconnue'}
            className={`inline-block h-2.5 w-2.5 rounded-full ${QUALITY_CLASSES[upper] ?? 'bg-muted-foreground/25'}`}
        />
    );
}
