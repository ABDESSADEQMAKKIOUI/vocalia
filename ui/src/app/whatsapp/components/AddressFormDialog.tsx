"use client";

import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import type { WorkflowSummaryResponse } from '@/client/types.gen';
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
import { Switch } from '@/components/ui/switch';
import {
    createMessagingAddress,
    type MessagingAddress,
    type MessagingAddressUpdatePayload,
    updateMessagingAddress,
} from '@/lib/messagingApi';

const NO_WORKFLOW = 'none';

type AddressFormDialogProps = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    /** Configuration receiving the new number (create mode). */
    configId: number | null;
    /** null → add a new number; otherwise edit this one. */
    existing: MessagingAddress | null;
    workflows: WorkflowSummaryResponse[];
    isLoadingWorkflows: boolean;
    onSaved: () => void;
};

export function AddressFormDialog({
    open,
    onOpenChange,
    configId,
    existing,
    workflows,
    isLoadingWorkflows,
    onSaved,
}: AddressFormDialogProps) {
    const [address, setAddress] = useState('');
    const [externalId, setExternalId] = useState('');
    const [workflowId, setWorkflowId] = useState<string>(NO_WORKFLOW);
    const [isActive, setIsActive] = useState(true);
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) return;
        if (existing) {
            setAddress(existing.address);
            setExternalId(existing.external_id);
            setWorkflowId(
                existing.inbound_workflow_id != null
                    ? String(existing.inbound_workflow_id)
                    : NO_WORKFLOW,
            );
            setIsActive(existing.is_active);
        } else {
            setAddress('');
            setExternalId('');
            setWorkflowId(NO_WORKFLOW);
            setIsActive(true);
        }
    }, [open, existing]);

    const handleSave = async () => {
        setSaving(true);
        try {
            if (existing) {
                const body: MessagingAddressUpdatePayload = { is_active: isActive };
                if (workflowId === NO_WORKFLOW) {
                    if (existing.inbound_workflow_id != null) {
                        body.clear_inbound_workflow = true;
                    }
                } else {
                    body.inbound_workflow_id = Number(workflowId);
                }
                await updateMessagingAddress(existing.id, body);
                toast.success('Numéro mis à jour');
            } else {
                if (configId === null) return;
                if (!address.trim() || !externalId.trim()) {
                    toast.error(
                        'Le numéro affiché et le phone_number_id sont requis',
                    );
                    return;
                }
                await createMessagingAddress(configId, {
                    address: address.trim(),
                    external_id: externalId.trim(),
                    inbound_workflow_id:
                        workflowId !== NO_WORKFLOW ? Number(workflowId) : null,
                });
                toast.success('Numéro ajouté');
            }
            onOpenChange(false);
            onSaved();
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : "Échec de l'enregistrement du numéro",
            );
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="sm:max-w-lg">
                <DialogHeader>
                    <DialogTitle>
                        {existing ? 'Modifier le numéro' : 'Ajouter un numéro'}
                    </DialogTitle>
                    <DialogDescription>
                        {existing
                            ? 'Modifiez le workflow entrant ou le statut du numéro.'
                            : 'Associez un numéro WhatsApp Business de ce compte Meta.'}
                    </DialogDescription>
                </DialogHeader>

                <div className="space-y-4">
                    <div className="grid gap-2">
                        <Label htmlFor="address-display">Numéro affiché</Label>
                        <Input
                            id="address-display"
                            value={address}
                            onChange={(e) => setAddress(e.target.value)}
                            placeholder="+33612345678"
                            disabled={!!existing}
                        />
                    </div>
                    <div className="grid gap-2">
                        <Label htmlFor="address-external-id">Phone number ID</Label>
                        <Input
                            id="address-external-id"
                            value={externalId}
                            onChange={(e) => setExternalId(e.target.value)}
                            placeholder="106540352242922"
                            disabled={!!existing}
                        />
                        <p className="text-xs text-muted-foreground">
                            {existing
                                ? 'Le numéro et le phone_number_id ne sont pas modifiables.'
                                : 'Identifiant du numéro fourni par Meta (phone_number_id).'}
                        </p>
                    </div>
                    <div className="grid gap-2">
                        <Label>Workflow entrant</Label>
                        <Select value={workflowId} onValueChange={setWorkflowId}>
                            <SelectTrigger className="w-full">
                                <SelectValue placeholder="Aucun" />
                            </SelectTrigger>
                            <SelectContent>
                                <SelectItem value={NO_WORKFLOW}>Aucun</SelectItem>
                                {isLoadingWorkflows ? (
                                    <SelectItem value="loading" disabled>
                                        Chargement des workflows…
                                    </SelectItem>
                                ) : (
                                    workflows.map((workflow) => (
                                        <SelectItem
                                            key={workflow.id}
                                            value={String(workflow.id)}
                                        >
                                            {workflow.name} (#{workflow.id})
                                        </SelectItem>
                                    ))
                                )}
                            </SelectContent>
                        </Select>
                        <p className="text-xs text-muted-foreground">
                            Workflow exécuté à la réception d&apos;un message entrant sur
                            ce numéro.
                        </p>
                    </div>
                    {existing && (
                        <div className="flex items-center justify-between rounded-md border p-3">
                            <div className="space-y-1">
                                <Label htmlFor="address-active">Numéro actif</Label>
                                <p className="text-xs text-muted-foreground">
                                    Un numéro inactif ne peut plus envoyer ni recevoir de
                                    messages.
                                </p>
                            </div>
                            <Switch
                                id="address-active"
                                checked={isActive}
                                onCheckedChange={setIsActive}
                            />
                        </div>
                    )}
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
                        {saving
                            ? 'Enregistrement…'
                            : existing
                                ? 'Enregistrer'
                                : 'Ajouter le numéro'}
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
