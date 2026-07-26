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
import { Textarea } from '@/components/ui/textarea';
import { suspendOrganization } from '@/lib/platformAdminApi';

type SuspendOrganizationDialogProps = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    orgId: number;
    onSaved: () => void;
};

export function SuspendOrganizationDialog({
    open,
    onOpenChange,
    orgId,
    onSaved,
}: SuspendOrganizationDialogProps) {
    const [reason, setReason] = useState('');
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) return;
        setReason('');
    }, [open]);

    const handleSuspend = async () => {
        setSaving(true);
        try {
            await suspendOrganization(orgId, reason.trim() || undefined);
            toast.success('Abonnement suspendu');
            onOpenChange(false);
            onSaved();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec de la suspension',
            );
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="sm:max-w-lg">
                <DialogHeader>
                    <DialogTitle>Suspendre l&apos;abonnement</DialogTitle>
                    <DialogDescription>
                        Le client ne pourra plus lancer d&apos;appels ni envoyer de
                        messages tant que l&apos;abonnement reste suspendu.
                    </DialogDescription>
                </DialogHeader>

                <div className="grid gap-2">
                    <Label htmlFor="suspend-reason">Motif</Label>
                    <Textarea
                        id="suspend-reason"
                        value={reason}
                        onChange={(e) => setReason(e.target.value)}
                        placeholder="Facture impayée depuis 30 jours…"
                    />
                </div>

                <DialogFooter>
                    <Button
                        variant="outline"
                        onClick={() => onOpenChange(false)}
                        disabled={saving}
                    >
                        Annuler
                    </Button>
                    <Button
                        variant="destructive"
                        onClick={handleSuspend}
                        disabled={saving}
                    >
                        {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                        Suspendre
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
