"use client";

import { Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { cancelOrganization } from '@/lib/platformAdminApi';

import { formatDate } from './format';

type CancelSubscriptionDialogProps = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    orgId: number;
    currentPeriodEnd?: string | null;
    onSaved: () => void;
};

export function CancelSubscriptionDialog({
    open,
    onOpenChange,
    orgId,
    currentPeriodEnd,
    onSaved,
}: CancelSubscriptionDialogProps) {
    const [atPeriodEnd, setAtPeriodEnd] = useState(true);
    const [reason, setReason] = useState('');
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) return;
        setAtPeriodEnd(true);
        setReason('');
    }, [open]);

    const handleCancel = async () => {
        setSaving(true);
        try {
            await cancelOrganization(orgId, {
                at_period_end: atPeriodEnd,
                ...(reason.trim() ? { reason: reason.trim() } : {}),
            });
            toast.success(
                atPeriodEnd
                    ? 'Annulation programmée en fin de période'
                    : 'Abonnement annulé',
            );
            onOpenChange(false);
            onSaved();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : "Échec de l'annulation",
            );
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="sm:max-w-lg">
                <DialogHeader>
                    <DialogTitle>Annuler l&apos;abonnement</DialogTitle>
                    <DialogDescription>
                        Un abonnement annulé bloque la production vocale et les envois
                        WhatsApp du client.
                    </DialogDescription>
                </DialogHeader>

                <div className="space-y-4">
                    <div className="flex items-center justify-between rounded-md border p-3">
                        <div className="space-y-1">
                            <Label htmlFor="cancel-at-period-end">
                                Annuler en fin de période
                            </Label>
                            <p className="text-xs text-muted-foreground">
                                Le client garde son accès jusqu&apos;au{' '}
                                {formatDate(currentPeriodEnd)}. Désactivez pour annuler
                                immédiatement.
                            </p>
                        </div>
                        <Switch
                            id="cancel-at-period-end"
                            checked={atPeriodEnd}
                            onCheckedChange={setAtPeriodEnd}
                        />
                    </div>

                    <div className="grid gap-2">
                        <Label htmlFor="cancel-reason">Motif</Label>
                        <Textarea
                            id="cancel-reason"
                            value={reason}
                            onChange={(e) => setReason(e.target.value)}
                            placeholder="Résiliation demandée par le client…"
                        />
                    </div>
                </div>

                <DialogFooter>
                    <Button
                        variant="outline"
                        onClick={() => onOpenChange(false)}
                        disabled={saving}
                    >
                        Fermer
                    </Button>
                    <Button
                        variant="destructive"
                        onClick={handleCancel}
                        disabled={saving}
                    >
                        {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                        Annuler l&apos;abonnement
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
