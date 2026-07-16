"use client";

import { MessageSquareText, Pencil, Plus, Trash2 } from 'lucide-react';
import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { getWorkflowsSummaryApiV1WorkflowSummaryGet } from '@/client/sdk.gen';
import type { WorkflowSummaryResponse } from '@/client/types.gen';
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
import {
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
} from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from '@/components/ui/table';
import { detailFromError } from '@/lib/apiError';
import { useAuth } from '@/lib/auth';
import {
    deleteMessagingAddress,
    deleteMessagingConfiguration,
    getWebhookInfo,
    listMessagingConfigurations,
    listSuppressions,
    type MessagingAddress,
    type MessagingConfiguration,
    type MessagingSuppression,
    type WebhookInfo,
} from '@/lib/messagingApi';

import { AddressFormDialog } from './components/AddressFormDialog';
import { ConfigFormDialog } from './components/ConfigFormDialog';
import { SuppressionsCard } from './components/SuppressionsCard';
import { WebhookInfoCard } from './components/WebhookInfoCard';

export default function WhatsAppConfigurationsPage() {
    const { user, getAccessToken, redirectToLogin, loading: authLoading } = useAuth();

    const [configurations, setConfigurations] = useState<MessagingConfiguration[]>([]);
    const [loadingConfigs, setLoadingConfigs] = useState(true);
    const [webhookInfo, setWebhookInfo] = useState<WebhookInfo | null>(null);
    const [loadingWebhook, setLoadingWebhook] = useState(true);
    const [suppressions, setSuppressions] = useState<MessagingSuppression[]>([]);
    const [loadingSuppressions, setLoadingSuppressions] = useState(true);
    const [workflows, setWorkflows] = useState<WorkflowSummaryResponse[]>([]);
    const [isLoadingWorkflows, setIsLoadingWorkflows] = useState(true);

    const [createOpen, setCreateOpen] = useState(false);
    const [editTarget, setEditTarget] = useState<MessagingConfiguration | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<MessagingConfiguration | null>(null);
    const [addressDialog, setAddressDialog] = useState<{
        configId: number;
        existing: MessagingAddress | null;
    } | null>(null);
    const [deleteAddressTarget, setDeleteAddressTarget] =
        useState<MessagingAddress | null>(null);
    const hasFetched = useRef(false);

    // Redirect if not authenticated
    useEffect(() => {
        if (!authLoading && !user) {
            redirectToLogin();
        }
    }, [authLoading, user, redirectToLogin]);

    const fetchConfigurations = useCallback(async () => {
        setLoadingConfigs(true);
        try {
            setConfigurations(await listMessagingConfigurations());
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : 'Échec du chargement des configurations',
            );
        } finally {
            setLoadingConfigs(false);
        }
    }, []);

    const fetchWebhook = useCallback(async () => {
        setLoadingWebhook(true);
        try {
            setWebhookInfo(await getWebhookInfo());
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : 'Échec du chargement des informations du webhook',
            );
        } finally {
            setLoadingWebhook(false);
        }
    }, []);

    const fetchSuppressions = useCallback(async () => {
        setLoadingSuppressions(true);
        try {
            setSuppressions(await listSuppressions());
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : "Échec du chargement de la liste d'exclusion",
            );
        } finally {
            setLoadingSuppressions(false);
        }
    }, []);

    // Same pattern as the campaign create page: workflow summaries through the
    // generated SDK with an explicit Bearer token.
    const fetchWorkflows = useCallback(async () => {
        try {
            const accessToken = await getAccessToken();
            const response = await getWorkflowsSummaryApiV1WorkflowSummaryGet({
                headers: {
                    Authorization: `Bearer ${accessToken}`,
                },
                query: {
                    status: 'active',
                },
            });
            if (response.error) {
                throw new Error(
                    detailFromError(response.error, 'Échec du chargement des workflows'),
                );
            }
            if (response.data) {
                setWorkflows(response.data);
            }
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec du chargement des workflows',
            );
        } finally {
            setIsLoadingWorkflows(false);
        }
    }, [getAccessToken]);

    // Fetch everything once when auth is ready
    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        fetchConfigurations();
        fetchWebhook();
        fetchSuppressions();
        fetchWorkflows();
    }, [
        authLoading,
        user,
        fetchConfigurations,
        fetchWebhook,
        fetchSuppressions,
        fetchWorkflows,
    ]);

    const handleConfirmDeleteConfig = async () => {
        if (!deleteTarget) return;
        try {
            await deleteMessagingConfiguration(deleteTarget.id);
            toast.success('Configuration supprimée');
            setDeleteTarget(null);
            await fetchConfigurations();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec de la suppression',
            );
        }
    };

    const handleConfirmDeleteAddress = async () => {
        if (!deleteAddressTarget) return;
        try {
            await deleteMessagingAddress(deleteAddressTarget.id);
            toast.success('Numéro supprimé');
            setDeleteAddressTarget(null);
            await fetchConfigurations();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec de la suppression',
            );
        }
    };

    const workflowLabel = (workflowId?: number | null) => {
        if (workflowId === null || workflowId === undefined) {
            return <span className="text-muted-foreground">—</span>;
        }
        const workflow = workflows.find((w) => w.id === workflowId);
        return workflow ? `${workflow.name} (#${workflow.id})` : `#${workflowId}`;
    };

    if (authLoading || !user) {
        return (
            <div className="min-h-screen flex items-center justify-center">
                <div className="space-y-4">
                    <Skeleton className="h-12 w-64" />
                    <Skeleton className="h-64 w-96" />
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen">
            <div className="container mx-auto px-4 py-8 space-y-8">
                <div className="flex flex-wrap items-start justify-between gap-4">
                    <div>
                        <h1 className="text-3xl font-bold mb-2">WhatsApp</h1>
                        <p className="text-muted-foreground">
                            Connectez vos comptes WhatsApp Business (Meta), gérez vos
                            numéros, le webhook entrant et la liste d&apos;exclusion.
                        </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                        <Button variant="outline" asChild>
                            <Link href="/whatsapp/templates">
                                <MessageSquareText className="h-4 w-4 mr-2" />
                                Gérer les modèles
                            </Link>
                        </Button>
                        <Button onClick={() => setCreateOpen(true)}>
                            <Plus className="h-4 w-4 mr-2" />
                            Ajouter une configuration
                        </Button>
                    </div>
                </div>

                <section className="space-y-3">
                    <h2 className="text-xl font-semibold">Configurations</h2>
                    {loadingConfigs ? (
                        <div className="grid gap-3">
                            <Skeleton className="h-32 w-full" />
                            <Skeleton className="h-32 w-full" />
                        </div>
                    ) : configurations.length === 0 ? (
                        <Card>
                            <CardHeader>
                                <CardTitle>Aucune configuration WhatsApp</CardTitle>
                                <CardDescription>
                                    Ajoutez-en une pour envoyer et recevoir des messages
                                    WhatsApp.
                                </CardDescription>
                            </CardHeader>
                            <CardContent>
                                <Button onClick={() => setCreateOpen(true)}>
                                    <Plus className="h-4 w-4 mr-2" />
                                    Ajouter une configuration
                                </Button>
                            </CardContent>
                        </Card>
                    ) : (
                        <div className="grid gap-4">
                            {configurations.map((config) => (
                                <Card key={config.id}>
                                    <CardHeader>
                                        <div className="flex flex-wrap items-start justify-between gap-2">
                                            <div className="min-w-0 space-y-1.5">
                                                <CardTitle className="flex flex-wrap items-center gap-2">
                                                    <span className="truncate">
                                                        {config.name}
                                                    </span>
                                                    <Badge variant="secondary">
                                                        {config.provider}
                                                    </Badge>
                                                    {config.is_active ? (
                                                        <Badge>Active</Badge>
                                                    ) : (
                                                        <Badge variant="outline">
                                                            Inactive
                                                        </Badge>
                                                    )}
                                                </CardTitle>
                                                <CardDescription className="font-mono text-xs">
                                                    WABA ID :{' '}
                                                    {config.credentials?.waba_id ?? '—'}
                                                </CardDescription>
                                            </div>
                                            <div className="flex items-center gap-1">
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    onClick={() => setEditTarget(config)}
                                                    title="Modifier"
                                                >
                                                    <Pencil className="h-4 w-4" />
                                                </Button>
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    onClick={() => setDeleteTarget(config)}
                                                    title="Supprimer"
                                                >
                                                    <Trash2 className="h-4 w-4 text-destructive" />
                                                </Button>
                                            </div>
                                        </div>
                                    </CardHeader>
                                    <CardContent className="space-y-3">
                                        <div className="flex flex-wrap items-center justify-between gap-2">
                                            <p className="text-sm font-medium">
                                                Numéros ({config.addresses.length})
                                            </p>
                                            <Button
                                                variant="outline"
                                                size="sm"
                                                onClick={() =>
                                                    setAddressDialog({
                                                        configId: config.id,
                                                        existing: null,
                                                    })
                                                }
                                            >
                                                <Plus className="h-4 w-4 mr-2" />
                                                Ajouter un numéro
                                            </Button>
                                        </div>
                                        {config.addresses.length === 0 ? (
                                            <p className="text-sm text-muted-foreground">
                                                Aucun numéro pour cette configuration.
                                            </p>
                                        ) : (
                                            <div className="overflow-x-auto">
                                                <Table>
                                                    <TableHeader>
                                                        <TableRow>
                                                            <TableHead>
                                                                Numéro affiché
                                                            </TableHead>
                                                            <TableHead>
                                                                Phone number ID
                                                            </TableHead>
                                                            <TableHead>
                                                                Workflow entrant
                                                            </TableHead>
                                                            <TableHead>Statut</TableHead>
                                                            <TableHead className="text-right">
                                                                Actions
                                                            </TableHead>
                                                        </TableRow>
                                                    </TableHeader>
                                                    <TableBody>
                                                        {config.addresses.map((address) => (
                                                            <TableRow key={address.id}>
                                                                <TableCell className="font-medium">
                                                                    {address.address}
                                                                </TableCell>
                                                                <TableCell className="font-mono text-xs">
                                                                    {address.external_id}
                                                                </TableCell>
                                                                <TableCell>
                                                                    {workflowLabel(
                                                                        address.inbound_workflow_id,
                                                                    )}
                                                                </TableCell>
                                                                <TableCell>
                                                                    {address.is_active ? (
                                                                        <Badge>Actif</Badge>
                                                                    ) : (
                                                                        <Badge variant="outline">
                                                                            Inactif
                                                                        </Badge>
                                                                    )}
                                                                </TableCell>
                                                                <TableCell className="text-right">
                                                                    <div className="flex justify-end gap-1">
                                                                        <Button
                                                                            variant="ghost"
                                                                            size="sm"
                                                                            onClick={() =>
                                                                                setAddressDialog({
                                                                                    configId:
                                                                                        config.id,
                                                                                    existing:
                                                                                        address,
                                                                                })
                                                                            }
                                                                            title="Modifier"
                                                                        >
                                                                            <Pencil className="h-4 w-4" />
                                                                        </Button>
                                                                        <Button
                                                                            variant="ghost"
                                                                            size="sm"
                                                                            onClick={() =>
                                                                                setDeleteAddressTarget(
                                                                                    address,
                                                                                )
                                                                            }
                                                                            title="Supprimer"
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
                            ))}
                        </div>
                    )}
                </section>

                <WebhookInfoCard info={webhookInfo} loading={loadingWebhook} />

                <SuppressionsCard
                    suppressions={suppressions}
                    loading={loadingSuppressions}
                    onChanged={fetchSuppressions}
                />
            </div>

            <ConfigFormDialog
                open={createOpen}
                onOpenChange={setCreateOpen}
                existing={null}
                workflows={workflows}
                isLoadingWorkflows={isLoadingWorkflows}
                onSaved={fetchConfigurations}
            />
            <ConfigFormDialog
                open={editTarget !== null}
                onOpenChange={(open) => !open && setEditTarget(null)}
                existing={editTarget}
                workflows={workflows}
                isLoadingWorkflows={isLoadingWorkflows}
                onSaved={fetchConfigurations}
            />
            <AddressFormDialog
                open={addressDialog !== null}
                onOpenChange={(open) => !open && setAddressDialog(null)}
                configId={addressDialog?.configId ?? null}
                existing={addressDialog?.existing ?? null}
                workflows={workflows}
                isLoadingWorkflows={isLoadingWorkflows}
                onSaved={fetchConfigurations}
            />

            <AlertDialog
                open={!!deleteTarget}
                onOpenChange={(open) => !open && setDeleteTarget(null)}
            >
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>Supprimer la configuration ?</AlertDialogTitle>
                        <AlertDialogDescription>
                            « {deleteTarget?.name} » et tous ses numéros seront supprimés.
                            Les campagnes WhatsApp qui utilisent cette configuration ne
                            pourront plus envoyer de messages.
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel>Annuler</AlertDialogCancel>
                        <AlertDialogAction onClick={handleConfirmDeleteConfig}>
                            Supprimer
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>

            <AlertDialog
                open={!!deleteAddressTarget}
                onOpenChange={(open) => !open && setDeleteAddressTarget(null)}
            >
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>Supprimer le numéro ?</AlertDialogTitle>
                        <AlertDialogDescription>
                            Le numéro {deleteAddressTarget?.address} sera supprimé et ne
                            pourra plus envoyer ni recevoir de messages WhatsApp.
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel>Annuler</AlertDialogCancel>
                        <AlertDialogAction onClick={handleConfirmDeleteAddress}>
                            Supprimer
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>
        </div>
    );
}
