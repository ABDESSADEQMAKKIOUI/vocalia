"use client";

import { AlertTriangle, CheckCircle2, Copy, Webhook } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import {
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
} from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import type { WebhookInfo } from '@/lib/messagingApi';

function StatusChip({
    ok,
    okLabel,
    missingLabel,
}: {
    ok: boolean;
    okLabel: string;
    missingLabel: string;
}) {
    return ok ? (
        <span className="inline-flex items-center gap-1.5 rounded-md border border-emerald-300 bg-emerald-50 px-2 py-1 text-xs font-medium text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200">
            <CheckCircle2 className="h-3.5 w-3.5" />
            {okLabel}
        </span>
    ) : (
        <span className="inline-flex items-center gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2 py-1 text-xs font-medium text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
            <AlertTriangle className="h-3.5 w-3.5" />
            {missingLabel}
        </span>
    );
}

type WebhookInfoCardProps = {
    info: WebhookInfo | null;
    loading: boolean;
};

export function WebhookInfoCard({ info, loading }: WebhookInfoCardProps) {
    const handleCopy = () => {
        if (!info) return;
        navigator.clipboard
            .writeText(info.webhook_url)
            .then(() => toast.success('URL du webhook copiée'))
            .catch(() => toast.error('Échec de la copie'));
    };

    const missingVars = info
        ? ([
            !info.verify_token_set && 'WHATSAPP_VERIFY_TOKEN',
            !info.app_secret_set && 'WHATSAPP_APP_SECRET',
        ].filter(Boolean) as string[])
        : [];

    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <Webhook className="h-5 w-5" />
                    Configuration du webhook
                </CardTitle>
                <CardDescription>
                    Recevez les messages entrants et les statuts de livraison envoyés
                    par Meta.
                </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
                {loading ? (
                    <div className="space-y-3">
                        <Skeleton className="h-10 w-full" />
                        <Skeleton className="h-6 w-64" />
                    </div>
                ) : !info ? (
                    <p className="text-sm text-muted-foreground">
                        Impossible de charger les informations du webhook.
                    </p>
                ) : (
                    <>
                        <div className="flex items-center gap-2">
                            <code className="min-w-0 flex-1 truncate rounded-md border bg-muted/40 px-3 py-2 font-mono text-sm">
                                {info.webhook_url}
                            </code>
                            <Button variant="outline" size="sm" onClick={handleCopy}>
                                <Copy className="h-4 w-4 mr-2" />
                                Copier
                            </Button>
                        </div>

                        <div className="flex flex-wrap items-center gap-2">
                            <StatusChip
                                ok={info.verify_token_set}
                                okLabel="Verify token configuré"
                                missingLabel="Verify token manquant"
                            />
                            <StatusChip
                                ok={info.app_secret_set}
                                okLabel="App secret configuré"
                                missingLabel="App secret manquant"
                            />
                        </div>

                        {missingVars.length > 0 && (
                            <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
                                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                                <p>
                                    Définissez la ou les variables d&apos;environnement{' '}
                                    <span className="font-mono">
                                        {missingVars.join(', ')}
                                    </span>{' '}
                                    sur le serveur API pour sécuriser la vérification et la
                                    signature du webhook.
                                </p>
                            </div>
                        )}

                        <p className="text-sm text-muted-foreground">
                            Dans le Meta App Dashboard, ouvrez{' '}
                            <span className="whitespace-nowrap">
                                WhatsApp → Configuration
                            </span>
                            , collez cette URL dans le champ « Callback URL », saisissez
                            votre verify token puis abonnez-vous au champ « messages ».
                        </p>
                    </>
                )}
            </CardContent>
        </Card>
    );
}
