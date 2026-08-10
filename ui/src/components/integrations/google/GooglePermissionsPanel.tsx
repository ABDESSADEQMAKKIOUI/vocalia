"use client";

import { Check, Minus, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { GoogleRequiredScopes } from "@/lib/googleWorkspace";

import { FAMILY_ORDER, familyLabel, scopeLabel, sensitivityLabel } from "./scopeLabels";

interface GooglePermissionsPanelProps {
    requiredScopes: GoogleRequiredScopes | null;
    loading: boolean;
    connected: boolean;
    /**
     * Family → scopes the current grant is still missing, as the overview
     * reported them. Undefined when there is no connection to compare against.
     * It comes from the server rather than being recomputed here because a
     * broad scope implies narrower ones (`drive` covers `drive.readonly`), and
     * a set-membership test in the browser would flag covered scopes as
     * missing.
     */
    missingByFamily?: Record<string, string[]>;
}

/**
 * What this deployment asks Google for, family by family, and what the current
 * grant actually covers.
 *
 * The list is server-supplied on purpose: a family whose scopes are absent from
 * `requested_scopes` cannot be fixed by re-consenting — an operator has to
 * widen `GOOGLE_OAUTH_SCOPES` first — and only the server knows that.
 */
export function GooglePermissionsPanel({
    requiredScopes,
    loading,
    connected,
    missingByFamily,
}: GooglePermissionsPanelProps) {
    if (loading) {
        return (
            <Card>
                <CardHeader>
                    <CardTitle>Permissions</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                    {[0, 1, 2].map((row) => (
                        <Skeleton key={row} className="h-14 w-full" />
                    ))}
                </CardContent>
            </Card>
        );
    }

    if (!requiredScopes) {
        return null;
    }

    // scope_details carries the server's own verdict per scope, computed with
    // the implication table (a deployment asking for "drive" IS asking for
    // "drive.readonly"). Re-deriving it here from requested_scopes with a set
    // lookup is the exact browser-side membership test this file's own comment
    // warns against, and it reports covered scopes as not requested.
    const requestedByScope = new Map(
        requiredScopes.scope_details.map((detail) => [detail.scope, detail.requested]),
    );
    const isRequested = (scope: string) => requestedByScope.get(scope) ?? false;

    return (
        <Card>
            <CardHeader>
                <CardTitle>Permissions</CardTitle>
                <CardDescription>
                    What each Google service needs. You grant these on Google&apos;s consent
                    screen — this deployment cannot add any others.
                </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
                {FAMILY_ORDER.map((family) => {
                    const scopes = requiredScopes.families[family] ?? [];
                    if (scopes.length === 0) return null;

                    const missing = missingByFamily?.[family];
                    const covered = connected && missing !== undefined && missing.length === 0;
                    const notRequested = scopes.filter((scope) => !isRequested(scope));

                    return (
                        <div key={family} className="space-y-2">
                            <div className="flex items-center justify-between gap-2">
                                <span className="font-medium">{familyLabel(family)}</span>
                                {covered ? (
                                    <span className="flex items-center gap-1 text-xs text-primary">
                                        <Check className="h-3.5 w-3.5" />
                                        Granted
                                    </span>
                                ) : connected && missing !== undefined ? (
                                    <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                        <X className="h-3.5 w-3.5" />
                                        Not granted
                                    </span>
                                ) : (
                                    <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                        <Minus className="h-3.5 w-3.5" />
                                        Not connected
                                    </span>
                                )}
                            </div>
                            <ul className="space-y-1">
                                {scopes.map((scope) => (
                                    <li
                                        key={scope}
                                        className="text-sm text-muted-foreground"
                                        title={scope}
                                    >
                                        {scopeLabel(scope)}
                                    </li>
                                ))}
                            </ul>
                            {notRequested.length > 0 && (
                                <p className="text-xs text-muted-foreground">
                                    This deployment does not request{" "}
                                    {notRequested.map(scopeLabel).join(", ")}. Reconnecting will
                                    not add it — an operator has to widen the configured scopes.
                                </p>
                            )}
                        </div>
                    );
                })}

                <div className="border-t border-border pt-3">
                    <Badge variant="outline">
                        Google classification:{" "}
                        {sensitivityLabel(requiredScopes.deployment_sensitivity)}
                    </Badge>
                </div>
            </CardContent>
        </Card>
    );
}

export default GooglePermissionsPanel;
