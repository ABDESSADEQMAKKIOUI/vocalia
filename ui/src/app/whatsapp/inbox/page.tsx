"use client";

import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { Skeleton } from '@/components/ui/skeleton';
import { useAuth } from '@/lib/auth';
import {
    getWhatsAppConversation,
    listWhatsAppConversations,
    sendWhatsAppReply,
    setWhatsAppAgentPaused,
    type WhatsAppInboxConversation,
    type WhatsAppInboxDetail,
    type WhatsAppInboxMessage,
} from '@/lib/messagingApi';
import { cn } from '@/lib/utils';

import {
    ConversationList,
    type InboxStateFilter,
} from './components/ConversationList';
import { ConversationThread } from './components/ConversationThread';

const LIST_POLL_MS = 8000;
const DETAIL_POLL_MS = 4000;

export default function WhatsAppInboxPage() {
    const { user, redirectToLogin, loading: authLoading } = useAuth();

    const [filter, setFilter] = useState<InboxStateFilter>('open');
    const [conversations, setConversations] = useState<WhatsAppInboxConversation[]>(
        [],
    );
    const [loadingList, setLoadingList] = useState(true);
    const [selectedId, setSelectedId] = useState<number | null>(null);
    const [detail, setDetail] = useState<WhatsAppInboxDetail | null>(null);
    const [loadingDetail, setLoadingDetail] = useState(false);
    const [pendingMessage, setPendingMessage] =
        useState<WhatsAppInboxMessage | null>(null);
    const [sending, setSending] = useState(false);
    const [togglingAgent, setTogglingAgent] = useState(false);

    // Refs so interval callbacks and late responses see the current values.
    const sendingRef = useRef(false);
    const selectedIdRef = useRef<number | null>(null);
    useEffect(() => {
        selectedIdRef.current = selectedId;
    }, [selectedId]);

    // Redirect if not authenticated
    useEffect(() => {
        if (!authLoading && !user) {
            redirectToLogin();
        }
    }, [authLoading, user, redirectToLogin]);

    const fetchList = useCallback(
        async (silent = false) => {
            if (!silent) setLoadingList(true);
            try {
                setConversations(await listWhatsAppConversations({ state: filter }));
            } catch (err) {
                if (!silent) {
                    toast.error(
                        err instanceof Error
                            ? err.message
                            : 'Échec du chargement des conversations',
                    );
                }
            } finally {
                if (!silent) setLoadingList(false);
            }
        },
        [filter],
    );

    const fetchDetail = useCallback(async (id: number, silent = false) => {
        if (!silent) setLoadingDetail(true);
        try {
            const data = await getWhatsAppConversation(id);
            // Ignore late responses for a deselected conversation, and never
            // clobber the optimistic thread while a send is in flight.
            if (selectedIdRef.current === id && !sendingRef.current) {
                setDetail(data);
            }
        } catch (err) {
            if (!silent) {
                toast.error(
                    err instanceof Error
                        ? err.message
                        : 'Échec du chargement de la conversation',
                );
            }
        } finally {
            if (!silent) setLoadingDetail(false);
        }
    }, []);

    // Poll the conversation list every 8s (immediately on filter change).
    useEffect(() => {
        if (authLoading || !user) return;
        fetchList();
        const timer = setInterval(() => {
            if (sendingRef.current) return;
            void fetchList(true);
        }, LIST_POLL_MS);
        return () => clearInterval(timer);
    }, [authLoading, user, fetchList]);

    // Poll the selected conversation every 4s.
    useEffect(() => {
        if (authLoading || !user || selectedId === null) return;
        fetchDetail(selectedId);
        const timer = setInterval(() => {
            if (sendingRef.current) return;
            void fetchDetail(selectedId, true);
        }, DETAIL_POLL_MS);
        return () => clearInterval(timer);
    }, [authLoading, user, selectedId, fetchDetail]);

    const handleSelect = (id: number) => {
        if (id === selectedId) return;
        setSelectedId(id);
        setDetail(null);
        setPendingMessage(null);
        // Show the thread skeleton right away (the fetch effect confirms it).
        setLoadingDetail(true);
    };

    const handleBack = () => {
        setSelectedId(null);
        setDetail(null);
        setPendingMessage(null);
    };

    const handleSend = useCallback(
        async (text: string): Promise<boolean> => {
            if (selectedId === null) return false;
            const id = selectedId;
            sendingRef.current = true;
            setSending(true);
            setPendingMessage({
                direction: 'out',
                origin: 'human',
                text,
                timestamp: new Date().toISOString(),
            });
            try {
                const fresh = await sendWhatsAppReply(id, text);
                if (selectedIdRef.current === id) {
                    setDetail(fresh);
                }
                void fetchList(true);
                return true;
            } catch (err) {
                toast.error(
                    err instanceof Error ? err.message : "Échec de l'envoi du message",
                );
                return false;
            } finally {
                sendingRef.current = false;
                setSending(false);
                setPendingMessage(null);
            }
        },
        [selectedId, fetchList],
    );

    const handleToggleAgent = useCallback(
        async (paused: boolean) => {
            if (selectedId === null) return;
            setTogglingAgent(true);
            try {
                const result = await setWhatsAppAgentPaused(selectedId, paused);
                setDetail((current) =>
                    current && current.conversation.id === result.id
                        ? {
                              ...current,
                              conversation: {
                                  ...current.conversation,
                                  agent_paused: result.agent_paused,
                              },
                          }
                        : current,
                );
                setConversations((current) =>
                    current.map((conversation) =>
                        conversation.id === result.id
                            ? { ...conversation, agent_paused: result.agent_paused }
                            : conversation,
                    ),
                );
                toast.success(
                    result.agent_paused
                        ? 'Agent mis en pause — vous répondez manuellement'
                        : 'Agent réactivé',
                );
            } catch (err) {
                toast.error(
                    err instanceof Error
                        ? err.message
                        : "Échec de la mise à jour de l'agent",
                );
            } finally {
                setTogglingAgent(false);
            }
        },
        [selectedId],
    );

    if (authLoading || !user) {
        return (
            <div className="min-h-screen flex items-center justify-center">
                <div className="space-y-4">
                    <Skeleton className="h-12 w-64" />
                    <Skeleton className="h-64 w-96" />
                </div>
            </div>
        );
    }

    return (
        <div className="flex h-[calc(100dvh-3.5rem)] overflow-hidden">
            {/* Left pane: conversation list (full width on mobile) */}
            <aside
                className={cn(
                    'w-full flex-col border-r md:flex md:w-[340px] md:shrink-0',
                    selectedId !== null ? 'hidden' : 'flex',
                )}
            >
                <div className="border-b px-4 py-3">
                    <h1 className="text-lg font-semibold">Boîte de réception</h1>
                    <p className="text-xs text-muted-foreground">
                        Conversations WhatsApp
                    </p>
                </div>
                <ConversationList
                    conversations={conversations}
                    loading={loadingList}
                    selectedId={selectedId}
                    filter={filter}
                    onFilterChange={setFilter}
                    onSelect={handleSelect}
                />
            </aside>

            {/* Right pane: thread (hidden on mobile until a selection) */}
            <section
                className={cn(
                    'min-w-0 flex-1 flex-col',
                    selectedId === null ? 'hidden md:flex' : 'flex',
                )}
            >
                <ConversationThread
                    detail={detail}
                    loading={selectedId !== null && detail === null && loadingDetail}
                    sending={sending}
                    togglingAgent={togglingAgent}
                    pendingMessage={pendingMessage}
                    onBack={handleBack}
                    onToggleAgent={handleToggleAgent}
                    onSend={handleSend}
                />
            </section>
        </div>
    );
}
