"use client";

import { AlertTriangle, Plus, Trash2, Variable } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
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
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
    createWhatsAppTemplate,
    updateWhatsAppTemplate,
    type WhatsAppTemplate,
} from '@/lib/messagingApi';

import { TemplatePreview } from './TemplatePreview';
import {
    buildComponents,
    type ButtonDraft,
    type ButtonType,
    draftFromComponents,
    emptyDraft,
    formatPlaceholders,
    MAX_BODY_LENGTH,
    MAX_BUTTON_TEXT_LENGTH,
    MAX_BUTTONS,
    MAX_FOOTER_LENGTH,
    MAX_HEADER_LENGTH,
    newButtonKey,
    type ParameterFormat,
    TEMPLATE_CATEGORIES,
    TEMPLATE_LANGUAGES,
    type TemplateComponent,
    type TemplateDraft,
    validateDraft,
} from './templateUtils';

const BUTTON_TYPE_LABELS: Record<ButtonType, string> = {
    QUICK_REPLY: 'Réponse rapide',
    URL: 'URL',
    PHONE_NUMBER: 'Numéro de téléphone',
};

const sanitizeName = (value: string) =>
    value.toLowerCase().replace(/[\s-]+/g, '_').replace(/[^a-z0-9_]/g, '');

const sanitizeParamName = (value: string) =>
    value.toLowerCase().replace(/[\s-]+/g, '_').replace(/[^a-z0-9_]/g, '');

type TemplateBuilderDialogProps = {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    configurationId: number | null;
    /** null → create a new DRAFT; otherwise edit this template. */
    existing: WhatsAppTemplate | null;
    onSaved: () => void;
};

export function TemplateBuilderDialog({
    open,
    onOpenChange,
    configurationId,
    existing,
    onSaved,
}: TemplateBuilderDialogProps) {
    const [draft, setDraft] = useState<TemplateDraft>(emptyDraft());
    const [namedVarInput, setNamedVarInput] = useState('');
    const [showErrors, setShowErrors] = useState(false);
    const [saving, setSaving] = useState(false);
    const bodyRef = useRef<HTMLTextAreaElement>(null);

    useEffect(() => {
        if (!open) return;
        setShowErrors(false);
        setNamedVarInput('');
        if (!existing) {
            setDraft(emptyDraft());
            return;
        }
        const parameterFormat: ParameterFormat =
            existing.parameter_format === 'named' ? 'named' : 'positional';
        setDraft({
            name: existing.name,
            language: existing.language,
            category: (existing.category || 'UTILITY').toUpperCase(),
            parameterFormat,
            ...draftFromComponents(
                (existing.components ?? []) as unknown as TemplateComponent[],
                parameterFormat,
            ),
        });
    }, [open, existing]);

    const patchDraft = (patch: Partial<TemplateDraft>) =>
        setDraft((prev) => ({ ...prev, ...patch }));

    const patchButton = (key: string, patch: Partial<ButtonDraft>) =>
        setDraft((prev) => ({
            ...prev,
            buttons: prev.buttons.map((button) =>
                button.key === key ? { ...button, ...patch } : button,
            ),
        }));

    const addButton = () =>
        setDraft((prev) => ({
            ...prev,
            buttons: [
                ...prev.buttons,
                {
                    key: newButtonKey(),
                    type: 'QUICK_REPLY',
                    text: '',
                    url: '',
                    phoneNumber: '',
                    example: '',
                },
            ],
        }));

    const removeButton = (key: string) =>
        setDraft((prev) => ({
            ...prev,
            buttons: prev.buttons.filter((button) => button.key !== key),
        }));

    const insertBodyVariable = () => {
        let token: string;
        if (draft.parameterFormat === 'positional') {
            const numbers = formatPlaceholders(draft.bodyText, 'positional').map(Number);
            token = String((numbers.length ? Math.max(...numbers) : 0) + 1);
        } else {
            token = sanitizeParamName(namedVarInput);
            if (!token) return;
            setNamedVarInput('');
        }
        const insertion = `{{${token}}}`;
        const element = bodyRef.current;
        const start = element?.selectionStart ?? draft.bodyText.length;
        const end = element?.selectionEnd ?? start;
        const next =
            draft.bodyText.slice(0, start) + insertion + draft.bodyText.slice(end);
        patchDraft({ bodyText: next });
        requestAnimationFrame(() => {
            element?.focus();
            element?.setSelectionRange(start + insertion.length, start + insertion.length);
        });
    };

    const errors = useMemo(() => validateDraft(draft), [draft]);

    const headerTokens = draft.headerEnabled
        ? formatPlaceholders(draft.headerText, draft.parameterFormat)
        : [];
    const bodyTokens = formatPlaceholders(draft.bodyText, draft.parameterFormat);
    const urlButtonsWithVariable = draft.buttons
        .map((button, index) => ({ button, index }))
        .filter(
            ({ button }) =>
                button.type === 'URL' &&
                formatPlaceholders(button.url, draft.parameterFormat).length > 0,
        );
    const hasExamples =
        headerTokens.length > 0 || bodyTokens.length > 0 || urlButtonsWithVariable.length > 0;

    const languageOptions = TEMPLATE_LANGUAGES.some(
        (option) => option.value === draft.language,
    )
        ? TEMPLATE_LANGUAGES
        : [...TEMPLATE_LANGUAGES, { value: draft.language, label: draft.language }];

    const handleSave = async () => {
        if (errors.length > 0) {
            setShowErrors(true);
            return;
        }
        if (!existing && configurationId === null) {
            toast.error("Sélectionnez d'abord une configuration");
            return;
        }
        setSaving(true);
        try {
            const components = buildComponents(draft);
            if (existing) {
                const body = {
                    name: draft.name,
                    language: draft.language,
                    category: draft.category,
                    parameter_format: draft.parameterFormat,
                    components,
                };
                await updateWhatsAppTemplate(
                    existing.id,
                    body as unknown as Parameters<typeof updateWhatsAppTemplate>[1],
                );
                toast.success('Modèle mis à jour');
            } else {
                const body = {
                    messaging_configuration_id: configurationId,
                    name: draft.name,
                    language: draft.language,
                    category: draft.category,
                    parameter_format: draft.parameterFormat,
                    components,
                };
                await createWhatsAppTemplate(
                    body as unknown as Parameters<typeof createWhatsAppTemplate>[0],
                );
                toast.success('Modèle créé en brouillon');
            }
            onOpenChange(false);
            onSaved();
        } catch (err) {
            toast.error(
                err instanceof Error ? err.message : "Échec de l'enregistrement du modèle",
            );
        } finally {
            setSaving(false);
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-5xl">
                <DialogHeader>
                    <DialogTitle>
                        {existing ? 'Modifier le modèle' : 'Nouveau modèle'}
                    </DialogTitle>
                    <DialogDescription>
                        Composez le modèle puis soumettez-le à Meta pour validation.
                    </DialogDescription>
                </DialogHeader>

                {existing?.rejection_reason && (
                    <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                        <p>
                            <span className="font-medium">Motif du rejet Meta :</span>{' '}
                            {existing.rejection_reason}
                        </p>
                    </div>
                )}

                <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
                    <div className="space-y-5">
                        {/* Identity */}
                        <div className="grid gap-4 sm:grid-cols-2">
                            <div className="grid gap-2">
                                <Label htmlFor="template-name">Nom</Label>
                                <Input
                                    id="template-name"
                                    value={draft.name}
                                    onChange={(e) =>
                                        patchDraft({ name: sanitizeName(e.target.value) })
                                    }
                                    placeholder="confirmation_commande"
                                />
                                <p className="text-xs text-muted-foreground">
                                    Minuscules, chiffres et underscores uniquement.
                                </p>
                            </div>
                            <div className="grid gap-2">
                                <Label>Langue</Label>
                                <Select
                                    value={draft.language}
                                    onValueChange={(value) => patchDraft({ language: value })}
                                >
                                    <SelectTrigger className="w-full">
                                        <SelectValue placeholder="Langue" />
                                    </SelectTrigger>
                                    <SelectContent>
                                        {languageOptions.map((option) => (
                                            <SelectItem key={option.value} value={option.value}>
                                                {option.label}
                                            </SelectItem>
                                        ))}
                                    </SelectContent>
                                </Select>
                            </div>
                            <div className="grid gap-2">
                                <Label>Catégorie</Label>
                                <Select
                                    value={draft.category}
                                    onValueChange={(value) => patchDraft({ category: value })}
                                >
                                    <SelectTrigger className="w-full">
                                        <SelectValue placeholder="Catégorie" />
                                    </SelectTrigger>
                                    <SelectContent>
                                        {TEMPLATE_CATEGORIES.map((category) => (
                                            <SelectItem key={category} value={category}>
                                                {category}
                                            </SelectItem>
                                        ))}
                                    </SelectContent>
                                </Select>
                            </div>
                            <div className="grid gap-2">
                                <Label>Format des paramètres</Label>
                                <Select
                                    value={draft.parameterFormat}
                                    onValueChange={(value) =>
                                        patchDraft({
                                            parameterFormat: value as ParameterFormat,
                                        })
                                    }
                                >
                                    <SelectTrigger className="w-full">
                                        <SelectValue placeholder="Format" />
                                    </SelectTrigger>
                                    <SelectContent>
                                        <SelectItem value="positional">
                                            Positionnel — {'{{1}}'}, {'{{2}}'}
                                        </SelectItem>
                                        <SelectItem value="named">
                                            Nommé — {'{{prenom}}'}
                                        </SelectItem>
                                    </SelectContent>
                                </Select>
                            </div>
                        </div>

                        {/* Header */}
                        <div className="space-y-2 rounded-md border p-3">
                            <div className="flex items-center justify-between">
                                <Label htmlFor="header-enabled">En-tête (texte, optionnel)</Label>
                                <Switch
                                    id="header-enabled"
                                    checked={draft.headerEnabled}
                                    onCheckedChange={(checked) =>
                                        patchDraft({ headerEnabled: checked })
                                    }
                                />
                            </div>
                            {draft.headerEnabled && (
                                <>
                                    <Input
                                        value={draft.headerText}
                                        maxLength={MAX_HEADER_LENGTH}
                                        onChange={(e) =>
                                            patchDraft({ headerText: e.target.value })
                                        }
                                        placeholder={
                                            draft.parameterFormat === 'positional'
                                                ? 'Votre commande {{1}}'
                                                : 'Votre commande {{numero_commande}}'
                                        }
                                    />
                                    <p className="text-xs text-muted-foreground">
                                        {draft.headerText.length}/{MAX_HEADER_LENGTH} — une seule
                                        variable autorisée
                                        {draft.parameterFormat === 'positional'
                                            ? ' ({{1}})'
                                            : ''}
                                        .
                                    </p>
                                </>
                            )}
                        </div>

                        {/* Body */}
                        <div className="space-y-2 rounded-md border p-3">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                                <Label htmlFor="body-text">Corps du message</Label>
                                <div className="flex items-center gap-2">
                                    {draft.parameterFormat === 'named' && (
                                        <Input
                                            value={namedVarInput}
                                            onChange={(e) =>
                                                setNamedVarInput(
                                                    sanitizeParamName(e.target.value),
                                                )
                                            }
                                            placeholder="nom_variable"
                                            className="h-8 w-36"
                                        />
                                    )}
                                    <Button
                                        type="button"
                                        variant="outline"
                                        size="sm"
                                        onClick={insertBodyVariable}
                                        disabled={
                                            draft.parameterFormat === 'named' &&
                                            !namedVarInput.trim()
                                        }
                                    >
                                        <Variable className="mr-1 h-4 w-4" />
                                        Insérer une variable
                                    </Button>
                                </div>
                            </div>
                            <Textarea
                                id="body-text"
                                ref={bodyRef}
                                value={draft.bodyText}
                                maxLength={MAX_BODY_LENGTH}
                                onChange={(e) => patchDraft({ bodyText: e.target.value })}
                                rows={6}
                                placeholder={
                                    draft.parameterFormat === 'positional'
                                        ? 'Bonjour {{1}}, votre commande {{2}} a été expédiée.'
                                        : 'Bonjour {{prenom}}, votre commande {{numero_commande}} a été expédiée.'
                                }
                            />
                            <p className="text-xs text-muted-foreground">
                                {draft.bodyText.length}/{MAX_BODY_LENGTH}
                            </p>
                        </div>

                        {/* Footer */}
                        <div className="space-y-2 rounded-md border p-3">
                            <div className="flex items-center justify-between">
                                <Label htmlFor="footer-enabled">Pied de page (optionnel)</Label>
                                <Switch
                                    id="footer-enabled"
                                    checked={draft.footerEnabled}
                                    onCheckedChange={(checked) =>
                                        patchDraft({ footerEnabled: checked })
                                    }
                                />
                            </div>
                            {draft.footerEnabled && (
                                <>
                                    <Input
                                        value={draft.footerText}
                                        maxLength={MAX_FOOTER_LENGTH}
                                        onChange={(e) =>
                                            patchDraft({ footerText: e.target.value })
                                        }
                                        placeholder="Répondez STOP pour vous désinscrire"
                                    />
                                    <p className="text-xs text-muted-foreground">
                                        {draft.footerText.length}/{MAX_FOOTER_LENGTH} — sans
                                        variables.
                                    </p>
                                </>
                            )}
                        </div>

                        {/* Buttons */}
                        <div className="space-y-3 rounded-md border p-3">
                            <div className="flex items-center justify-between">
                                <Label>
                                    Boutons ({draft.buttons.length}/{MAX_BUTTONS})
                                </Label>
                                <Button
                                    type="button"
                                    variant="outline"
                                    size="sm"
                                    onClick={addButton}
                                    disabled={draft.buttons.length >= MAX_BUTTONS}
                                >
                                    <Plus className="mr-1 h-4 w-4" />
                                    Ajouter un bouton
                                </Button>
                            </div>
                            {draft.buttons.map((button, index) => (
                                <div
                                    key={button.key}
                                    className="space-y-2 rounded-md border bg-muted/30 p-3"
                                >
                                    <div className="flex items-center gap-2">
                                        <Select
                                            value={button.type}
                                            onValueChange={(value) =>
                                                patchButton(button.key, {
                                                    type: value as ButtonType,
                                                })
                                            }
                                        >
                                            <SelectTrigger className="w-[210px]">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {(
                                                    Object.keys(
                                                        BUTTON_TYPE_LABELS,
                                                    ) as ButtonType[]
                                                ).map((type) => (
                                                    <SelectItem key={type} value={type}>
                                                        {BUTTON_TYPE_LABELS[type]}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                        <Input
                                            value={button.text}
                                            maxLength={MAX_BUTTON_TEXT_LENGTH}
                                            onChange={(e) =>
                                                patchButton(button.key, {
                                                    text: e.target.value,
                                                })
                                            }
                                            placeholder={`Libellé (max ${MAX_BUTTON_TEXT_LENGTH})`}
                                        />
                                        <Button
                                            type="button"
                                            variant="ghost"
                                            size="sm"
                                            onClick={() => removeButton(button.key)}
                                            title={`Supprimer le bouton ${index + 1}`}
                                        >
                                            <Trash2 className="h-4 w-4 text-destructive" />
                                        </Button>
                                    </div>
                                    {button.type === 'URL' && (
                                        <>
                                            <Input
                                                value={button.url}
                                                onChange={(e) =>
                                                    patchButton(button.key, {
                                                        url: e.target.value,
                                                    })
                                                }
                                                placeholder={
                                                    draft.parameterFormat === 'positional'
                                                        ? 'https://exemple.com/suivi/{{1}}'
                                                        : 'https://exemple.com/suivi/{{code}}'
                                                }
                                            />
                                            <p className="text-xs text-muted-foreground">
                                                Une seule variable autorisée, en fin d&apos;URL.
                                            </p>
                                        </>
                                    )}
                                    {button.type === 'PHONE_NUMBER' && (
                                        <Input
                                            value={button.phoneNumber}
                                            onChange={(e) =>
                                                patchButton(button.key, {
                                                    phoneNumber: e.target.value,
                                                })
                                            }
                                            placeholder="+33612345678"
                                        />
                                    )}
                                </div>
                            ))}
                        </div>

                        {/* Examples for every detected variable */}
                        {hasExamples && (
                            <div className="space-y-3 rounded-md border p-3">
                                <div>
                                    <Label>Valeurs d&apos;exemple</Label>
                                    <p className="text-xs text-muted-foreground">
                                        Meta exige une valeur d&apos;exemple pour chaque variable
                                        détectée.
                                    </p>
                                </div>
                                {headerTokens.map((token) => (
                                    <div
                                        key={`header-${token}`}
                                        className="grid grid-cols-[180px_1fr] items-center gap-2"
                                    >
                                        <span className="truncate text-sm text-muted-foreground">
                                            En-tête — {`{{${token}}}`}
                                        </span>
                                        <Input
                                            value={draft.headerExample}
                                            onChange={(e) =>
                                                patchDraft({ headerExample: e.target.value })
                                            }
                                            placeholder="Valeur d'exemple"
                                        />
                                    </div>
                                ))}
                                {bodyTokens.map((token) => (
                                    <div
                                        key={`body-${token}`}
                                        className="grid grid-cols-[180px_1fr] items-center gap-2"
                                    >
                                        <span className="truncate text-sm text-muted-foreground">
                                            Corps — {`{{${token}}}`}
                                        </span>
                                        <Input
                                            value={draft.bodyExamples[token] ?? ''}
                                            onChange={(e) =>
                                                patchDraft({
                                                    bodyExamples: {
                                                        ...draft.bodyExamples,
                                                        [token]: e.target.value,
                                                    },
                                                })
                                            }
                                            placeholder="Valeur d'exemple"
                                        />
                                    </div>
                                ))}
                                {urlButtonsWithVariable.map(({ button, index }) => (
                                    <div
                                        key={`url-${button.key}`}
                                        className="grid grid-cols-[180px_1fr] items-center gap-2"
                                    >
                                        <span className="truncate text-sm text-muted-foreground">
                                            Bouton {index + 1} (URL) —{' '}
                                            {`{{${formatPlaceholders(button.url, draft.parameterFormat)[0]}}}`}
                                        </span>
                                        <Input
                                            value={button.example}
                                            onChange={(e) =>
                                                patchButton(button.key, {
                                                    example: e.target.value,
                                                })
                                            }
                                            placeholder="Valeur d'exemple (fin d'URL)"
                                        />
                                    </div>
                                ))}
                            </div>
                        )}

                        {showErrors && errors.length > 0 && (
                            <div className="rounded-md border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive">
                                <ul className="list-disc space-y-1 pl-4">
                                    {errors.map((error) => (
                                        <li key={error}>{error}</li>
                                    ))}
                                </ul>
                            </div>
                        )}
                    </div>

                    {/* Live preview */}
                    <div className="lg:sticky lg:top-0 lg:self-start">
                        <TemplatePreview draft={draft} />
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
                        {saving
                            ? 'Enregistrement…'
                            : existing
                                ? 'Enregistrer'
                                : 'Créer le modèle'}
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
