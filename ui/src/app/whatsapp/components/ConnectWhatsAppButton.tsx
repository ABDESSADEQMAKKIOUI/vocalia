"use client";

import { Loader2, MessageCircle } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { useAuth } from '@/lib/auth';
import { launchEmbeddedSignup, loadFacebookSdk } from '@/lib/facebookSdk';
import {
    completeEmbeddedSignup,
    type EmbeddedSignupConfig,
    getEmbeddedSignupConfig,
} from '@/lib/messagingApi';

type ConnectWhatsAppButtonProps = {
    /** Called after a configuration is successfully provisioned, to refresh the list. */
    onConnected: () => void;
};

/**
 * "Connecter un compte WhatsApp" — opens Meta's Embedded Signup popup and, once
 * the user has selected/created their WABA + number, provisions the messaging
 * configuration through the backend (no manual token paste).
 *
 * Renders nothing when the feature is not configured on the deployment
 * (env vars unset -> `enabled === false`) or the config endpoint is
 * unavailable, so the page still offers the manual (advanced) fallback.
 */
export function ConnectWhatsAppButton({ onConnected }: ConnectWhatsAppButtonProps) {
    const { user, loading: authLoading } = useAuth();
    const [config, setConfig] = useState<EmbeddedSignupConfig | null>(null);
    const [connecting, setConnecting] = useState(false);
    const hasFetched = useRef(false);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        getEmbeddedSignupConfig()
            .then(setConfig)
            .catch(() => {
                // Endpoint unavailable (older backend / not deployed): treat as
                // disabled and fall back to the manual flow.
                setConfig({
                    enabled: false,
                    app_id: null,
                    config_id: null,
                    graph_version: '',
                });
            });
    }, [authLoading, user]);

    const handleConnect = async () => {
        if (!config?.enabled || !config.app_id || !config.config_id) return;
        setConnecting(true);
        try {
            await loadFacebookSdk(config.app_id, config.graph_version);
            const result = await launchEmbeddedSignup(config.config_id);
            if (!result.waba_id || !result.phone_number_id) {
                toast.error(
                    "La connexion n'a pas abouti — veuillez sélectionner un compte WhatsApp Business.",
                );
                return;
            }
            await completeEmbeddedSignup({
                code: result.code,
                waba_id: result.waba_id,
                phone_number_id: result.phone_number_id,
                business_id: result.business_id ?? null,
            });
            toast.success('Compte WhatsApp connecté');
            onConnected();
        } catch (err) {
            const message =
                err instanceof Error ? err.message : 'Échec de la connexion WhatsApp';
            // Closing the popup rejects with "Connexion annulée." — surface it as
            // a low-key notice rather than a hard error.
            if (message === 'Connexion annulée.') {
                toast.info(message);
            } else {
                toast.error(message);
            }
        } finally {
            setConnecting(false);
        }
    };

    if (!config?.enabled) {
        return null;
    }

    return (
        <div className="flex flex-col items-start gap-1">
            <Button onClick={handleConnect} disabled={connecting}>
                {connecting ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                    <MessageCircle className="h-4 w-4 mr-2" />
                )}
                {connecting ? 'Connexion…' : 'Connecter un compte WhatsApp'}
            </Button>
            <p className="text-xs text-muted-foreground">
                Connectez votre compte Meta et sélectionnez votre WhatsApp Business —
                sans copier de jeton.
            </p>
        </div>
    );
}
