"use client";

import {
    AlertCircle,
    ArrowLeft,
    Ban,
    History,
    Loader2,
    PauseCircle,
    PlayCircle,
    RefreshCw,
    RotateCw,
    SlidersHorizontal,
} from 'lucide-react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from '@/components/ui/table';
import {
    getOrganization,
    getOrganizationUsage,
    listOrganizationEvents,
    listPlans,
    type OrganizationDetailDto,
    type PlanDto,
    reactivateOrganization,
    renewOrganizationPeriod,
    type SubscriptionEventDto,
    type UsageSnapshot,
} from '@/lib/platformAdminApi';

import { AssignPlanDialog } from '../../components/AssignPlanDialog';
import { CancelSubscriptionDialog } from '../../components/CancelSubscriptionDialog';
import {
    BILLING_INTERVAL_LABELS,
    EVENT_LABELS,
    FEATURE_KEYS,
    FEATURE_LABELS,
    formatDate,
    formatDateTime,
    formatLimit,
    formatNumber,
    formatPrice,
} from '../../components/format';
import { LimitOverridesDialog } from '../../components/LimitOverridesDialog';
import { SubscriptionStatusBadge } from '../../components/SubscriptionStatusBadge';
import { SuperuserGuard } from '../../components/SuperuserGuard';
import { SuspendOrganizationDialog } from '../../components/SuspendOrganizationDialog';
import { UsageBar } from '../../components/UsageBar';

type InlineAction = 'reactivate' | 'renew' | 'usage' | 'events';

export default function PlatformAdminOrganizationPage() {
    return (
        <SuperuserGuard>
            <OrganizationDetail />
        </SuperuserGuard>
    );
}

/** Rendered only once SuperuserGuard confirmed auth is loaded and superuser. */
function OrganizationDetail() {
    const params = useParams();
    const orgId = Number(params.orgId);

    const [detail, setDetail] = useState<OrganizationDetailDto | null>(null);
    const [usage, setUsage] = useState<UsageSnapshot | null>(null);
    const [events, setEvents] = useState<SubscriptionEventDto[]>([]);
    const [plans, setPlans] = useState<PlanDto[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [pendingAction, setPendingAction] = useState<InlineAction | null>(null);

    const [assignOpen, setAssignOpen] = useState(false);
    const [suspendOpen, setSuspendOpen] = useState(false);
    const [cancelOpen, setCancelOpen] = useState(false);
    const [limitsOpen, setLimitsOpen] = useState(false);

    const fetchDetail = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const data = await getOrganization(orgId);
            setDetail(data);
            setUsage(data.usage);
            setEvents(data.events ?? []);
        } catch (err) {
            setError(
                err instanceof Error ? err.message : 'Échec du chargement du client',
            );
        } finally {
            setLoading(false);
        }
    }, [orgId]);

    const fetchPlans = useCallback(async () => {
        try {
            setPlans(await listPlans({ includeInactive: true }));
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec du chargement des plans',
            );
        }
    }, []);

    useEffect(() => {
        if (Number.isNaN(orgId)) {
            setError('Identifiant de client invalide');
            setLoading(false);
            return;
        }
        fetchDetail();
        fetchPlans();
    }, [orgId, fetchDetail, fetchPlans]);

    const handleReactivate = async () => {
        setPendingAction('reactivate');
        try {
            await reactivateOrganization(orgId);
            toast.success('Abonnement réactivé');
            await fetchDetail();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec de la réactivation',
            );
        } finally {
            setPendingAction(null);
        }
    };

    const handleRenew = async () => {
        setPendingAction('renew');
        try {
            await renewOrganizationPeriod(orgId);
            toast.success('Période renouvelée');
            await fetchDetail();
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : 'Échec du renouvellement de la période',
            );
        } finally {
            setPendingAction(null);
        }
    };

    const handleRefreshUsage = async () => {
        setPendingAction('usage');
        try {
            const data = await getOrganizationUsage(orgId);
            setUsage(data.usage);
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : 'Échec du chargement de la consommation',
            );
        } finally {
            setPendingAction(null);
        }
    };

    const handleLoadEvents = async () => {
        setPendingAction('events');
        try {
            setEvents(await listOrganizationEvents(orgId, 50));
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec du chargement du journal',
            );
        } finally {
            setPendingAction(null);
        }
    };

    if (loading) {
        return (
            <div className="container mx-auto px-4 py-8 space-y-4">
                <Skeleton className="h-10 w-72" />
                <Skeleton className="h-40 w-full" />
                <Skeleton className="h-64 w-full" />
            </div>
        );
    }

    if (error || !detail) {
        return (
            <div className="container mx-auto px-4 py-8">
                <Card className="max-w-xl">
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2">
                            <AlertCircle className="h-5 w-5 text-destructive" />
                            Client introuvable
                        </CardTitle>
                        <CardDescription>
                            {error ?? 'Aucune donnée disponible pour ce client.'}
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        <Button variant="outline" asChild>
                            <Link href="/platform-admin">
                                <ArrowLeft className="h-4 w-4 mr-2" />
                                Retour à la console
                            </Link>
                        </Button>
                    </CardContent>
                </Card>
            </div>
        );
    }

    const { organization, subscription, plan, limits } = detail;
    const snapshot = usage ?? detail.usage;
    const usageItems = [
        {
            key: 'voice',
            label: 'Minutes de voix',
            used: snapshot.voice_minutes,
            limit: limits.max_voice_minutes,
            unit: ' min',
            decimal: true,
        },
        {
            key: 'whatsapp',
            label: 'Messages WhatsApp',
            used: snapshot.whatsapp_messages,
            limit: limits.max_whatsapp_messages,
            unit: '',
            decimal: false,
        },
        {
            key: 'workflows',
            label: 'Workflows',
            used: snapshot.workflows,
            limit: limits.max_workflows,
            unit: '',
            decimal: false,
        },
        {
            key: 'campaigns',
            label: 'Campagnes sur la période',
            used: snapshot.campaigns_this_period,
            limit: limits.max_campaigns_per_month,
            unit: '',
            decimal: false,
        },
        {
            key: 'users',
            label: 'Utilisateurs',
            used: snapshot.users,
            limit: limits.max_users,
            unit: '',
            decimal: false,
        },
    ];

    return (
        <div className="min-h-screen">
            <div className="container mx-auto px-4 py-8 space-y-8">
                <div className="flex flex-wrap items-start justify-between gap-4">
                    <div>
                        <Button variant="ghost" size="sm" className="mb-2 -ml-2" asChild>
                            <Link href="/platform-admin">
                                <ArrowLeft className="h-4 w-4 mr-2" />
                                Retour à la console
                            </Link>
                        </Button>
                        <h1 className="text-3xl font-bold mb-2">
                            {organization.name ?? `Organisation #${organization.id}`}
                        </h1>
                        <p className="text-muted-foreground">
                            {organization.contact_email ?? 'Aucun email de contact'} ·
                            organisation #{organization.id} · créée le{' '}
                            {formatDate(organization.created_at)}
                        </p>
                    </div>
                    <Button variant="outline" onClick={fetchDetail}>
                        <RefreshCw className="h-4 w-4 mr-2" />
                        Actualiser
                    </Button>
                </div>

                <Card>
                    <CardHeader>
                        <div className="flex flex-wrap items-start justify-between gap-3">
                            <div className="space-y-1.5">
                                <CardTitle className="flex flex-wrap items-center gap-2">
                                    <span>
                                        {plan
                                            ? plan.name
                                            : 'Aucun plan attribué'}
                                    </span>
                                    <SubscriptionStatusBadge
                                        status={subscription?.status}
                                    />
                                    {subscription?.cancel_at_period_end && (
                                        <Badge variant="outline">
                                            annulation en fin de période
                                        </Badge>
                                    )}
                                </CardTitle>
                                <CardDescription>
                                    {plan
                                        ? `${formatPrice(plan.price_amount, plan.currency)} · ${BILLING_INTERVAL_LABELS[plan.billing_interval]} · code ${plan.code}`
                                        : 'Attribuez un plan pour activer les quotas de ce client.'}
                                </CardDescription>
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                                <Button onClick={() => setAssignOpen(true)}>
                                    Changer de plan
                                </Button>
                                <Button
                                    variant="outline"
                                    onClick={() => setLimitsOpen(true)}
                                    disabled={!subscription}
                                >
                                    <SlidersHorizontal className="h-4 w-4 mr-2" />
                                    Modifier les limites
                                </Button>
                                <Button
                                    variant="outline"
                                    onClick={handleRenew}
                                    disabled={!subscription || pendingAction !== null}
                                >
                                    {pendingAction === 'renew' ? (
                                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                                    ) : (
                                        <RotateCw className="h-4 w-4 mr-2" />
                                    )}
                                    Renouveler la période
                                </Button>
                                {subscription?.status === 'suspended' ? (
                                    <Button
                                        variant="outline"
                                        onClick={handleReactivate}
                                        disabled={pendingAction !== null}
                                    >
                                        {pendingAction === 'reactivate' ? (
                                            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                                        ) : (
                                            <PlayCircle className="h-4 w-4 mr-2" />
                                        )}
                                        Réactiver
                                    </Button>
                                ) : (
                                    <Button
                                        variant="outline"
                                        onClick={() => setSuspendOpen(true)}
                                        disabled={!subscription}
                                    >
                                        <PauseCircle className="h-4 w-4 mr-2" />
                                        Suspendre
                                    </Button>
                                )}
                                <Button
                                    variant="outline"
                                    onClick={() => setCancelOpen(true)}
                                    disabled={!subscription}
                                >
                                    <Ban className="h-4 w-4 mr-2" />
                                    Annuler
                                </Button>
                            </div>
                        </div>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                            <div>
                                <p className="text-xs text-muted-foreground">
                                    Début de période
                                </p>
                                <p className="text-sm font-medium">
                                    {formatDate(subscription?.current_period_start)}
                                </p>
                            </div>
                            <div>
                                <p className="text-xs text-muted-foreground">
                                    Fin de période
                                </p>
                                <p className="text-sm font-medium">
                                    {formatDate(subscription?.current_period_end)}
                                </p>
                            </div>
                            <div>
                                <p className="text-xs text-muted-foreground">
                                    Fin d&apos;essai
                                </p>
                                <p className="text-sm font-medium">
                                    {formatDate(subscription?.trial_end_at)}
                                </p>
                            </div>
                            <div>
                                <p className="text-xs text-muted-foreground">
                                    Appels simultanés
                                </p>
                                <p className="text-sm font-medium">
                                    {formatLimit(limits.max_concurrent_calls)}
                                </p>
                            </div>
                        </div>

                        {plan && (
                            <>
                                <Separator />
                                <div className="flex flex-wrap items-center gap-2">
                                    <span className="text-xs text-muted-foreground">
                                        Fonctionnalités :
                                    </span>
                                    {FEATURE_KEYS.filter(
                                        (key) => plan.features?.[key] === true,
                                    ).map((key) => (
                                        <Badge key={key} variant="secondary">
                                            {FEATURE_LABELS[key]}
                                        </Badge>
                                    ))}
                                </div>
                            </>
                        )}

                        {subscription?.notes && (
                            <p className="text-sm text-muted-foreground">
                                Note : {subscription.notes}
                            </p>
                        )}
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <div className="flex flex-wrap items-start justify-between gap-3">
                            <div>
                                <CardTitle>Consommation</CardTitle>
                                <CardDescription>
                                    Période du {formatDate(snapshot.period_start)} au{' '}
                                    {formatDate(snapshot.period_end)}
                                </CardDescription>
                            </div>
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={handleRefreshUsage}
                                disabled={pendingAction !== null}
                            >
                                {pendingAction === 'usage' ? (
                                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                                ) : (
                                    <RefreshCw className="h-4 w-4 mr-2" />
                                )}
                                Recalculer
                            </Button>
                        </div>
                    </CardHeader>
                    <CardContent>
                        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
                            {usageItems.map((item) => (
                                <UsageBar
                                    key={item.key}
                                    label={item.label}
                                    used={item.used}
                                    limit={item.limit}
                                    unit={item.unit}
                                    decimal={item.decimal}
                                />
                            ))}
                        </div>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <CardTitle>Utilisateurs</CardTitle>
                        <CardDescription>
                            {formatNumber(detail.users.length)} compte(s) rattaché(s) à
                            cette organisation.
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        {detail.users.length === 0 ? (
                            <p className="text-sm text-muted-foreground">
                                Aucun utilisateur.
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <Table>
                                    <TableHeader>
                                        <TableRow>
                                            <TableHead>Email</TableHead>
                                            <TableHead>Identifiant</TableHead>
                                            <TableHead>Rôle</TableHead>
                                        </TableRow>
                                    </TableHeader>
                                    <TableBody>
                                        {detail.users.map((user) => (
                                            <TableRow key={user.id}>
                                                <TableCell className="font-medium">
                                                    {user.email}
                                                </TableCell>
                                                <TableCell className="font-mono text-xs">
                                                    #{user.id}
                                                </TableCell>
                                                <TableCell>
                                                    {user.is_superuser ? (
                                                        <Badge variant="secondary">
                                                            superutilisateur
                                                        </Badge>
                                                    ) : (
                                                        <Badge variant="outline">
                                                            utilisateur
                                                        </Badge>
                                                    )}
                                                </TableCell>
                                            </TableRow>
                                        ))}
                                    </TableBody>
                                </Table>
                            </div>
                        )}
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <div className="flex flex-wrap items-start justify-between gap-3">
                            <div>
                                <CardTitle>Journal des événements</CardTitle>
                                <CardDescription>
                                    Historique des décisions d&apos;abonnement et des
                                    blocages de quota.
                                </CardDescription>
                            </div>
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={handleLoadEvents}
                                disabled={pendingAction !== null}
                            >
                                {pendingAction === 'events' ? (
                                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                                ) : (
                                    <History className="h-4 w-4 mr-2" />
                                )}
                                Charger l&apos;historique complet
                            </Button>
                        </div>
                    </CardHeader>
                    <CardContent>
                        {events.length === 0 ? (
                            <p className="text-sm text-muted-foreground">
                                Aucun événement enregistré.
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <Table>
                                    <TableHeader>
                                        <TableRow>
                                            <TableHead>Date</TableHead>
                                            <TableHead>Événement</TableHead>
                                            <TableHead>Auteur</TableHead>
                                            <TableHead>Note</TableHead>
                                        </TableRow>
                                    </TableHeader>
                                    <TableBody>
                                        {events.map((event) => (
                                            <TableRow key={event.id}>
                                                <TableCell className="text-sm whitespace-nowrap">
                                                    {formatDateTime(event.created_at)}
                                                </TableCell>
                                                <TableCell>
                                                    <Badge variant="secondary">
                                                        {EVENT_LABELS[event.event_type]}
                                                    </Badge>
                                                </TableCell>
                                                <TableCell className="text-sm">
                                                    {event.actor_user_id
                                                        ? `#${event.actor_user_id}`
                                                        : 'système'}
                                                </TableCell>
                                                <TableCell className="text-sm text-muted-foreground">
                                                    {event.note ?? '—'}
                                                </TableCell>
                                            </TableRow>
                                        ))}
                                    </TableBody>
                                </Table>
                            </div>
                        )}
                    </CardContent>
                </Card>
            </div>

            <AssignPlanDialog
                open={assignOpen}
                onOpenChange={setAssignOpen}
                orgId={orgId}
                plans={plans}
                subscription={subscription}
                onSaved={fetchDetail}
            />
            <LimitOverridesDialog
                open={limitsOpen}
                onOpenChange={setLimitsOpen}
                orgId={orgId}
                overrides={subscription?.limit_overrides}
                plan={plan}
                onSaved={fetchDetail}
            />
            <SuspendOrganizationDialog
                open={suspendOpen}
                onOpenChange={setSuspendOpen}
                orgId={orgId}
                onSaved={fetchDetail}
            />
            <CancelSubscriptionDialog
                open={cancelOpen}
                onOpenChange={setCancelOpen}
                orgId={orgId}
                currentPeriodEnd={subscription?.current_period_end}
                onSaved={fetchDetail}
            />
        </div>
    );
}
