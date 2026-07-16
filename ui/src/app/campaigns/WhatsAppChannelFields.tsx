"use client";

import { Label } from '@/components/ui/label';
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from '@/components/ui/select';
import type { MessagingConfiguration, WhatsAppTemplate } from '@/lib/messagingApi';

export interface WhatsAppChannelFieldsProps {
    messagingConfigs: MessagingConfiguration[];
    isLoadingMessagingConfigs: boolean;
    selectedMessagingConfigId: string;
    onMessagingConfigChange: (value: string) => void;
    templates: WhatsAppTemplate[];
    isLoadingTemplates: boolean;
    selectedTemplateId: string;
    onTemplateChange: (value: string) => void;
}

/** Loose structural view of a Meta template component (stored as dicts). */
type TemplateComponent = {
    type?: string;
    format?: string;
    text?: string;
    buttons?: Array<{ type?: string; url?: string }>;
};

const PLACEHOLDER_REGEX = /\{\{(\w+)\}\}/g;

function extractKeys(text: string): string[] {
    return Array.from(text.matchAll(PLACEHOLDER_REGEX), (match) => match[1]);
}

/**
 * Derive the placeholder keys a sender must provide for a template:
 * {{key}} occurrences in the body text, then the text header, then URL
 * buttons — deduplicated, first-occurrence order kept.
 */
export function deriveTemplatePlaceholders(components: unknown): string[] {
    const list: TemplateComponent[] = Array.isArray(components) ? components : [];
    const texts: string[] = [];
    for (const component of list) {
        if (component?.type?.toUpperCase() === 'BODY' && typeof component.text === 'string') {
            texts.push(component.text);
        }
    }
    for (const component of list) {
        if (
            component?.type?.toUpperCase() === 'HEADER' &&
            component?.format?.toUpperCase() === 'TEXT' &&
            typeof component.text === 'string'
        ) {
            texts.push(component.text);
        }
    }
    for (const component of list) {
        if (component?.type?.toUpperCase() === 'BUTTONS' && Array.isArray(component.buttons)) {
            for (const button of component.buttons) {
                if (button?.type?.toUpperCase() === 'URL' && typeof button.url === 'string') {
                    texts.push(button.url);
                }
            }
        }
    }
    const seen = new Set<string>();
    const placeholders: string[] = [];
    for (const text of texts) {
        for (const key of extractKeys(text)) {
            if (!seen.has(key)) {
                seen.add(key);
                placeholders.push(key);
            }
        }
    }
    return placeholders;
}

export default function WhatsAppChannelFields({
    messagingConfigs,
    isLoadingMessagingConfigs,
    selectedMessagingConfigId,
    onMessagingConfigChange,
    templates,
    isLoadingTemplates,
    selectedTemplateId,
    onTemplateChange,
}: WhatsAppChannelFieldsProps) {
    const selectedTemplate = templates.find(
        (template) => String(template.id) === selectedTemplateId,
    );
    const placeholders = selectedTemplate
        ? deriveTemplatePlaceholders(selectedTemplate.components)
        : [];

    return (
        <>
            <div className="space-y-2">
                <Label htmlFor="messaging-config">Configuration de messagerie</Label>
                {!isLoadingMessagingConfigs && messagingConfigs.length === 0 ? (
                    <div className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
                        Aucune configuration de messagerie. Créez-en une pour lancer une
                        campagne WhatsApp.
                    </div>
                ) : (
                    <Select
                        value={selectedMessagingConfigId}
                        onValueChange={onMessagingConfigChange}
                        required
                    >
                        <SelectTrigger id="messaging-config">
                            <SelectValue placeholder="Sélectionner une configuration de messagerie" />
                        </SelectTrigger>
                        <SelectContent>
                            {isLoadingMessagingConfigs ? (
                                <SelectItem value="loading" disabled>
                                    Chargement des configurations...
                                </SelectItem>
                            ) : (
                                messagingConfigs.map((config) => (
                                    <SelectItem
                                        key={config.id}
                                        value={config.id.toString()}
                                    >
                                        {config.name} ({config.provider})
                                    </SelectItem>
                                ))
                            )}
                        </SelectContent>
                    </Select>
                )}
                <p className="text-sm text-muted-foreground">
                    Les messages de cette campagne seront envoyés depuis les numéros de
                    cette configuration
                </p>
            </div>

            <div className="space-y-2">
                <Label htmlFor="whatsapp-template">Modèle WhatsApp</Label>
                <Select
                    value={selectedTemplateId}
                    onValueChange={onTemplateChange}
                    disabled={!selectedMessagingConfigId}
                    required
                >
                    <SelectTrigger id="whatsapp-template">
                        <SelectValue placeholder="Sélectionner un modèle approuvé" />
                    </SelectTrigger>
                    <SelectContent>
                        {isLoadingTemplates ? (
                            <SelectItem value="loading" disabled>
                                Chargement des modèles...
                            </SelectItem>
                        ) : templates.length === 0 ? (
                            <SelectItem value="none" disabled>
                                Aucun modèle approuvé pour cette configuration
                            </SelectItem>
                        ) : (
                            templates.map((template) => (
                                <SelectItem
                                    key={template.id}
                                    value={template.id.toString()}
                                >
                                    {template.name} ({template.language}, {template.category})
                                </SelectItem>
                            ))
                        )}
                    </SelectContent>
                </Select>
                <p className="text-sm text-muted-foreground">
                    Seuls les modèles approuvés par Meta peuvent être utilisés en campagne
                </p>
            </div>

            {selectedTemplate && (
                <div className="rounded-md border bg-muted/50 p-3 text-sm space-y-1">
                    <p>
                        <span className="font-medium">Colonnes CSV requises:</span>{' '}
                        {['phone_number', ...placeholders].join(', ')}
                    </p>
                    <p className="text-muted-foreground">
                        Les modèles positionnels nécessitent des colonnes littéralement
                        nommées 1, 2, …
                    </p>
                </div>
            )}
        </>
    );
}
