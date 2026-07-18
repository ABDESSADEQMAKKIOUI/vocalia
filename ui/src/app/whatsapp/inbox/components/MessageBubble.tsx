"use client";

import { Megaphone } from 'lucide-react';

import { type WhatsAppInboxMessage } from '@/lib/messagingApi';
import { cn } from '@/lib/utils';

import { formatHourMinute } from './inboxUtils';

interface MessageBubbleProps {
    message: WhatsAppInboxMessage;
    /** Optimistic message not yet confirmed by the server. */
    pending?: boolean;
}

/**
 * WhatsApp-style bubble: inbound left/white, agent replies right/green,
 * human replies right/blue labeled "Vous", template messages italic with
 * a megaphone icon. Palette matches templates/components/TemplatePreview.
 */
export function MessageBubble({ message, pending = false }: MessageBubbleProps) {
    const outbound = message.direction === 'out';
    const isHuman = message.origin === 'human';
    const isTemplate = message.origin === 'template';

    const bubbleClass = !outbound
        ? 'bg-white text-gray-900 dark:bg-[#202C33] dark:text-gray-100'
        : isHuman
            ? 'bg-sky-100 text-gray-900 dark:bg-sky-900 dark:text-gray-100'
            : 'bg-[#D9FDD3] text-gray-900 dark:bg-[#005C4B] dark:text-gray-100';

    return (
        <div className={cn('flex flex-col', outbound ? 'items-end' : 'items-start')}>
            <div
                className={cn(
                    'max-w-[75%] rounded-lg px-3 py-2 shadow-sm',
                    bubbleClass,
                    pending && 'opacity-70',
                )}
            >
                {isHuman && (
                    <p className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-sky-700 dark:text-sky-300">
                        Vous
                    </p>
                )}
                {isTemplate ? (
                    <div className="flex items-start gap-1.5">
                        <Megaphone className="mt-0.5 h-3.5 w-3.5 shrink-0 opacity-70" />
                        <p className="whitespace-pre-wrap break-words text-sm italic">
                            {message.text}
                        </p>
                    </div>
                ) : (
                    <p className="whitespace-pre-wrap break-words text-sm">
                        {message.text}
                    </p>
                )}
            </div>
            {message.timestamp && (
                <p className="mt-0.5 px-1 text-[10px] text-gray-500 dark:text-gray-400">
                    {formatHourMinute(message.timestamp)}
                </p>
            )}
        </div>
    );
}
