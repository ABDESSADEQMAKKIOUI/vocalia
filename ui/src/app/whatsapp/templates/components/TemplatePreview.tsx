"use client";

import { ExternalLink, Phone, Reply } from 'lucide-react';

import {
    formatPlaceholders,
    substitutePlaceholders,
    type TemplateDraft,
} from './templateUtils';

function ButtonIcon({ type }: { type: string }) {
    if (type === 'URL') return <ExternalLink className="h-3.5 w-3.5" />;
    if (type === 'PHONE_NUMBER') return <Phone className="h-3.5 w-3.5" />;
    return <Reply className="h-3.5 w-3.5" />;
}

/**
 * WhatsApp-style live preview: variables are substituted with their
 * example values; unfilled ones stay visible as {{…}}.
 */
export function TemplatePreview({ draft }: { draft: TemplateDraft }) {
    const headerValues: Record<string, string> = {};
    for (const token of formatPlaceholders(draft.headerText, draft.parameterFormat)) {
        headerValues[token] = draft.headerExample;
    }
    const headerRendered =
        draft.headerEnabled && draft.headerText.trim()
            ? substitutePlaceholders(draft.headerText, headerValues)
            : '';
    const bodyRendered = substitutePlaceholders(draft.bodyText, draft.bodyExamples);
    const footerRendered =
        draft.footerEnabled && draft.footerText.trim() ? draft.footerText : '';
    const buttons = draft.buttons.filter((button) => button.text.trim());

    return (
        <div className="overflow-hidden rounded-lg border">
            <div className="bg-[#075E54] px-3 py-2 text-sm font-medium text-white">
                Aperçu WhatsApp
            </div>
            <div className="min-h-[240px] bg-[#ECE5DD] p-4 dark:bg-[#0B141A]">
                <div className="max-w-[85%] overflow-hidden rounded-lg bg-white shadow-sm dark:bg-[#202C33]">
                    <div className="space-y-1 px-3 pt-2">
                        {headerRendered && (
                            <p className="break-words text-sm font-semibold text-gray-900 dark:text-gray-100">
                                {headerRendered}
                            </p>
                        )}
                        <p className="whitespace-pre-wrap break-words text-sm text-gray-900 dark:text-gray-100">
                            {bodyRendered || (
                                <span className="italic text-muted-foreground">
                                    Le corps du message s&apos;affichera ici…
                                </span>
                            )}
                        </p>
                        {footerRendered && (
                            <p className="break-words text-xs text-gray-500 dark:text-gray-400">
                                {footerRendered}
                            </p>
                        )}
                        <p className="pb-1 text-right text-[10px] text-gray-400">12:00</p>
                    </div>
                    {buttons.length > 0 && (
                        <div className="divide-y border-t dark:divide-gray-700 dark:border-gray-700">
                            {buttons.map((button) => (
                                <div
                                    key={button.key}
                                    className="flex items-center justify-center gap-1.5 py-2 text-sm font-medium text-[#00A5F4]"
                                >
                                    <ButtonIcon type={button.type} />
                                    <span className="truncate">{button.text}</span>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
