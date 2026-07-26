"use client";

import { Progress } from '@/components/ui/progress';
import { cn } from '@/lib/utils';

import { formatLimit, formatMinutes, formatNumber } from './format';

type UsageBarProps = {
    used: number;
    /** null means unlimited: no bar is drawn, only the consumed value. */
    limit: number | null;
    /** Short suffix appended to both numbers, e.g. " min". */
    unit?: string;
    /** Decimal values (voice minutes) keep one digit. */
    decimal?: boolean;
    label?: string;
    className?: string;
};

export function UsageBar({
    used,
    limit,
    unit = '',
    decimal = false,
    label,
    className,
}: UsageBarProps) {
    const formatValue = decimal ? formatMinutes : formatNumber;
    const percent =
        limit !== null && limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
    const reached = limit !== null && limit > 0 && used >= limit;

    return (
        <div className={cn('min-w-32 space-y-1', className)}>
            {label && <p className="text-xs text-muted-foreground">{label}</p>}
            <p
                className={cn(
                    'text-xs',
                    reached ? 'text-destructive font-medium' : 'text-muted-foreground',
                )}
            >
                {formatValue(used)}
                {unit} / {limit === null ? formatLimit(null) : `${formatValue(limit)}${unit}`}
            </p>
            {limit !== null && (
                <Progress
                    value={percent}
                    className={cn(
                        reached &&
                        '[&>[data-slot=progress-indicator]]:bg-destructive',
                    )}
                />
            )}
        </div>
    );
}
