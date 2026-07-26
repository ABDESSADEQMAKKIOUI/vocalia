"use client";

import { Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
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
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
    type BillingInterval,
    createPlan,
    type PlanDto,
    type PlanFeatureKey,
    type PlanFeatures,
    type PlanWritePayload,
    updatePlan,
} from '@/lib/platformAdminApi';

import {
    FEATURE_KEYS,
    FEATURE_LABELS,
    inputsToLimits,
    LIMIT_KEYS,
    LIMIT_LABELS,
    type LimitInputs,
    limitsToInputs,
} from './format';

type PlanFormDialogProps = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    /** null → create a new plan; otherwise edit this one. */
    existing: PlanDto | null;
    onSaved: () => void;
};

export function PlanFormDialog({
    open,
    onOpenChange,
    existing,
    onSaved,
}: PlanFormDialogProps) {
    const [code, setCode] = useState('');
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [priceAmount, setPriceAmount] = useState('0');
    const [currency, setCurrency] = useState('EUR');
    const [billingInterval, setBillingInterval] = useState<BillingInterval>('monthly');
    const [trialDays, setTrialDays] = useState('0');
    const [limits, setLimits] = useState<LimitInputs>(() => limitsToInputs(null));
    const [features, setFeatures] = useState<PlanFeatures>({});
    const [isActive, setIsActive] = useState(true);
    const [isPublic, setIsPublic] = useState(true);
    const [sortOrder, setSortOrder] = useState('0');
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) return;
        if (existing) {
            setCode(existing.code);
            setName(existing.name);
            setDescription(existing.description ?? '');
            setPriceAmount(String(existing.price_amount));
            setCurrency(existing.currency);
            setBillingInterval(existing.billing_interval);
            setTrialDays(String(existing.trial_days));
            setLimits(limitsToInputs(existing));
            setFeatures(existing.features ?? {});
            setIsActive(existing.is_active);
            setIsPublic(existing.is_public);
            setSortOrder(String(existing.sort_order));
        } else {
            setCode('');
            setName('');
            setDescription('');
            setPriceAmount('0');
            setCurrency('EUR');
            setBillingInterval('monthly');
            setTrialDays('0');
            setLimits(limitsToInputs(null));
            setFeatures({ voice: true });
            setIsActive(true);
            setIsPublic(true);
            setSortOrder('0');
        }
    }, [open, existing]);

    const toggleFeature = (key: PlanFeatureKey, checked: boolean) => {
        setFeatures((current) => ({ ...current, [key]: checked }));
    };

    const handleSave = async () => {
        if (!code.trim() || !name.trim()) {
            toast.error('Le code et le nom du plan sont requis');
            return;
        }

        const payload: PlanWritePayload = {
            code: code.trim(),
            name: name.trim(),
            description: description.trim() || null,
            price_amount: Number(priceAmount) || 0,
            currency: currency.trim().toUpperCase() || 'EUR',
            billing_interval: billingInterval,
            trial_days: Number(trialDays) || 0,
            ...inputsToLimits(limits),
            features,
            is_active: isActive,
            is_public: isPublic,
            sort_order: Number(sortOrder) || 0,
        };

        setSaving(true);
        try {
            if (existing) {
                await updatePlan(existing.id, payload);
                toast.success('Plan mis à jour');
            } else {
                await createPlan(payload);
                toast.success('Plan créé');
            }
            onOpenChange(false);
            onSaved();
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : "Échec de l'enregistrement du plan",
            );
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
                <DialogHeader>
                    <DialogTitle>
                        {existing ? 'Modifier le plan' : 'Nouveau plan'}
                    </DialogTitle>
                    <DialogDescription>
                        Une limite laissée vide signifie illimité.
                    </DialogDescription>
                </DialogHeader>

                <div className="space-y-5">
                    <div className="grid gap-4 sm:grid-cols-2">
                        <div className="grid gap-2">
                            <Label htmlFor="plan-code">Code</Label>
                            <Input
                                id="plan-code"
                                value={code}
                                onChange={(e) => setCode(e.target.value)}
                                placeholder="starter"
                                disabled={existing !== null}
                            />
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="plan-name">Nom</Label>
                            <Input
                                id="plan-name"
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                placeholder="Starter"
                            />
                        </div>
                    </div>

                    <div className="grid gap-2">
                        <Label htmlFor="plan-description">Description</Label>
                        <Textarea
                            id="plan-description"
                            value={description}
                            onChange={(e) => setDescription(e.target.value)}
                            placeholder="Pour les petites équipes qui démarrent."
                        />
                    </div>

                    <div className="grid gap-4 sm:grid-cols-4">
                        <div className="grid gap-2">
                            <Label htmlFor="plan-price">Prix</Label>
                            <Input
                                id="plan-price"
                                type="number"
                                min={0}
                                step="0.01"
                                value={priceAmount}
                                onChange={(e) => setPriceAmount(e.target.value)}
                            />
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="plan-currency">Devise</Label>
                            <Input
                                id="plan-currency"
                                maxLength={3}
                                value={currency}
                                onChange={(e) => setCurrency(e.target.value)}
                                placeholder="EUR"
                            />
                        </div>
                        <div className="grid gap-2">
                            <Label>Facturation</Label>
                            <Select
                                value={billingInterval}
                                onValueChange={(value) =>
                                    setBillingInterval(value as BillingInterval)
                                }
                            >
                                <SelectTrigger className="w-full">
                                    <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                    <SelectItem value="monthly">Mensuelle</SelectItem>
                                    <SelectItem value="yearly">Annuelle</SelectItem>
                                </SelectContent>
                            </Select>
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="plan-trial-days">Jours d&apos;essai</Label>
                            <Input
                                id="plan-trial-days"
                                type="number"
                                min={0}
                                value={trialDays}
                                onChange={(e) => setTrialDays(e.target.value)}
                            />
                        </div>
                    </div>

                    <div className="space-y-3 rounded-md border p-3">
                        <div>
                            <p className="text-sm font-medium">Limites</p>
                            <p className="text-xs text-muted-foreground">
                                Laissez un champ vide pour ne pas limiter cette
                                ressource.
                            </p>
                        </div>
                        <div className="grid gap-4 sm:grid-cols-3">
                            {LIMIT_KEYS.map((key) => (
                                <div key={key} className="grid gap-2">
                                    <Label htmlFor={`plan-${key}`}>
                                        {LIMIT_LABELS[key]}
                                    </Label>
                                    <Input
                                        id={`plan-${key}`}
                                        type="number"
                                        min={0}
                                        value={limits[key]}
                                        onChange={(e) =>
                                            setLimits((current) => ({
                                                ...current,
                                                [key]: e.target.value,
                                            }))
                                        }
                                        placeholder="illimité"
                                    />
                                </div>
                            ))}
                        </div>
                    </div>

                    <div className="space-y-3 rounded-md border p-3">
                        <p className="text-sm font-medium">Fonctionnalités incluses</p>
                        <div className="grid gap-3 sm:grid-cols-3">
                            {FEATURE_KEYS.map((key) => (
                                <div key={key} className="flex items-center gap-2">
                                    <Checkbox
                                        id={`plan-feature-${key}`}
                                        checked={features[key] === true}
                                        onCheckedChange={(checked) =>
                                            toggleFeature(key, checked === true)
                                        }
                                    />
                                    <Label
                                        htmlFor={`plan-feature-${key}`}
                                        className="font-normal"
                                    >
                                        {FEATURE_LABELS[key]}
                                    </Label>
                                </div>
                            ))}
                        </div>
                    </div>

                    <div className="grid gap-4 sm:grid-cols-3">
                        <div className="flex items-center justify-between rounded-md border p-3">
                            <Label htmlFor="plan-active">Plan actif</Label>
                            <Switch
                                id="plan-active"
                                checked={isActive}
                                onCheckedChange={setIsActive}
                            />
                        </div>
                        <div className="flex items-center justify-between rounded-md border p-3">
                            <Label htmlFor="plan-public">Visible publiquement</Label>
                            <Switch
                                id="plan-public"
                                checked={isPublic}
                                onCheckedChange={setIsPublic}
                            />
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="plan-sort-order">Ordre d&apos;affichage</Label>
                            <Input
                                id="plan-sort-order"
                                type="number"
                                value={sortOrder}
                                onChange={(e) => setSortOrder(e.target.value)}
                            />
                        </div>
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
                        {existing ? 'Enregistrer' : 'Créer le plan'}
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
