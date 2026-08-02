"use client";

import { ArrowLeft, MessageSquareText } from 'lucide-react';
import { type ReactNode, useEffect, useRef } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import {
    type WhatsAppInboxDetail,
    type WhatsAppInboxMessage,
} from '@/lib/messagingApi';

import {
    conversationDisplayName,
    conversationInitials,
    dayKey,
    dayLabel,
    formatHourMinute,
    isServiceWindowOpen,
} from './inboxUtils';
import { MessageBubble } from './MessageBubble';
import { ReplyBox } from './ReplyBox';

interface ConversationThreadProps {
    detail: WhatsAppInboxDetail | null;
    loading: boolean;
    sending: boolean;
    togglingAgent: boolean;
    /** Optimistic outbound message appended while the send is in flight. */
    pendingMessage: WhatsAppInboxMessage | null;
    onBack: () => void;
    onToggleAgent: (paused: boolean) => void;
    onSend: (text: string) => Promise<boolean>;
}

function DaySeparator({ label }: { label: string }) {
    return (
        <div className="flex justify-center py-2">
            <span className="rounded-full bg-white/90 px-3 py-1 text-[11px] text-gray-600 shadow-sm dark:bg-[#202C33] dark:text-gray-300">
                {label}
            </span>
        </div>
    );
}

export function ConversationThread({
    detail,
    loading,
    sending,
    togglingAgent,
    pendingMessage,
    onBack,
    onToggleAgent,
    onSend,
}: ConversationThreadProps) {
    const bottomRef = useRef<HTMLDivElement | null>(null);

    const conversation = detail?.conversation ?? null;
    const messages = detail?.messages ?? [];
    const messageCount = messages.length + (pendingMessage ? 1 : 0);

    // Auto-scroll to the newest message when the thread grows or changes.
    useEffect(() => {
        bottomRef.current?.scrollIntoView({ block: 'end' });
    }, [conversation?.id, messageCount]);

    if (loading) {
        return (
            <div className="flex min-h-0 flex-1 flex-col">
                <div className="border-b p-4">
                    <Skeleton className="h-8 w-56" />
                </div>
                <div className="flex-1 space-y-3 p-4">
                    <Skeleton className="h-12 w-2/3" />
                    <Skeleton className="ml-auto h-12 w-2/3" />
                    <Skeleton className="h-12 w-1/2" />
                </div>
            </div>
        );
    }

    if (!conversation) {
        return (
            <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 text-muted-foreground">
                <MessageSquareText className="h-12 w-12" />
                <p className="text-sm">
                    Sélectionnez une conversation pour afficher les messages.
                </p>
            </div>
        );
    }

    const windowOpen = isServiceWindowOpen(conversation.service_window_expires_at);

    const items: ReactNode[] = [];
    let previousDay: string | null = null;
    messages.forEach((message, index) => {
        const timestamp = message.timestamp;
        if (timestamp) {
            const key = dayKey(timestamp);
            if (key && key !== previousDay) {
                previousDay = key;
                items.push(
                    <DaySeparator key={`day-${key}`} label={dayLabel(timestamp)} />,
                );
            }
        }
        items.push(<MessageBubble key={`msg-${index}`} message={message} />);
    });

    return (
        <div className="flex min-h-0 flex-1 flex-col">
            <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b bg-background px-4 py-3">
                <Button
                    variant="ghost"
                    size="icon"
                    className="md:hidden"
                    onClick={onBack}
                    aria-label="Retour aux conversations"
                >
                    <ArrowLeft className="h-5 w-5" />
                </Button>
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#075E54] text-xs font-semibold text-white">
                    {conversationInitials(conversation)}
                </div>
                <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold">
                        {conversationDisplayName(conversation)}
                    </p>
                    <p className="truncate text-xs text-muted-foreground">
                        +{conversation.wa_id}
                        {conversation.display_number
                            ? ` · via ${conversation.display_number}`
                            : ''}
                        {conversation.agent_name ? ` · ${conversation.agent_name}` : ''}
                    </p>
                </div>
                {windowOpen ? (
                    <Badge className="bg-emerald-600 text-white hover:bg-emerald-600">
                        Fenêtre ouverte — expire à{' '}
                        {formatHourMinute(conversation.service_window_expires_at)}
                    </Badge>
                ) : (
                    <Badge variant="secondary">Fenêtre fermée</Badge>
                )}
                <div className="flex items-center gap-2">
                    <Label htmlFor="agent-ia-switch" className="text-xs font-medium">
                        Agent IA
                    </Label>
                    <Switch
                        id="agent-ia-switch"
                        checked={!conversation.agent_paused}
                        disabled={togglingAgent}
                        onCheckedChange={(checked) => onToggleAgent(!checked)}
                        aria-label="Activer ou mettre en pause l'agent IA"
                    />
                </div>
            </header>
            <div className="min-h-0 flex-1 overflow-y-auto bg-[#ECE5DD] p-4 dark:bg-[#0B141A]">
                <div className="mx-auto flex max-w-3xl flex-col gap-1.5">
                    {items.length === 0 && !pendingMessage && (
                        <DaySeparator label="Aucun message dans cette conversation" />
                    )}
                    {items}
                    {pendingMessage && (
                        <MessageBubble message={pendingMessage} pending />
                    )}
                    <div ref={bottomRef} />
                </div>
            </div>
            <ReplyBox
                windowClosed={!windowOpen}
                sending={sending}
                onSend={onSend}
            />
        </div>
    );
}
