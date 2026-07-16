"use client";

import { Plus, ShieldBan, Trash2 } from 'lucide-react';
import { useState } from 'react';
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
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from '@/components/ui/select';
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from '@/components/ui/table';
import {
    addSuppression,
    deleteSuppression,
    type MessagingSuppression,
    type MessagingSuppressionCreatePayload,
} from '@/lib/messagingApi';

const SCOPE_LABELS: Record<string, string> = {
    marketing: 'Marketing',
    all: 'Tous les messages',
};

const REASON_LABELS: Record<string, string> = {
    manual: 'Manuel',
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

type SuppressionsCardProps = {
    suppressions: MessagingSuppression[];
    loading: boolean;
    onChanged: () => void;
};

export function SuppressionsCard({
    suppressions,
    loading,
    onChanged,
}: SuppressionsCardProps) {
    const [addOpen, setAddOpen] = useState(false);
    const [address, setAddress] = useState('');
    const [scope, setScope] = useState<'marketing' | 'all'>('all');
    const [expiresAt, setExpiresAt] = useState('');
    const [saving, setSaving] = useState(false);
    const [deleteTarget, setDeleteTarget] = useState<MessagingSuppression | null>(
        null,
    );

    const openAdd = () => {
        setAddress('');
        setScope('all');
        setExpiresAt('');
        setAddOpen(true);
    };

    const handleAdd = async () => {
        if (!address.trim()) {
            toast.error("L'adresse est requise");
            return;
        }
        setSaving(true);
        try {
            const payload: MessagingSuppressionCreatePayload = {
                address: address.trim(),
                scope,
                reason: 'manual',
            };
            if (expiresAt) {
                payload.expires_at = new Date(expiresAt).toISOString();
            }
            await addSuppression(payload);
            toast.success("Adresse ajoutée à la liste d'exclusion");
            setAddOpen(false);
            onChanged();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : "Échec de l'ajout de l'exclusion",
            );
        } finally {
            setSaving(false);
        }
    };

    const handleConfirmDelete = async () => {
        if (!deleteTarget) return;
        try {
            await deleteSuppression(deleteTarget.id);
            toast.success('Exclusion supprimée');
            setDeleteTarget(null);
            onChanged();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec de la suppression',
            );
        }
    };

    return (
        <Card>
            <CardHeader>
                <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="space-y-1.5">
                        <CardTitle className="flex items-center gap-2">
                            <ShieldBan className="h-5 w-5" />
                            Liste d&apos;exclusion
                        </CardTitle>
                        <CardDescription>
                            Les numéros de cette liste ne reçoivent plus de messages
                            WhatsApp, selon la portée choisie.
                        </CardDescription>
                    </div>
                    <Button variant="outline" size="sm" onClick={openAdd}>
                        <Plus className="h-4 w-4 mr-2" />
                        Ajouter une adresse
                    </Button>
                </div>
            </CardHeader>
            <CardContent>
                {loading ? (
                    <div className="animate-pulse space-y-3">
                        {[...Array(3)].map((_, i) => (
                            <div key={i} className="h-10 bg-muted rounded"></div>
                        ))}
                    </div>
                ) : suppressions.length === 0 ? (
                    <p className="text-sm text-muted-foreground">
                        Aucune adresse exclue pour le moment.
                    </p>
                ) : (
                    <div className="overflow-x-auto">
                        <Table>
                            <TableHeader>
                                <TableRow>
                                    <TableHead>Adresse</TableHead>
                                    <TableHead>Portée</TableHead>
                                    <TableHead>Motif</TableHead>
                                    <TableHead>Expire le</TableHead>
                                    <TableHead>Créé le</TableHead>
                                    <TableHead className="text-right">Actions</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {suppressions.map((suppression) => (
                                    <TableRow key={suppression.id}>
                                        <TableCell className="font-medium font-mono">
                                            {suppression.address}
                                        </TableCell>
                                        <TableCell>
                                            <Badge variant="outline">
                                                {SCOPE_LABELS[suppression.scope] ??
                                                    suppression.scope}
                                            </Badge>
                                        </TableCell>
                                        <TableCell>
                                            {REASON_LABELS[suppression.reason] ??
                                                suppression.reason}
                                        </TableCell>
                                        <TableCell>
                                            {suppression.expires_at
                                                ? formatDate(suppression.expires_at)
                                                : 'Jamais'}
                                        </TableCell>
                                        <TableCell>
                                            {formatDate(suppression.created_at)}
                                        </TableCell>
                                        <TableCell className="text-right">
                                            <Button
                                                variant="ghost"
                                                size="sm"
                                                onClick={() => setDeleteTarget(suppression)}
                                                title="Supprimer"
                                            >
                                                <Trash2 className="h-4 w-4 text-destructive" />
                                            </Button>
                                        </TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </div>
                )}
            </CardContent>

            <Dialog open={addOpen} onOpenChange={setAddOpen}>
                <DialogContent className="sm:max-w-lg">
                    <DialogHeader>
                        <DialogTitle>Ajouter une exclusion</DialogTitle>
                        <DialogDescription>
                            Cette adresse ne recevra plus de messages WhatsApp selon la
                            portée choisie.
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-4">
                        <div className="grid gap-2">
                            <Label htmlFor="suppression-address">Adresse</Label>
                            <Input
                                id="suppression-address"
                                value={address}
                                onChange={(e) => setAddress(e.target.value)}
                                placeholder="+33612345678"
                            />
                            <p className="text-xs text-muted-foreground">
                                Numéro au format international.
                            </p>
                        </div>
                        <div className="grid gap-2">
                            <Label>Portée</Label>
                            <Select
                                value={scope}
                                onValueChange={(value) =>
                                    setScope(value as 'marketing' | 'all')
                                }
                            >
                                <SelectTrigger className="w-full">
                                    <SelectValue placeholder="Portée" />
                                </SelectTrigger>
                                <SelectContent>
                                    <SelectItem value="all">Tous les messages</SelectItem>
                                    <SelectItem value="marketing">
                                        Marketing uniquement
                                    </SelectItem>
                                </SelectContent>
                            </Select>
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="suppression-reason">Motif</Label>
                            <Input id="suppression-reason" value="manual" disabled />
                            <p className="text-xs text-muted-foreground">
                                Les ajouts effectués depuis cette page sont enregistrés
                                avec le motif « manual ».
                            </p>
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="suppression-expires">
                                Expiration (optionnel)
                            </Label>
                            <Input
                                id="suppression-expires"
                                type="datetime-local"
                                value={expiresAt}
                                onChange={(e) => setExpiresAt(e.target.value)}
                            />
                            <p className="text-xs text-muted-foreground">
                                Laissez vide pour une exclusion permanente.
                            </p>
                        </div>
                    </div>
                    <DialogFooter>
                        <Button
                            variant="outline"
                            onClick={() => setAddOpen(false)}
                            disabled={saving}
                        >
                            Annuler
                        </Button>
                        <Button onClick={handleAdd} disabled={saving}>
                            {saving ? 'Enregistrement…' : 'Ajouter'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <AlertDialog
                open={!!deleteTarget}
                onOpenChange={(open) => !open && setDeleteTarget(null)}
            >
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>Supprimer l&apos;exclusion ?</AlertDialogTitle>
                        <AlertDialogDescription>
                            {deleteTarget?.address} pourra de nouveau recevoir des
                            messages WhatsApp.
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
        </Card>
    );
}
