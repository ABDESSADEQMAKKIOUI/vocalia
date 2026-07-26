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
    type PlanDto,
    provisionOrganization,
    type ProvisionOrganizationPayload,
} from '@/lib/platformAdminApi';

type ProvisionOrganizationDialogProps = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    plans: PlanDto[];
    onCreated: () => void;
};

export function ProvisionOrganizationDialog({
    open,
    onOpenChange,
    plans,
    onCreated,
}: ProvisionOrganizationDialogProps) {
    const [name, setName] = useState('');
    const [contactEmail, setContactEmail] = useState('');
    const [ownerEmail, setOwnerEmail] = useState('');
    const [ownerPassword, setOwnerPassword] = useState('');
    const [planCode, setPlanCode] = useState('');
    const [trialDays, setTrialDays] = useState('');
    const [notes, setNotes] = useState('');
    const [saving, setSaving] = useState(false);

    const activePlans = plans.filter((plan) => plan.is_active);

    useEffect(() => {
        if (!open) return;
        setName('');
        setContactEmail('');
        setOwnerEmail('');
        setOwnerPassword('');
        setPlanCode(plans.find((plan) => plan.is_active)?.code ?? '');
        setTrialDays('');
        setNotes('');
    }, [open, plans]);

    const handleSave = async () => {
        if (!name.trim()) {
            toast.error('Le nom du client est requis');
            return;
        }
        if (!ownerEmail.trim() || !ownerPassword) {
            toast.error("L'email et le mot de passe du propriétaire sont requis");
            return;
        }
        if (!planCode) {
            toast.error('Sélectionnez un plan');
            return;
        }

        const payload: ProvisionOrganizationPayload = {
            name: name.trim(),
            contact_email: contactEmail.trim(),
            owner_email: ownerEmail.trim(),
            owner_password: ownerPassword,
            plan_code: planCode,
        };
        if (trialDays.trim()) {
            payload.trial_days = Number(trialDays);
        }
        if (notes.trim()) {
            payload.notes = notes.trim();
        }

        setSaving(true);
        try {
            await provisionOrganization(payload);
            toast.success('Client créé');
            onOpenChange(false);
            onCreated();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : 'Échec de la création du client',
            );
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
                <DialogHeader>
                    <DialogTitle>Nouveau client</DialogTitle>
                    <DialogDescription>
                        Crée l&apos;organisation, son utilisateur propriétaire et son
                        abonnement en une seule opération.
                    </DialogDescription>
                </DialogHeader>

                <div className="space-y-5">
                    <div className="grid gap-2">
                        <Label htmlFor="provision-name">Nom du client</Label>
                        <Input
                            id="provision-name"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            placeholder="Clinique Saint-Louis"
                        />
                    </div>

                    <div className="grid gap-2">
                        <Label htmlFor="provision-contact-email">Email de contact</Label>
                        <Input
                            id="provision-contact-email"
                            type="email"
                            value={contactEmail}
                            onChange={(e) => setContactEmail(e.target.value)}
                            placeholder="contact@client.fr"
                        />
                    </div>

                    <div className="space-y-3 rounded-md border p-3">
                        <div>
                            <p className="text-sm font-medium">Compte propriétaire</p>
                            <p className="text-xs text-muted-foreground">
                                Identifiants de connexion du premier utilisateur du
                                client.
                            </p>
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="provision-owner-email">
                                Email du propriétaire
                            </Label>
                            <Input
                                id="provision-owner-email"
                                type="email"
                                autoComplete="off"
                                value={ownerEmail}
                                onChange={(e) => setOwnerEmail(e.target.value)}
                                placeholder="admin@client.fr"
                            />
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="provision-owner-password">
                                Mot de passe du propriétaire
                            </Label>
                            <Input
                                id="provision-owner-password"
                                type="password"
                                autoComplete="new-password"
                                value={ownerPassword}
                                onChange={(e) => setOwnerPassword(e.target.value)}
                                placeholder="••••••••"
                            />
                            <p className="text-xs text-muted-foreground">
                                Transmettez-le au client, il pourra le changer ensuite.
                            </p>
                        </div>
                    </div>

                    <div className="grid gap-4 sm:grid-cols-2">
                        <div className="grid gap-2">
                            <Label>Plan</Label>
                            <Select value={planCode} onValueChange={setPlanCode}>
                                <SelectTrigger className="w-full">
                                    <SelectValue placeholder="Choisir un plan" />
                                </SelectTrigger>
                                <SelectContent>
                                    {activePlans.length === 0 ? (
                                        <SelectItem value="none" disabled>
                                            Aucun plan actif
                                        </SelectItem>
                                    ) : (
                                        activePlans.map((plan) => (
                                            <SelectItem key={plan.id} value={plan.code}>
                                                {plan.name} ({plan.code})
                                            </SelectItem>
                                        ))
                                    )}
                                </SelectContent>
                            </Select>
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="provision-trial-days">
                                Jours d&apos;essai
                            </Label>
                            <Input
                                id="provision-trial-days"
                                type="number"
                                min={0}
                                value={trialDays}
                                onChange={(e) => setTrialDays(e.target.value)}
                                placeholder="Valeur du plan"
                            />
                        </div>
                    </div>

                    <div className="grid gap-2">
                        <Label htmlFor="provision-notes">Notes</Label>
                        <Textarea
                            id="provision-notes"
                            value={notes}
                            onChange={(e) => setNotes(e.target.value)}
                            placeholder="Contexte commercial, contact signataire…"
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
                        Créer le client
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
