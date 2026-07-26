"use client";

import { Building2, CircleDollarSign, PauseCircle, Sparkles, TrendingUp } from 'lucide-react';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import type { PlatformMetricsDto } from '@/lib/platformAdminApi';

import { formatMinutes, formatNumber, formatPrice } from './format';

type MetricsBannerProps = {
    metrics: PlatformMetricsDto | null;
    loading: boolean;
};

export function MetricsBanner({ metrics, loading }: MetricsBannerProps) {
    if (loading) {
        return (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
                {Array.from({ length: 5 }).map((_, index) => (
                    <Skeleton key={index} className="h-24 w-full" />
                ))}
            </div>
        );
    }

    if (!metrics) {
        return null;
    }

    const cards = [
        {
            title: 'Organisations',
            value: formatNumber(metrics.total_organizations),
            icon: Building2,
            hint: `${formatNumber(metrics.past_due)} en impayé`,
        },
        {
            title: 'Abonnements actifs',
            value: formatNumber(metrics.active_subscriptions),
            icon: TrendingUp,
            hint: null,
        },
        {
            title: 'Essais en cours',
            value: formatNumber(metrics.trialing),
            icon: Sparkles,
            hint: null,
        },
        {
            title: 'Suspendus',
            value: formatNumber(metrics.suspended),
            icon: PauseCircle,
            hint: null,
        },
        {
            title: 'Revenu mensuel',
            value: formatPrice(metrics.mrr_amount, metrics.currency),
            icon: CircleDollarSign,
            hint: null,
        },
    ];

    return (
        <div className="space-y-2">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
                {cards.map((card) => (
                    <Card key={card.title}>
                        <CardHeader className="pb-2">
                            <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                                <card.icon className="h-4 w-4" />
                                {card.title}
                            </CardTitle>
                        </CardHeader>
                        <CardContent>
                            <p className="text-2xl font-semibold">{card.value}</p>
                            {card.hint && (
                                <p className="text-xs text-muted-foreground">{card.hint}</p>
                            )}
                        </CardContent>
                    </Card>
                ))}
            </div>
            <p className="text-xs text-muted-foreground">
                Consommation de la période en cours :{' '}
                {formatMinutes(metrics.voice_minutes_this_period)} minutes de voix ·{' '}
                {formatNumber(metrics.whatsapp_messages_this_period)} messages WhatsApp
            </p>
        </div>
    );
}
