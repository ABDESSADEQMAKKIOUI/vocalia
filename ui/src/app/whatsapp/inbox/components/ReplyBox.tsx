"use client";

import { Send } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
    Tooltip,
    TooltipContent,
    TooltipTrigger,
} from '@/components/ui/tooltip';

interface ReplyBoxProps {
    /** True when the 24h service window is closed — input disabled. */
    windowClosed: boolean;
    sending: boolean;
    /** Resolves true on success; on false the draft text is restored. */
    onSend: (text: string) => Promise<boolean>;
}

export function ReplyBox({ windowClosed, sending, onSend }: ReplyBoxProps) {
    const [text, setText] = useState('');

    const submit = async () => {
        const value = text.trim();
        if (!value || windowClosed || sending) return;
        // Optimistic: clear immediately, restore only if the send fails and
        // the user has not started typing a new message meanwhile.
        setText('');
        const ok = await onSend(value);
        if (!ok) {
            setText((current) => (current.length > 0 ? current : value));
        }
    };

    const field = (
        <div className="flex items-end gap-2 border-t bg-background p-3">
            <Textarea
                value={text}
                onChange={(event) => setText(event.target.value)}
                onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault();
                        void submit();
                    }
                }}
                placeholder={
                    windowClosed
                        ? 'Fenêtre de 24h fermée'
                        : 'Écrivez un message… (Entrée pour envoyer)'
                }
                disabled={windowClosed}
                rows={1}
                className="max-h-32 min-h-[40px] flex-1 resize-none"
                aria-label="Message"
            />
            <Button
                size="icon"
                onClick={() => void submit()}
                disabled={windowClosed || sending || text.trim().length === 0}
                aria-label="Envoyer"
                title="Envoyer"
            >
                <Send className="h-4 w-4" />
            </Button>
        </div>
    );

    if (!windowClosed) {
        return field;
    }

    return (
        <Tooltip>
            <TooltipTrigger asChild>
                <div className="cursor-not-allowed">{field}</div>
            </TooltipTrigger>
            <TooltipContent side="top">
                La fenêtre de 24h est fermée — envoyez un modèle WhatsApp pour
                recontacter ce contact.
            </TooltipContent>
        </Tooltip>
    );
}
