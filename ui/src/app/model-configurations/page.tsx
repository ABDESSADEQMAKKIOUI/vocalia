"use client";

import { Brain, Lock } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import {
    getAuthUserApiV1UserAuthUserGet,
    getModelConfigurationV2ApiV1OrganizationsModelConfigurationsV2Get,
} from "@/client/sdk.gen";
import ModelConfigurationV2 from "@/components/ModelConfigurationV2";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { SETTINGS_DOCUMENTATION_URLS } from "@/constants/documentation";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

/**
 * Model configuration is a platform-admin surface.
 *
 * Why read-only instead of a redirect: this page is still linked from Overview
 * and Billing, and users had it in their sidebar until now. A page that simply
 * disappears (or bounces elsewhere) reads as a bug and produces a support
 * ticket. Showing what the administrator has set — masked, with no way to
 * change it and a sentence saying who owns the decision — answers the question
 * the user actually has ("which model am I running?") without pretending they
 * can act on it.
 *
 * The real access control is server-side: PUT/POST on
 * /api/v1/organizations/model-configurations/v2 now require a superuser
 * (api/routes/organization.py). Everything here is presentation.
 */

type Role = "checking" | "admin" | "member";

type ServiceSummary = {
    label: string;
    provider: string;
    model: string;
    voice?: string;
    language?: string;
};

const SOURCE_LABELS: Record<string, string> = {
    organization_v2: "Configuration de l'organisation",
    legacy_user_v1: "Ancienne configuration (v1)",
    empty: "Aucune configuration",
};

/** Pull provider/model/voice out of one service block of the masked effective config. */
function summarizeService(
    effective: Record<string, unknown>,
    key: string,
    label: string,
): ServiceSummary | null {
    const block = effective[key];
    if (!block || typeof block !== "object") return null;
    const record = block as Record<string, unknown>;
    const provider = typeof record.provider === "string" ? record.provider : "";
    const model = typeof record.model === "string" ? record.model : "";
    if (!provider && !model) return null;
    return {
        label,
        provider: provider || "—",
        model: model || "—",
        voice: typeof record.voice === "string" ? record.voice : undefined,
        language: typeof record.language === "string" ? record.language : undefined,
    };
}

function summarizeEffectiveConfiguration(
    effective: Record<string, unknown>,
): ServiceSummary[] {
    // A realtime organization runs a single speech-to-speech service; a
    // pipeline organization runs the STT/LLM/TTS trio. Showing both would
    // display services that are not actually in the call path.
    if (effective.is_realtime === true) {
        const realtime = summarizeService(effective, "realtime", "Temps réel (voix à voix)");
        const llm = summarizeService(effective, "llm", "LLM (outils et analyse)");
        return [realtime, llm].filter((s): s is ServiceSummary => s !== null);
    }
    return [
        summarizeService(effective, "stt", "Transcription (STT)"),
        summarizeService(effective, "llm", "Modèle de langage (LLM)"),
        summarizeService(effective, "tts", "Synthèse vocale (TTS)"),
    ].filter((s): s is ServiceSummary => s !== null);
}

export default function ServiceConfigurationPage() {
    const { user, loading: authLoading, getAccessToken } = useAuth();
    const [role, setRole] = useState<Role>("checking");
    const [summary, setSummary] = useState<ServiceSummary[] | null>(null);
    const [source, setSource] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const hasChecked = useRef(false);

    useEffect(() => {
        // The auth interceptor is only registered once auth is loaded; fetching
        // earlier sends an unauthenticated request that silently fails.
        if (authLoading || !user || hasChecked.current) return;
        hasChecked.current = true;

        const load = async () => {
            let isSuperuser = false;
            try {
                const accessToken = await getAccessToken();
                const authUser = await getAuthUserApiV1UserAuthUserGet({
                    headers: { Authorization: `Bearer ${accessToken}` },
                });
                isSuperuser = Boolean(authUser.data?.is_superuser);
            } catch {
                // Unknown role: fall back to the restricted view. The server
                // rejects writes anyway, so guessing "admin" here would only
                // show an editor whose save button always fails.
                isSuperuser = false;
            }

            if (isSuperuser) {
                setRole("admin");
                return;
            }

            setRole("member");
            const response = await getModelConfigurationV2ApiV1OrganizationsModelConfigurationsV2Get();
            if (response.error) {
                setError(detailFromError(response.error, "Impossible de charger la configuration des modèles"));
                return;
            }
            if (!response.data) return;
            setSource(response.data.source);
            setSummary(summarizeEffectiveConfiguration(response.data.effective_configuration));
        };

        load();
    }, [authLoading, user, getAccessToken]);

    if (authLoading || role === "checking") {
        return (
            <div className="container mx-auto space-y-4 px-4 py-8">
                <Skeleton className="h-10 w-72" />
                <Skeleton className="h-64 w-full" />
            </div>
        );
    }

    if (role === "admin") {
        return (
            <div className="min-h-screen">
                <div className="container mx-auto px-4 py-8">
                    <div className="mx-auto max-w-4xl">
                        <ModelConfigurationV2 docsUrl={SETTINGS_DOCUMENTATION_URLS.modelOverrides ?? undefined} />
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen">
            <div className="container mx-auto px-4 py-8">
                <div className="mx-auto max-w-4xl space-y-6">
                    <Card>
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2">
                                <Lock className="h-5 w-5 text-muted-foreground" />
                                Modèles gérés par l&apos;administrateur
                            </CardTitle>
                            <CardDescription>
                                Le choix des modèles (et les clés fournisseur associées) est réglé
                                une fois pour toute l&apos;organisation par l&apos;administrateur de
                                la plateforme. Sur vos agents, vous choisissez la langue et la voix.
                            </CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            {error && (
                                <p className="text-sm text-destructive">{error}</p>
                            )}

                            {!error && summary && summary.length === 0 && (
                                <p className="text-sm text-muted-foreground">
                                    Aucun modèle n&apos;est configuré pour le moment. Demandez à
                                    votre administrateur de plateforme de terminer la configuration.
                                </p>
                            )}

                            {!error && summary === null && (
                                <div className="space-y-2">
                                    <Skeleton className="h-12 w-full" />
                                    <Skeleton className="h-12 w-full" />
                                </div>
                            )}

                            {!error && summary && summary.length > 0 && (
                                <div className="space-y-3">
                                    {source && (
                                        <Badge variant="secondary" className="font-normal">
                                            {SOURCE_LABELS[source] ?? source}
                                        </Badge>
                                    )}
                                    <div className="divide-y rounded-lg border">
                                        {summary.map((service) => (
                                            <div
                                                key={service.label}
                                                className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3"
                                            >
                                                <span className="text-sm font-medium">{service.label}</span>
                                                <span className="text-sm text-muted-foreground">
                                                    {service.provider} · {service.model}
                                                    {service.voice ? ` · voix ${service.voice}` : ""}
                                                    {service.language ? ` · ${service.language}` : ""}
                                                </span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}

                            <div className="pt-2">
                                <Button asChild variant="outline">
                                    <Link href="/workflow">
                                        <Brain className="mr-2 h-4 w-4" />
                                        Choisir la langue et la voix d&apos;un agent
                                    </Link>
                                </Button>
                            </div>
                        </CardContent>
                    </Card>
                </div>
            </div>
        </div>
    );
}
