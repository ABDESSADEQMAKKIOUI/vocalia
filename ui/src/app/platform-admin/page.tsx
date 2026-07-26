"use client";

import { ArrowRight, ChevronLeft, ChevronRight, Layers, Plus, RefreshCw, Search } from 'lucide-react';
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from '@/components/ui/select';
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
    getPlatformMetrics,
    listOrganizations,
    listPlans,
    type OrganizationListDto,
    type PlanDto,
    type PlatformMetricsDto,
    type SubscriptionStatus,
} from '@/lib/platformAdminApi';

import { formatDate, formatNumber, STATUS_LABELS } from './components/format';
import { MetricsBanner } from './components/MetricsBanner';
import { ProvisionOrganizationDialog } from './components/ProvisionOrganizationDialog';
import { SubscriptionStatusBadge } from './components/SubscriptionStatusBadge';
import { SuperuserGuard } from './components/SuperuserGuard';
import { UsageBar } from './components/UsageBar';

const ALL_STATUSES = 'all';
const PAGE_SIZE = 25;

const STATUS_OPTIONS: SubscriptionStatus[] = [
    'trialing',
    'active',
    'past_due',
    'suspended',
    'cancelled',
];

export default function PlatformAdminPage() {
    return (
        <SuperuserGuard>
            <PlatformAdminDashboard />
        </SuperuserGuard>
    );
}

/** Rendered only once SuperuserGuard confirmed auth is loaded and superuser. */
function PlatformAdminDashboard() {
    const [metrics, setMetrics] = useState<PlatformMetricsDto | null>(null);
    const [loadingMetrics, setLoadingMetrics] = useState(true);
    const [organizations, setOrganizations] = useState<OrganizationListDto | null>(null);
    const [loadingOrganizations, setLoadingOrganizations] = useState(true);
    const [plans, setPlans] = useState<PlanDto[]>([]);

    const [searchInput, setSearchInput] = useState('');
    const [search, setSearch] = useState('');
    const [status, setStatus] = useState<string>(ALL_STATUSES);
    const [page, setPage] = useState(1);
    const [provisionOpen, setProvisionOpen] = useState(false);

    const fetchMetrics = useCallback(async () => {
        setLoadingMetrics(true);
        try {
            setMetrics(await getPlatformMetrics());
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : 'Échec du chargement des métriques',
            );
        } finally {
            setLoadingMetrics(false);
        }
    }, []);

    const fetchOrganizations = useCallback(async () => {
        setLoadingOrganizations(true);
        try {
            setOrganizations(
                await listOrganizations({
                    search: search || undefined,
                    status:
                        status === ALL_STATUSES
                            ? undefined
                            : (status as SubscriptionStatus),
                    page,
                    limit: PAGE_SIZE,
                }),
            );
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec du chargement des clients',
            );
        } finally {
            setLoadingOrganizations(false);
        }
    }, [search, status, page]);

    const fetchPlans = useCallback(async () => {
        try {
            setPlans(await listPlans());
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec du chargement des plans',
            );
        }
    }, []);

    useEffect(() => {
        fetchMetrics();
        fetchPlans();
    }, [fetchMetrics, fetchPlans]);

    useEffect(() => {
        fetchOrganizations();
    }, [fetchOrganizations]);

    const handleSearch = (e: React.FormEvent) => {
        e.preventDefault();
        setPage(1);
        setSearch(searchInput.trim());
    };

    const handleStatusChange = (value: string) => {
        setPage(1);
        setStatus(value);
    };

    const handleRefresh = () => {
        fetchMetrics();
        fetchOrganizations();
    };

    const rows = organizations?.organizations ?? [];
    const totalPages = organizations?.total_pages ?? 0;

    return (
        <div className="min-h-screen">
            <div className="container mx-auto px-4 py-8 space-y-8">
                <div className="flex flex-wrap items-start justify-between gap-4">
                    <div>
                        <h1 className="text-3xl font-bold mb-2">Console plateforme</h1>
                        <p className="text-muted-foreground">
                            Suivez vos clients, leurs abonnements et leur consommation.
                        </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                        <Button variant="outline" onClick={handleRefresh}>
                            <RefreshCw className="h-4 w-4 mr-2" />
                            Actualiser
                        </Button>
                        <Button variant="outline" asChild>
                            <Link href="/platform-admin/plans">
                                <Layers className="h-4 w-4 mr-2" />
                                Catalogue de plans
                            </Link>
                        </Button>
                        <Button onClick={() => setProvisionOpen(true)}>
                            <Plus className="h-4 w-4 mr-2" />
                            Nouveau client
                        </Button>
                    </div>
                </div>

                <MetricsBanner metrics={metrics} loading={loadingMetrics} />

                <Card>
                    <CardHeader>
                        <div className="flex flex-wrap items-start justify-between gap-3">
                            <div>
                                <CardTitle>Clients</CardTitle>
                                <CardDescription>
                                    {organizations
                                        ? `${formatNumber(organizations.total_count)} organisation(s)`
                                        : 'Chargement…'}
                                </CardDescription>
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                                <form onSubmit={handleSearch} className="flex gap-2">
                                    <Input
                                        value={searchInput}
                                        onChange={(e) => setSearchInput(e.target.value)}
                                        placeholder="Rechercher un client"
                                        className="w-56"
                                    />
                                    <Button type="submit" variant="outline">
                                        <Search className="h-4 w-4" />
                                    </Button>
                                </form>
                                <Select value={status} onValueChange={handleStatusChange}>
                                    <SelectTrigger className="w-48">
                                        <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
                                        <SelectItem value={ALL_STATUSES}>
                                            Tous les statuts
                                        </SelectItem>
                                        {STATUS_OPTIONS.map((value) => (
                                            <SelectItem key={value} value={value}>
                                                {STATUS_LABELS[value]}
                                            </SelectItem>
                                        ))}
                                    </SelectContent>
                                </Select>
                            </div>
                        </div>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        {loadingOrganizations ? (
                            <div className="space-y-3">
                                <Skeleton className="h-10 w-full" />
                                <Skeleton className="h-10 w-full" />
                                <Skeleton className="h-10 w-full" />
                            </div>
                        ) : rows.length === 0 ? (
                            <p className="text-sm text-muted-foreground">
                                Aucun client ne correspond à cette recherche.
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <Table>
                                    <TableHeader>
                                        <TableRow>
                                            <TableHead>Client</TableHead>
                                            <TableHead>Contact</TableHead>
                                            <TableHead>Plan</TableHead>
                                            <TableHead>Statut</TableHead>
                                            <TableHead>Voix</TableHead>
                                            <TableHead>WhatsApp</TableHead>
                                            <TableHead>Fin de période</TableHead>
                                            <TableHead className="text-right">
                                                Actions
                                            </TableHead>
                                        </TableRow>
                                    </TableHeader>
                                    <TableBody>
                                        {rows.map((organization) => (
                                            <TableRow key={organization.id}>
                                                <TableCell className="font-medium">
                                                    <div className="flex flex-col">
                                                        <span>
                                                            {organization.name ??
                                                                `Organisation #${organization.id}`}
                                                        </span>
                                                        <span className="text-xs text-muted-foreground">
                                                            {organization.users_count}{' '}
                                                            utilisateur(s) · créée le{' '}
                                                            {formatDate(
                                                                organization.created_at,
                                                            )}
                                                        </span>
                                                    </div>
                                                </TableCell>
                                                <TableCell className="text-sm">
                                                    {organization.contact_email ?? '—'}
                                                </TableCell>
                                                <TableCell className="text-sm">
                                                    {organization.plan_name ??
                                                        organization.plan_code ??
                                                        '—'}
                                                </TableCell>
                                                <TableCell>
                                                    <SubscriptionStatusBadge
                                                        status={organization.status}
                                                    />
                                                </TableCell>
                                                <TableCell>
                                                    <UsageBar
                                                        used={
                                                            organization.usage
                                                                .voice_minutes
                                                        }
                                                        limit={
                                                            organization.limits
                                                                .max_voice_minutes
                                                        }
                                                        unit=" min"
                                                        decimal
                                                    />
                                                </TableCell>
                                                <TableCell>
                                                    <UsageBar
                                                        used={
                                                            organization.usage
                                                                .whatsapp_messages
                                                        }
                                                        limit={
                                                            organization.limits
                                                                .max_whatsapp_messages
                                                        }
                                                    />
                                                </TableCell>
                                                <TableCell className="text-sm">
                                                    {formatDate(
                                                        organization.current_period_end,
                                                    )}
                                                </TableCell>
                                                <TableCell className="text-right">
                                                    <Button variant="ghost" size="sm" asChild>
                                                        <Link
                                                            href={`/platform-admin/organizations/${organization.id}`}
                                                        >
                                                            Ouvrir
                                                            <ArrowRight className="h-4 w-4 ml-2" />
                                                        </Link>
                                                    </Button>
                                                </TableCell>
                                            </TableRow>
                                        ))}
                                    </TableBody>
                                </Table>
                            </div>
                        )}

                        {totalPages > 1 && (
                            <div className="flex items-center justify-between">
                                <p className="text-sm text-muted-foreground">
                                    Page {organizations?.page ?? page} sur {totalPages}
                                </p>
                                <div className="flex gap-2">
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => setPage((current) => current - 1)}
                                        disabled={page <= 1 || loadingOrganizations}
                                    >
                                        <ChevronLeft className="h-4 w-4" />
                                        Précédent
                                    </Button>
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => setPage((current) => current + 1)}
                                        disabled={
                                            page >= totalPages || loadingOrganizations
                                        }
                                    >
                                        Suivant
                                        <ChevronRight className="h-4 w-4" />
                                    </Button>
                                </div>
                            </div>
                        )}
                    </CardContent>
                </Card>
            </div>

            <ProvisionOrganizationDialog
                open={provisionOpen}
                onOpenChange={setProvisionOpen}
                plans={plans}
                onCreated={handleRefresh}
            />
        </div>
    );
}
