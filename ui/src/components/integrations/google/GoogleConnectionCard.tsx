"use client";

import { AlertTriangle, CheckCircle2, Link2, Loader2, Unlink } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { GoogleConnectionStatus } from "@/lib/googleWorkspace";

import { formatTimestamp, scopeLabel } from "./scopeLabels";

interface GoogleConnectionCardProps {
    status: GoogleConnectionStatus | null;
    loading: boolean;
    connecting: boolean;
    disconnecting: boolean;
    error?: string | null;
    onConnect: () => void;
    onDisconnect: () => void;
}

/**
 * The connection half of the page: who is connected, with which permissions,
 * and the two actions that change that.
 *
 * A revoked connection is deliberately not shown as "disconnected". Google can
 * drop a grant without the organization doing anything, and the remedy differs:
 * a disconnected org connects, a revoked one has to walk the consent screen
 * again before anything works. Collapsing the two states hides that.
 */
export function GoogleConnectionCard({
    status,
    loading,
    connecting,
    disconnecting,
    error,
    onConnect,
    onDisconnect,
}: GoogleConnectionCardProps) {
    if (loading) {
        return (
            <Card>
                <CardHeader>
                    <CardTitle>Connection</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                    <Skeleton className="h-5 w-56" />
                    <Skeleton className="h-4 w-40" />
                    <Skeleton className="h-9 w-36" />
                </CardContent>
            </Card>
        );
    }

    const configured = status?.oauth_config.enabled ?? false;
    const connected = Boolean(status?.connected);
    const revoked = Boolean(status?.requires_reconsent);
    const expiresAt = formatTimestamp(status?.expires_at);

    return (
        <Card>
            <CardHeader>
                <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                        <CardTitle>Connection</CardTitle>
                        <CardDescription>
                            One Google account per organization. Everything an agent reads or
                            writes goes through it.
                        </CardDescription>
                    </div>
                    {connected ? (
                        <Badge variant="success" className="gap-1">
                            <CheckCircle2 className="h-3 w-3" />
                            Active
                        </Badge>
                    ) : revoked ? (
                        <Badge variant="destructive">Revoked</Badge>
                    ) : (
                        <Badge variant="secondary">Not connected</Badge>
                    )}
                </div>
            </CardHeader>

            <CardContent className="space-y-4">
                {error && (
                    <div className="rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive">
                        {error}
                    </div>
                )}

                {!configured && (
                    <div className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground">
                        Google is not configured on this deployment. An operator has to set
                        the Google OAuth client credentials before an account can be
                        connected.
                    </div>
                )}

                {connected ? (
                    <>
                        <div className="space-y-1">
                            <p className="text-sm text-muted-foreground">Connected account</p>
                            <p className="font-medium break-all">
                                {status?.account_email ?? "Google account (email not shared)"}
                            </p>
                            {!status?.account_email && (
                                <p className="text-xs text-muted-foreground">
                                    The granted permissions do not include an identity scope, so
                                    Google does not tell us which account this is.
                                </p>
                            )}
                        </div>

                        {expiresAt && (
                            <p className="text-xs text-muted-foreground">
                                Access token renews automatically — current one valid until{" "}
                                {expiresAt}.
                            </p>
                        )}

                        <div className="space-y-2">
                            <p className="text-sm text-muted-foreground">Granted permissions</p>
                            {status && status.scopes.length > 0 ? (
                                <div className="flex flex-wrap gap-1.5">
                                    {status.scopes.map((scope) => (
                                        <Badge key={scope} variant="outline" title={scope}>
                                            {scopeLabel(scope)}
                                        </Badge>
                                    ))}
                                </div>
                            ) : (
                                <p className="text-sm text-muted-foreground">
                                    Google reported no scopes for this grant.
                                </p>
                            )}
                        </div>

                        <div className="flex flex-wrap gap-2 pt-1">
                            <Button
                                variant="outline"
                                onClick={onConnect}
                                disabled={connecting || !configured}
                            >
                                {connecting ? (
                                    <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                    <Link2 className="h-4 w-4" />
                                )}
                                Review permissions
                            </Button>
                            <Button
                                variant="ghost"
                                onClick={onDisconnect}
                                disabled={disconnecting}
                                className="text-destructive hover:text-destructive/90"
                            >
                                {disconnecting ? (
                                    <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                    <Unlink className="h-4 w-4" />
                                )}
                                Disconnect
                            </Button>
                        </div>
                    </>
                ) : (
                    <>
                        {revoked && (
                            <div className="flex gap-2 rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive">
                                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                                <div>
                                    <p className="font-medium">Google revoked this connection.</p>
                                    <p className="text-destructive/90">
                                        {status?.revoked_reason
                                            ? `Reason reported by Google: ${status.revoked_reason}.`
                                            : "Renewing the token cannot recover it."}{" "}
                                        Reconnect to grant access again.
                                    </p>
                                </div>
                            </div>
                        )}

                        <p className="text-sm text-muted-foreground">
                            Connect a Google account to let your agents read the spreadsheets,
                            documents and calendars you point them at. You choose what to grant
                            on Google&apos;s own consent screen, and you can disconnect at any
                            time.
                        </p>

                        <Button onClick={onConnect} disabled={connecting || !configured}>
                            {connecting ? (
                                <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                                <Link2 className="h-4 w-4" />
                            )}
                            {revoked ? "Reconnect Google" : "Connect Google"}
                        </Button>
                    </>
                )}
            </CardContent>
        </Card>
    );
}

export default GoogleConnectionCard;
