"use client";

import { MessageSquareText, Pencil, Plus, RefreshCw, Send, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
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
import {
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
} from '@/components/ui/card';
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
import { useAuth } from '@/lib/auth';
import {
    deleteWhatsAppTemplate,
    listMessagingConfigurations,
    listWhatsAppTemplates,
    type MessagingConfiguration,
    submitWhatsAppTemplate,
    syncWhatsAppTemplates,
    type WhatsAppTemplate,
} from '@/lib/messagingApi';

import { TemplateBuilderDialog } from './components/TemplateBuilderDialog';
import { QualityDot, TemplateStatusBadge } from './components/TemplateStatusBadge';
import {
    extractRows,
    LOCALLY_EDITABLE_STATUSES,
    SUBMITTABLE_STATUSES,
} from './components/templateUtils';

export default function WhatsAppTemplatesPage() {
    const { user, redirectToLogin, loading: authLoading } = useAuth();

    const [configurations, setConfigurations] = useState<MessagingConfiguration[]>([]);
    const [selectedConfigId, setSelectedConfigId] = useState<number | null>(null);
    const [templates, setTemplates] = useState<WhatsAppTemplate[]>([]);
    const [loadingConfigs, setLoadingConfigs] = useState(true);
    const [loadingTemplates, setLoadingTemplates] = useState(false);
    const [syncing, setSyncing] = useState(false);
    const [builderOpen, setBuilderOpen] = useState(false);
    const [editTarget, setEditTarget] = useState<WhatsAppTemplate | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<WhatsAppTemplate | null>(null);
    const [submittingId, setSubmittingId] = useState<number | null>(null);
    const hasFetchedConfigs = useRef(false);

    // Redirect if not authenticated
    useEffect(() => {
        if (!authLoading && !user) {
            redirectToLogin();
        }
    }, [authLoading, user, redirectToLogin]);

    // Fetch configurations once when auth is ready
    useEffect(() => {
        if (authLoading || !user || hasFetchedConfigs.current) return;
        hasFetchedConfigs.current = true;

        const fetchConfigurations = async () => {
            setLoadingConfigs(true);
            try {
                const data: unknown = await listMessagingConfigurations();
                const rows = extractRows<MessagingConfiguration>(data, 'configurations');
                setConfigurations(rows);
                if (rows.length > 0) {
                    setSelectedConfigId(rows[0].id);
                }
            } catch (err) {
                toast.error(
                    err instanceof Error
                        ? err.message
                        : 'Échec du chargement des configurations',
                );
            } finally {
                setLoadingConfigs(false);
            }
        };

        fetchConfigurations();
    }, [authLoading, user]);

    const fetchTemplates = useCallback(async () => {
        if (authLoading || !user || selectedConfigId === null) return;
        setLoadingTemplates(true);
        try {
            const data: unknown = await listWhatsAppTemplates({
                configurationId: selectedConfigId,
            });
            setTemplates(extractRows<WhatsAppTemplate>(data, 'templates'));
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec du chargement des modèles',
            );
        } finally {
            setLoadingTemplates(false);
        }
    }, [authLoading, user, selectedConfigId]);

    useEffect(() => {
        fetchTemplates();
    }, [fetchTemplates]);

    const handleSync = async () => {
        if (selectedConfigId === null) return;
        setSyncing(true);
        try {
            const counts = (await syncWhatsAppTemplates(selectedConfigId)) as unknown as {
                fetched?: number;
                created?: number;
                updated?: number;
            } | null;
            if (counts && typeof counts.fetched === 'number') {
                toast.success(
                    `Synchronisation terminée : ${counts.fetched} récupérés, ` +
                    `${counts.created ?? 0} créés, ${counts.updated ?? 0} mis à jour`,
                );
            } else {
                toast.success('Synchronisation terminée');
            }
            await fetchTemplates();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec de la synchronisation',
            );
        } finally {
            setSyncing(false);
        }
    };

    const handleSubmit = async (template: WhatsAppTemplate) => {
        setSubmittingId(template.id);
        try {
            await submitWhatsAppTemplate(template.id);
            toast.success(`« ${template.name} » soumis à Meta pour validation`);
            await fetchTemplates();
        } catch (err) {
            toast.error(err instanceof Error ? err.message : 'Échec de la soumission');
        } finally {
            setSubmittingId(null);
        }
    };

    const handleConfirmDelete = async () => {
        if (!deleteTarget) return;
        try {
            await deleteWhatsAppTemplate(deleteTarget.id);
            toast.success('Modèle supprimé');
            setDeleteTarget(null);
            await fetchTemplates();
        } catch (err) {
            toast.error(err instanceof Error ? err.message : 'Échec de la suppression');
        }
    };

    const openCreate = () => {
        setEditTarget(null);
        setBuilderOpen(true);
    };

    const openEdit = (template: WhatsAppTemplate) => {
        setEditTarget(template);
        setBuilderOpen(true);
    };

    const formatDate = (value?: string | null) => {
        if (!value) return '—';
        return new Date(value).toLocaleDateString('fr-FR', {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
        });
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
            <div className="container mx-auto px-4 py-8">
                <div className="flex flex-wrap items-start justify-between gap-4 mb-6">
                    <div>
                        <h1 className="text-3xl font-bold mb-2">Modèles WhatsApp</h1>
                        <p className="text-muted-foreground">
                            Composez vos modèles de message, soumettez-les à Meta et suivez
                            leur statut de validation.
                        </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                        <Select
                            value={selectedConfigId !== null ? String(selectedConfigId) : undefined}
                            onValueChange={(value) => setSelectedConfigId(Number(value))}
                            disabled={loadingConfigs || configurations.length === 0}
                        >
                            <SelectTrigger className="w-[240px]">
                                <SelectValue
                                    placeholder={
                                        loadingConfigs ? 'Chargement…' : 'Configuration'
                                    }
                                />
                            </SelectTrigger>
                            <SelectContent>
                                {configurations.map((configuration) => (
                                    <SelectItem
                                        key={configuration.id}
                                        value={String(configuration.id)}
                                    >
                                        {configuration.name}
                                    </SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                        <Button
                            variant="outline"
                            onClick={handleSync}
                            disabled={syncing || selectedConfigId === null}
                        >
                            <RefreshCw
                                className={`h-4 w-4 mr-2 ${syncing ? 'animate-spin' : ''}`}
                            />
                            Synchroniser avec Meta
                        </Button>
                        <Button onClick={openCreate} disabled={selectedConfigId === null}>
                            <Plus className="h-4 w-4 mr-2" />
                            Nouveau modèle
                        </Button>
                    </div>
                </div>

                {!loadingConfigs && configurations.length === 0 ? (
                    <Card>
                        <CardHeader>
                            <CardTitle>Aucune configuration de messagerie</CardTitle>
                            <CardDescription>
                                Créez d&apos;abord une configuration WhatsApp pour composer et
                                synchroniser vos modèles de message.
                            </CardDescription>
                        </CardHeader>
                    </Card>
                ) : (
                    <Card>
                        <CardHeader>
                            <CardTitle>Modèles</CardTitle>
                            <CardDescription>
                                Les brouillons sont stockés localement jusqu&apos;à leur
                                soumission ; après soumission, Meta contrôle le statut.
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            {loadingConfigs || loadingTemplates ? (
                                <div className="animate-pulse space-y-3">
                                    {[...Array(4)].map((_, i) => (
                                        <div key={i} className="h-12 bg-muted rounded"></div>
                                    ))}
                                </div>
                            ) : templates.length === 0 ? (
                                <div className="text-center py-12">
                                    <MessageSquareText className="w-12 h-12 text-muted-foreground mx-auto mb-4" />
                                    <p className="text-muted-foreground mb-4">
                                        Aucun modèle pour cette configuration
                                    </p>
                                    <div className="flex justify-center gap-2">
                                        <Button onClick={openCreate}>
                                            <Plus className="h-4 w-4 mr-2" />
                                            Nouveau modèle
                                        </Button>
                                        <Button variant="outline" onClick={handleSync} disabled={syncing}>
                                            <RefreshCw
                                                className={`h-4 w-4 mr-2 ${syncing ? 'animate-spin' : ''}`}
                                            />
                                            Synchroniser avec Meta
                                        </Button>
                                    </div>
                                </div>
                            ) : (
                                <div className="overflow-x-auto">
                                    <Table>
                                        <TableHeader>
                                            <TableRow>
                                                <TableHead>Nom</TableHead>
                                                <TableHead>Langue</TableHead>
                                                <TableHead>Catégorie</TableHead>
                                                <TableHead>Statut</TableHead>
                                                <TableHead>Qualité</TableHead>
                                                <TableHead>Mis à jour</TableHead>
                                                <TableHead className="text-right">Actions</TableHead>
                                            </TableRow>
                                        </TableHeader>
                                        <TableBody>
                                            {templates.map((template) => {
                                                const status = (template.status || '').toUpperCase();
                                                const canEdit =
                                                    LOCALLY_EDITABLE_STATUSES.includes(status);
                                                const canSubmit =
                                                    SUBMITTABLE_STATUSES.includes(status);
                                                return (
                                                    <TableRow key={template.id}>
                                                        <TableCell className="font-medium font-mono">
                                                            {template.name}
                                                        </TableCell>
                                                        <TableCell>{template.language}</TableCell>
                                                        <TableCell>
                                                            <Badge variant="outline">
                                                                {template.category}
                                                            </Badge>
                                                        </TableCell>
                                                        <TableCell>
                                                            <TemplateStatusBadge
                                                                status={status}
                                                                rejectionReason={
                                                                    template.rejection_reason
                                                                }
                                                            />
                                                        </TableCell>
                                                        <TableCell>
                                                            <QualityDot
                                                                quality={template.quality_score}
                                                            />
                                                        </TableCell>
                                                        <TableCell>
                                                            {formatDate(
                                                                template.updated_at ??
                                                                template.created_at,
                                                            )}
                                                        </TableCell>
                                                        <TableCell className="text-right">
                                                            <div className="flex justify-end gap-1">
                                                                {canEdit && (
                                                                    <Button
                                                                        variant="ghost"
                                                                        size="sm"
                                                                        onClick={() =>
                                                                            openEdit(template)
                                                                        }
                                                                        title="Modifier"
                                                                    >
                                                                        <Pencil className="h-4 w-4" />
                                                                    </Button>
                                                                )}
                                                                {canSubmit && (
                                                                    <Button
                                                                        variant="ghost"
                                                                        size="sm"
                                                                        onClick={() =>
                                                                            handleSubmit(template)
                                                                        }
                                                                        disabled={
                                                                            submittingId ===
                                                                            template.id
                                                                        }
                                                                        title="Soumettre à Meta"
                                                                    >
                                                                        <Send className="h-4 w-4" />
                                                                    </Button>
                                                                )}
                                                                <Button
                                                                    variant="ghost"
                                                                    size="sm"
                                                                    onClick={() =>
                                                                        setDeleteTarget(template)
                                                                    }
                                                                    title="Supprimer"
                                                                >
                                                                    <Trash2 className="h-4 w-4 text-destructive" />
                                                                </Button>
                                                            </div>
                                                        </TableCell>
                                                    </TableRow>
                                                );
                                            })}
                                        </TableBody>
                                    </Table>
                                </div>
                            )}
                        </CardContent>
                    </Card>
                )}
            </div>

            <TemplateBuilderDialog
                open={builderOpen}
                onOpenChange={setBuilderOpen}
                configurationId={selectedConfigId}
                existing={editTarget}
                onSaved={fetchTemplates}
            />

            <AlertDialog
                open={!!deleteTarget}
                onOpenChange={(open) => !open && setDeleteTarget(null)}
            >
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>Supprimer le modèle ?</AlertDialogTitle>
                        <AlertDialogDescription>
                            « {deleteTarget?.name} » ({deleteTarget?.language}) sera supprimé
                            localement. S&apos;il a déjà été soumis, sa suppression chez Meta
                            sera également tentée.
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel>Annuler</AlertDialogCancel>
                        <AlertDialogAction onClick={handleConfirmDelete}>
                            Supprimer
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>
        </div>
    );
}
