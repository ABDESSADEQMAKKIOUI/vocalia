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
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import {
    assignPlan,
    type AssignPlanPayload,
    type PlanDto,
    type SubscriptionDto,
    type SubscriptionStatus,
} from '@/lib/platformAdminApi';

import { STATUS_LABELS, toDateInput } from './format';

const KEEP_STATUS = 'keep';

const ASSIGNABLE_STATUSES: SubscriptionStatus[] = [
    'trialing',
    'active',
    'past_due',
    'suspended',
    'cancelled',
];

type AssignPlanDialogProps = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    orgId: number;
    plans: PlanDto[];
    subscription: SubscriptionDto | null;
    onSaved: () => void;
};

export function AssignPlanDialog({
    open,
    onOpenChange,
    orgId,
    plans,
    subscription,
    onSaved,
}: AssignPlanDialogProps) {
    const [planCode, setPlanCode] = useState('');
    const [status, setStatus] = useState<string>(KEEP_STATUS);
    const [periodStart, setPeriodStart] = useState('');
    const [periodEnd, setPeriodEnd] = useState('');
    const [notes, setNotes] = useState('');
    const [saving, setSaving] = useState(false);

    const currentPlanCode =
        plans.find((plan) => plan.id === subscription?.plan_id)?.code ?? '';

    useEffect(() => {
        if (!open) return;
        setPlanCode(currentPlanCode);
        setStatus(KEEP_STATUS);
        setPeriodStart(toDateInput(subscription?.current_period_start));
        setPeriodEnd(toDateInput(subscription?.current_period_end));
        setNotes(subscription?.notes ?? '');
    }, [open, currentPlanCode, subscription]);

    const handleSave = async () => {
        if (!planCode) {
            toast.error('Sélectionnez un plan');
            return;
        }

        const payload: AssignPlanPayload = { plan_code: planCode };
        if (status !== KEEP_STATUS) {
            payload.status = status as SubscriptionStatus;
        }
        if (periodStart) {
            payload.current_period_start = new Date(periodStart).toISOString();
        }
        if (periodEnd) {
            payload.current_period_end = new Date(periodEnd).toISOString();
        }
        if (notes.trim()) {
            payload.notes = notes.trim();
        }

        setSaving(true);
        try {
            await assignPlan(orgId, payload);
            toast.success('Abonnement mis à jour');
            onOpenChange(false);
            onSaved();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec du changement de plan',
            );
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
                <DialogHeader>
                    <DialogTitle>Changer de plan</DialogTitle>
                    <DialogDescription>
                        Le nouveau plan s&apos;applique immédiatement. Laissez les dates
                        inchangées pour conserver la période en cours.
                    </DialogDescription>
                </DialogHeader>

                <div className="space-y-5">
                    <div className="grid gap-2">
                        <Label>Plan</Label>
                        <Select value={planCode} onValueChange={setPlanCode}>
                            <SelectTrigger className="w-full">
                                <SelectValue placeholder="Choisir un plan" />
                            </SelectTrigger>
                            <SelectContent>
                                {plans.length === 0 ? (
                                    <SelectItem value="none" disabled>
                                        Aucun plan disponible
                                    </SelectItem>
                                ) : (
                                    plans.map((plan) => (
                                        <SelectItem key={plan.id} value={plan.code}>
                                            {plan.name} ({plan.code})
                                            {plan.is_active ? '' : ' — inactif'}
                                        </SelectItem>
                                    ))
                                )}
                            </SelectContent>
                        </Select>
                    </div>

                    <div className="grid gap-2">
                        <Label>Statut</Label>
                        <Select value={status} onValueChange={setStatus}>
                            <SelectTrigger className="w-full">
                                <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                                <SelectItem value={KEEP_STATUS}>
                                    Conserver le statut actuel
                                </SelectItem>
                                {ASSIGNABLE_STATUSES.map((value) => (
                                    <SelectItem key={value} value={value}>
                                        {STATUS_LABELS[value]}
                                    </SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                    </div>

                    <div className="grid gap-4 sm:grid-cols-2">
                        <div className="grid gap-2">
                            <Label htmlFor="assign-period-start">Début de période</Label>
                            <Input
                                id="assign-period-start"
                                type="date"
                                value={periodStart}
                                onChange={(e) => setPeriodStart(e.target.value)}
                            />
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="assign-period-end">Fin de période</Label>
                            <Input
                                id="assign-period-end"
                                type="date"
                                value={periodEnd}
                                onChange={(e) => setPeriodEnd(e.target.value)}
                            />
                        </div>
                    </div>

                    <div className="grid gap-2">
                        <Label htmlFor="assign-notes">Notes</Label>
                        <Textarea
                            id="assign-notes"
                            value={notes}
                            onChange={(e) => setNotes(e.target.value)}
                            placeholder="Motif du changement, référence du contrat…"
                        />
                    </div>
                </div>

                <DialogFooter>
                    <Button
                        variant="outline"
                        onClick={() => onOpenChange(false)}
                        disabled={saving}
                    >
                        Annuler
                    </Button>
                    <Button onClick={handleSave} disabled={saving}>
                        {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                        Appliquer le plan
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
