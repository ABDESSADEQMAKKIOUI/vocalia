"use client";

import { ArrowLeft, Loader2, Pencil, Plus, Sparkles, Trash2 } from 'lucide-react';
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from '@/components/ui/table';
import {
    deactivatePlan,
    listPlans,
    type PlanDto,
    seedDefaultPlans,
    updatePlan,
} from '@/lib/platformAdminApi';

import {
    BILLING_INTERVAL_LABELS,
    FEATURE_KEYS,
    FEATURE_LABELS,
    formatLimit,
    formatNumber,
    formatPrice,
    LIMIT_KEYS,
    LIMIT_LABELS,
} from '../components/format';
import { PlanFormDialog } from '../components/PlanFormDialog';
import { SuperuserGuard } from '../components/SuperuserGuard';

export default function PlatformAdminPlansPage() {
    return (
        <SuperuserGuard>
            <PlansCatalog />
        </SuperuserGuard>
    );
}

/** Rendered only once SuperuserGuard confirmed auth is loaded and superuser. */
function PlansCatalog() {
    const [plans, setPlans] = useState<PlanDto[]>([]);
    const [loading, setLoading] = useState(true);
    const [seeding, setSeeding] = useState(false);
    const [createOpen, setCreateOpen] = useState(false);
    const [editTarget, setEditTarget] = useState<PlanDto | null>(null);
    const [deactivateTarget, setDeactivateTarget] = useState<PlanDto | null>(null);

    const fetchPlans = useCallback(async () => {
        setLoading(true);
        try {
            setPlans(await listPlans({ includeInactive: true }));
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec du chargement des plans',
            );
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        fetchPlans();
    }, [fetchPlans]);

    const handleSeedDefaults = async () => {
        setSeeding(true);
        try {
            await seedDefaultPlans();
            toast.success('Plans par défaut créés');
            await fetchPlans();
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : 'Échec de la création des plans par défaut',
            );
        } finally {
            setSeeding(false);
        }
    };

    const handleToggleActive = async (plan: PlanDto, isActive: boolean) => {
        try {
            await updatePlan(plan.id, { is_active: isActive });
            toast.success(isActive ? 'Plan activé' : 'Plan désactivé');
            await fetchPlans();
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : 'Échec de la mise à jour du plan',
            );
        }
    };

    const handleConfirmDeactivate = async () => {
        if (!deactivateTarget) return;
        try {
            await deactivatePlan(deactivateTarget.id);
            toast.success('Plan désactivé');
            setDeactivateTarget(null);
            await fetchPlans();
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : 'Échec de la désactivation du plan',
            );
        }
    };

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
                        <h1 className="text-3xl font-bold mb-2">Catalogue de plans</h1>
                        <p className="text-muted-foreground">
                            Définissez les offres commerciales, leurs limites et les
                            fonctionnalités incluses.
                        </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                        <Button
                            variant="outline"
                            onClick={handleSeedDefaults}
                            disabled={seeding}
                        >
                            {seeding ? (
                                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                            ) : (
                                <Sparkles className="h-4 w-4 mr-2" />
                            )}
                            Créer les plans par défaut
                        </Button>
                        <Button onClick={() => setCreateOpen(true)}>
                            <Plus className="h-4 w-4 mr-2" />
                            Nouveau plan
                        </Button>
                    </div>
                </div>

                <Card>
                    <CardHeader>
                        <CardTitle>Plans</CardTitle>
                        <CardDescription>
                            Une limite vide signifie illimité. La désactivation est
                            refusée tant que des clients sont abonnés au plan.
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        {loading ? (
                            <div className="space-y-3">
                                <Skeleton className="h-10 w-full" />
                                <Skeleton className="h-10 w-full" />
                                <Skeleton className="h-10 w-full" />
                            </div>
                        ) : plans.length === 0 ? (
                            <p className="text-sm text-muted-foreground">
                                Aucun plan pour le moment. Créez-en un ou générez les
                                plans par défaut.
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <Table>
                                    <TableHeader>
                                        <TableRow>
                                            <TableHead>Plan</TableHead>
                                            <TableHead>Prix</TableHead>
                                            <TableHead>Essai</TableHead>
                                            <TableHead>Limites</TableHead>
                                            <TableHead>Fonctionnalités</TableHead>
                                            <TableHead>Visibilité</TableHead>
                                            <TableHead>Actif</TableHead>
                                            <TableHead className="text-right">
                                                Actions
                                            </TableHead>
                                        </TableRow>
                                    </TableHeader>
                                    <TableBody>
                                        {plans.map((plan) => (
                                            <TableRow key={plan.id}>
                                                <TableCell className="font-medium">
                                                    <div className="flex flex-col">
                                                        <span>{plan.name}</span>
                                                        <span className="font-mono text-xs text-muted-foreground">
                                                            {plan.code}
                                                        </span>
                                                    </div>
                                                </TableCell>
                                                <TableCell className="text-sm">
                                                    <div className="flex flex-col">
                                                        <span>
                                                            {formatPrice(
                                                                plan.price_amount,
                                                                plan.currency,
                                                            )}
                                                        </span>
                                                        <span className="text-xs text-muted-foreground">
                                                            {BILLING_INTERVAL_LABELS[plan.billing_interval]}
                                                        </span>
                                                    </div>
                                                </TableCell>
                                                <TableCell className="text-sm">
                                                    {plan.trial_days > 0
                                                        ? `${formatNumber(plan.trial_days)} j`
                                                        : '—'}
                                                </TableCell>
                                                <TableCell>
                                                    <div className="space-y-0.5">
                                                        {LIMIT_KEYS.map((key) => (
                                                            <p
                                                                key={key}
                                                                className="text-xs text-muted-foreground whitespace-nowrap"
                                                            >
                                                                {LIMIT_LABELS[key]} :{' '}
                                                                {formatLimit(plan[key])}
                                                            </p>
                                                        ))}
                                                    </div>
                                                </TableCell>
                                                <TableCell>
                                                    <div className="flex flex-wrap gap-1 max-w-56">
                                                        {FEATURE_KEYS.filter(
                                                            (key) =>
                                                                plan.features?.[key] ===
                                                                true,
                                                        ).map((key) => (
                                                            <Badge
                                                                key={key}
                                                                variant="secondary"
                                                            >
                                                                {FEATURE_LABELS[key]}
                                                            </Badge>
                                                        ))}
                                                    </div>
                                                </TableCell>
                                                <TableCell>
                                                    {plan.is_public ? (
                                                        <Badge variant="outline">
                                                            public
                                                        </Badge>
                                                    ) : (
                                                        <Badge variant="secondary">
                                                            privé
                                                        </Badge>
                                                    )}
                                                </TableCell>
                                                <TableCell>
                                                    <Switch
                                                        checked={plan.is_active}
                                                        onCheckedChange={(checked) =>
                                                            handleToggleActive(
                                                                plan,
                                                                checked,
                                                            )
                                                        }
                                                        aria-label="Activer le plan"
                                                    />
                                                </TableCell>
                                                <TableCell className="text-right">
                                                    <div className="flex justify-end gap-1">
                                                        <Button
                                                            variant="ghost"
                                                            size="sm"
                                                            onClick={() =>
                                                                setEditTarget(plan)
                                                            }
                                                            title="Modifier"
                                                        >
                                                            <Pencil className="h-4 w-4" />
                                                        </Button>
                                                        <Button
                                                            variant="ghost"
                                                            size="sm"
                                                            onClick={() =>
                                                                setDeactivateTarget(plan)
                                                            }
                                                            disabled={!plan.is_active}
                                                            title="Désactiver"
                                                        >
                                                            <Trash2 className="h-4 w-4 text-destructive" />
                                                        </Button>
                                                    </div>
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

            <PlanFormDialog
                open={createOpen}
                onOpenChange={setCreateOpen}
                existing={null}
                onSaved={fetchPlans}
            />
            <PlanFormDialog
                open={editTarget !== null}
                onOpenChange={(open) => !open && setEditTarget(null)}
                existing={editTarget}
                onSaved={fetchPlans}
            />

            <AlertDialog
                open={!!deactivateTarget}
                onOpenChange={(open) => !open && setDeactivateTarget(null)}
            >
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>Désactiver le plan ?</AlertDialogTitle>
                        <AlertDialogDescription>
                            « {deactivateTarget?.name} » ne sera plus proposé aux
                            nouveaux clients. L&apos;opération est refusée si des
                            organisations y sont encore abonnées.
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel>Annuler</AlertDialogCancel>
                        <AlertDialogAction onClick={handleConfirmDeactivate}>
                            Désactiver
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>
        </div>
    );
}
