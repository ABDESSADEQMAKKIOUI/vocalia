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
    createMessagingConfiguration,
    type MessagingConfiguration,
    type MessagingConfigurationCreatePayload,
    type MessagingCredentialsPayload,
    updateMessagingConfiguration,
} from '@/lib/messagingApi';

const NO_WORKFLOW = 'none';

type ConfigFormDialogProps = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    /** null → create a new configuration; otherwise edit this one. */
    existing: MessagingConfiguration | null;
    workflows: WorkflowSummaryResponse[];
    isLoadingWorkflows: boolean;
    onSaved: () => void;
};

export function ConfigFormDialog({
    open,
    onOpenChange,
    existing,
    workflows,
    isLoadingWorkflows,
    onSaved,
}: ConfigFormDialogProps) {
    const [name, setName] = useState('');
    const [isActive, setIsActive] = useState(true);
    const [accessToken, setAccessToken] = useState('');
    const [wabaId, setWabaId] = useState('');
    const [verifyToken, setVerifyToken] = useState('');
    const [appSecret, setAppSecret] = useState('');
    // First phone number (create mode only)
    const [firstAddress, setFirstAddress] = useState('');
    const [firstExternalId, setFirstExternalId] = useState('');
    const [firstWorkflowId, setFirstWorkflowId] = useState<string>(NO_WORKFLOW);
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        if (!open) return;
        if (existing) {
            setName(existing.name);
            setIsActive(existing.is_active);
            setAccessToken(existing.credentials?.access_token ?? '');
            setWabaId(existing.credentials?.waba_id ?? '');
            setVerifyToken(existing.credentials?.verify_token ?? '');
            setAppSecret(existing.credentials?.app_secret ?? '');
        } else {
            setName('');
            setIsActive(true);
            setAccessToken('');
            setWabaId('');
            setVerifyToken('');
            setAppSecret('');
        }
        setFirstAddress('');
        setFirstExternalId('');
        setFirstWorkflowId(NO_WORKFLOW);
    }, [open, existing]);

    const handleSave = async () => {
        if (!name.trim()) {
            toast.error('Le nom est requis');
            return;
        }
        if (!existing) {
            if (!accessToken.trim() || !wabaId.trim()) {
                toast.error("L'access token et le WABA ID sont requis");
                return;
            }
            const hasAddressInput =
                firstAddress.trim() !== '' || firstExternalId.trim() !== '';
            if (hasAddressInput && (!firstAddress.trim() || !firstExternalId.trim())) {
                toast.error(
                    'Renseignez le numéro affiché et le phone_number_id, ou laissez les deux champs vides',
                );
                return;
            }
        }
        setSaving(true);
        try {
            if (existing) {
                // Only send non-empty credential fields: a masked value sent
                // back (or an omitted field) keeps the stored secret.
                const credentials: MessagingCredentialsPayload = {};
                if (accessToken.trim()) credentials.access_token = accessToken.trim();
                if (wabaId.trim()) credentials.waba_id = wabaId.trim();
                if (verifyToken.trim()) credentials.verify_token = verifyToken.trim();
                if (appSecret.trim()) credentials.app_secret = appSecret.trim();
                await updateMessagingConfiguration(existing.id, {
                    name: name.trim(),
                    is_active: isActive,
                    ...(Object.keys(credentials).length > 0 ? { credentials } : {}),
                });
                toast.success('Configuration mise à jour');
            } else {
                const credentials: MessagingCredentialsPayload = {
                    access_token: accessToken.trim(),
                    waba_id: wabaId.trim(),
                };
                if (verifyToken.trim()) credentials.verify_token = verifyToken.trim();
                if (appSecret.trim()) credentials.app_secret = appSecret.trim();
                const payload: MessagingConfigurationCreatePayload = {
                    name: name.trim(),
                    credentials,
                };
                if (firstAddress.trim()) {
                    payload.addresses = [
                        {
                            address: firstAddress.trim(),
                            external_id: firstExternalId.trim(),
                            inbound_workflow_id:
                                firstWorkflowId !== NO_WORKFLOW
                                    ? Number(firstWorkflowId)
                                    : null,
                        },
                    ];
                }
                await createMessagingConfiguration(payload);
                toast.success('Configuration créée');
            }
            onOpenChange(false);
            onSaved();
        } catch (err) {
            toast.error(
                err instanceof Error
                    ? err.message
                    : "Échec de l'enregistrement de la configuration",
            );
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
                <DialogHeader>
                    <DialogTitle>
                        {existing
                            ? 'Modifier la configuration'
                            : 'Nouvelle configuration WhatsApp'}
                    </DialogTitle>
                    <DialogDescription>
                        {existing
                            ? 'Mettez à jour le nom, le statut ou les identifiants Meta.'
                            : 'Connectez un compte WhatsApp Business (Meta) à Dograh.'}
                    </DialogDescription>
                </DialogHeader>

                <div className="space-y-5">
                    <div className="grid gap-2">
                        <Label htmlFor="config-name">Nom</Label>
                        <Input
                            id="config-name"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            placeholder="Compte WhatsApp principal"
                        />
                    </div>

                    {existing && (
                        <div className="flex items-center justify-between rounded-md border p-3">
                            <div className="space-y-1">
                                <Label htmlFor="config-active">Configuration active</Label>
                                <p className="text-xs text-muted-foreground">
                                    Une configuration inactive ne peut plus envoyer ni
                                    recevoir de messages.
                                </p>
                            </div>
                            <Switch
                                id="config-active"
                                checked={isActive}
                                onCheckedChange={setIsActive}
                            />
                        </div>
                    )}

                    <div className="space-y-3 rounded-md border p-3">
                        <div>
                            <p className="text-sm font-medium">Identifiants Meta</p>
                            {existing && (
                                <p className="text-xs text-muted-foreground">
                                    Les valeurs masquées correspondent aux secrets déjà
                                    enregistrés : laissez-les inchangées (ou videz le champ)
                                    pour conserver le secret actuel.
                                </p>
                            )}
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="config-access-token">Access token</Label>
                            <Input
                                id="config-access-token"
                                type="password"
                                autoComplete="off"
                                value={accessToken}
                                onChange={(e) => setAccessToken(e.target.value)}
                                placeholder="EAAG…"
                            />
                            <p className="text-xs text-muted-foreground">
                                Jeton permanent du System User Meta.
                            </p>
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="config-waba-id">WABA ID</Label>
                            <Input
                                id="config-waba-id"
                                value={wabaId}
                                onChange={(e) => setWabaId(e.target.value)}
                                placeholder="102290129340398"
                            />
                            <p className="text-xs text-muted-foreground">
                                Identifiant du compte WhatsApp Business.
                            </p>
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="config-verify-token">
                                Verify token (optionnel)
                            </Label>
                            <Input
                                id="config-verify-token"
                                autoComplete="off"
                                value={verifyToken}
                                onChange={(e) => setVerifyToken(e.target.value)}
                                placeholder="mon-verify-token"
                            />
                        </div>
                        <div className="grid gap-2">
                            <Label htmlFor="config-app-secret">
                                App secret (optionnel)
                            </Label>
                            <Input
                                id="config-app-secret"
                                type="password"
                                autoComplete="off"
                                value={appSecret}
                                onChange={(e) => setAppSecret(e.target.value)}
                                placeholder="••••••••"
                            />
                        </div>
                    </div>

                    {!existing && (
                        <div className="space-y-3 rounded-md border p-3">
                            <div>
                                <p className="text-sm font-medium">
                                    Premier numéro (optionnel)
                                </p>
                                <p className="text-xs text-muted-foreground">
                                    Vous pourrez ajouter d&apos;autres numéros après la
                                    création.
                                </p>
                            </div>
                            <div className="grid gap-2">
                                <Label htmlFor="config-first-address">Numéro affiché</Label>
                                <Input
                                    id="config-first-address"
                                    value={firstAddress}
                                    onChange={(e) => setFirstAddress(e.target.value)}
                                    placeholder="+33612345678"
                                />
                            </div>
                            <div className="grid gap-2">
                                <Label htmlFor="config-first-external-id">
                                    Phone number ID
                                </Label>
                                <Input
                                    id="config-first-external-id"
                                    value={firstExternalId}
                                    onChange={(e) => setFirstExternalId(e.target.value)}
                                    placeholder="106540352242922"
                                />
                                <p className="text-xs text-muted-foreground">
                                    Identifiant du numéro fourni par Meta (phone_number_id).
                                </p>
                            </div>
                            <div className="grid gap-2">
                                <Label>Workflow entrant (optionnel)</Label>
                                <Select
                                    value={firstWorkflowId}
                                    onValueChange={setFirstWorkflowId}
                                >
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
                                    Workflow exécuté à la réception d&apos;un message entrant
                                    sur ce numéro.
                                </p>
                            </div>
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
                                : 'Créer la configuration'}
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
