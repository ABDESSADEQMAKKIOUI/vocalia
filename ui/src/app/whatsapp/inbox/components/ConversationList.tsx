"use client";

import { Inbox, PauseCircle } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { type WhatsAppInboxConversation } from '@/lib/messagingApi';
import { cn } from '@/lib/utils';

import {
    conversationDisplayName,
    conversationInitials,
    formatRelativeTime,
} from './inboxUtils';

export type InboxStateFilter = 'open' | 'closed' | 'all';

interface ConversationListProps {
    conversations: WhatsAppInboxConversation[];
    loading: boolean;
    selectedId: number | null;
    filter: InboxStateFilter;
    onFilterChange: (filter: InboxStateFilter) => void;
    onSelect: (id: number) => void;
}

function StateBadge({ state }: { state: string }) {
    if (state === 'closed') {
        return (
            <Badge variant="outline" className="px-1.5 py-0 text-[10px]">
                Fermée
            </Badge>
        );
    }
    return <Badge className="px-1.5 py-0 text-[10px]">Ouverte</Badge>;
}

export function ConversationList({
    conversations,
    loading,
    selectedId,
    filter,
    onFilterChange,
    onSelect,
}: ConversationListProps) {
    return (
        <div className="flex min-h-0 flex-1 flex-col">
            <div className="border-b p-3">
                <Tabs
                    value={filter}
                    onValueChange={(value) => onFilterChange(value as InboxStateFilter)}
                >
                    <TabsList className="w-full">
                        <TabsTrigger value="open" className="flex-1">
                            Actives
                        </TabsTrigger>
                        <TabsTrigger value="closed" className="flex-1">
                            Fermées
                        </TabsTrigger>
                        <TabsTrigger value="all" className="flex-1">
                            Toutes
                        </TabsTrigger>
                    </TabsList>
                </Tabs>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
                {loading ? (
                    <div className="space-y-3 p-3">
                        {[...Array(6)].map((_, i) => (
                            <div key={i} className="flex items-center gap-3">
                                <Skeleton className="h-10 w-10 shrink-0 rounded-full" />
                                <div className="flex-1 space-y-1.5">
                                    <Skeleton className="h-3.5 w-2/3" />
                                    <Skeleton className="h-3 w-full" />
                                </div>
                            </div>
                        ))}
                    </div>
                ) : conversations.length === 0 ? (
                    <div className="flex flex-col items-center gap-3 px-4 py-12 text-center text-muted-foreground">
                        <Inbox className="h-10 w-10" />
                        <p className="text-sm">Aucune conversation pour le moment.</p>
                    </div>
                ) : (
                    conversations.map((conversation) => (
                        <button
                            key={conversation.id}
                            type="button"
                            onClick={() => onSelect(conversation.id)}
                            className={cn(
                                'flex w-full items-start gap-3 border-b px-3 py-3 text-left transition-colors hover:bg-accent/50',
                                selectedId === conversation.id && 'bg-accent',
                            )}
                        >
                            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-[#075E54] text-sm font-semibold text-white">
                                {conversationInitials(conversation)}
                            </div>
                            <div className="min-w-0 flex-1">
                                <div className="flex items-center justify-between gap-2">
                                    <span className="truncate text-sm font-medium">
                                        {conversationDisplayName(conversation)}
                                    </span>
                                    <span className="shrink-0 text-xs text-muted-foreground">
                                        {formatRelativeTime(conversation.updated_at)}
                                    </span>
                                </div>
                                <div className="mt-0.5 flex items-center justify-between gap-2">
                                    <span className="truncate text-xs text-muted-foreground">
                                        {conversation.last_message_preview ?? 'Aucun message'}
                                    </span>
                                    <span className="flex shrink-0 items-center gap-1.5">
                                        {conversation.agent_paused && (
                                            <PauseCircle
                                                className="h-3.5 w-3.5 text-amber-500"
                                                aria-label="Agent en pause"
                                            />
                                        )}
                                        <StateBadge state={conversation.state} />
                                    </span>
                                </div>
                            </div>
                        </button>
                    ))
                )}
            </div>
        </div>
    );
}
