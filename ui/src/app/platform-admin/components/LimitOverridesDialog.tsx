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
    type EffectiveLimits,
    type PlanDto,
    updateOrganizationLimits,
} from '@/lib/platformAdminApi';

import {
    formatLimit,
    LIMIT_HINTS,
    LIMIT_KEYS,
    LIMIT_LABELS,
    type LimitInputs,
    type LimitKey,
    limitsToInputs,
    parseLimitInput,
} from './format';

type LimitOverridesDialogProps = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    orgId: number;
    /** Overrides already stored on the subscription, if any. */
    overrides?: Partial<EffectiveLimits> | null;
    /** Plan the overrides fall back to, for the placeholders. */
    plan: PlanDto | null;
    onSaved: () => void;
};

export function LimitOverridesDialog({
    open,
    onOpenChange,
    orgId,
    overrides,
    plan,
    onSaved,
}: LimitOverridesDialogProps) {
    const [inputs, setInputs] = useState<LimitInputs>(() => limitsToInputs(null));
    // Keys the operator marked unlimited for this tenant. The backend reads an
    // explicit null as "unlimited" and an absent key as "fall back to the
    // plan", so the two cases need distinct controls.
    const [unlimited, setUnlimited] = useState<Set<LimitKey>>(new Set());
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) return;
        setInputs(limitsToInputs(overrides));
        setUnlimited(
            new Set(
                LIMIT_KEYS.filter(
                    (key) =>
                        overrides !== null &&
                        overrides !== undefined &&
                        key in overrides &&
                        overrides[key] === null,
                ),
            ),
        );
    }, [open, overrides]);

    const toggleUnlimited = (key: LimitKey) => {
        setUnlimited((current) => {
            const next = new Set(current);
            if (next.has(key)) {
                next.delete(key);
            } else {
                next.add(key);
            }
            return next;
        });
    };

    const handleSave = async () => {
        // Three states per key: marked unlimited -> explicit null; a number ->
        // that ceiling; empty -> key omitted, the plan value applies again.
        const payload: Partial<EffectiveLimits> = {};
        for (const key of LIMIT_KEYS) {
            if (unlimited.has(key)) {
                payload[key] = null;
                continue;
            }
            const value = parseLimitInput(inputs[key]);
            if (value !== null) {
                payload[key] = value;
            }
        }

        setSaving(true);
        try {
            await updateOrganizationLimits(orgId, payload);
            toast.success('Limites mises à jour');
            onOpenChange(false);
            onSaved();
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : 'Échec de la mise à jour des limites',
            );
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
                <DialogHeader>
                    <DialogTitle>Modifier les limites</DialogTitle>
                    <DialogDescription>
                        Ces valeurs remplacent celles du plan pour ce client uniquement.
                        Videz un champ pour revenir à la limite du plan, ou cochez
                        « illimité » pour lever le plafond pour ce client.
                    </DialogDescription>
                </DialogHeader>

                <div className="grid gap-4 sm:grid-cols-2">
                    {LIMIT_KEYS.map((key) => (
                        <div key={key} className="grid gap-2">
                            <Label htmlFor={`override-${key}`}>{LIMIT_LABELS[key]}</Label>
                            <Input
                                id={`override-${key}`}
                                type="number"
                                min={0}
                                value={unlimited.has(key) ? '' : inputs[key]}
                                disabled={unlimited.has(key)}
                                onChange={(e) =>
                                    setInputs((current) => ({
                                        ...current,
                                        [key]: e.target.value,
                                    }))
                                }
                                placeholder={
                                    unlimited.has(key)
                                        ? 'illimité'
                                        : plan
                                          ? formatLimit(plan[key])
                                          : 'valeur du plan'
                                }
                            />
                            <label
                                htmlFor={`override-${key}-unlimited`}
                                className="flex items-center gap-2 text-xs text-muted-foreground"
                            >
                                <input
                                    id={`override-${key}-unlimited`}
                                    type="checkbox"
                                    className="h-3.5 w-3.5"
                                    checked={unlimited.has(key)}
                                    onChange={() => toggleUnlimited(key)}
                                />
                                Illimité pour ce client
                            </label>
                            {LIMIT_HINTS[key] && (
                                <p className="text-xs text-muted-foreground">
                                    {LIMIT_HINTS[key]}
                                </p>
                            )}
                        </div>
                    ))}
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
                        Enregistrer les limites
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
