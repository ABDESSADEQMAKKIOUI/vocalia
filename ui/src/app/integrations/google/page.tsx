"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { GoogleConnectionCard } from "@/components/integrations/google/GoogleConnectionCard";
import { GooglePermissionsPanel } from "@/components/integrations/google/GooglePermissionsPanel";
import type { OAuthReturn } from "@/components/integrations/google/OAuthReturnBanner";
import { OAuthReturnBanner } from "@/components/integrations/google/OAuthReturnBanner";
import { WorkspaceResources } from "@/components/integrations/google/WorkspaceResources";
import { useAuth } from "@/lib/auth";
import type {
    GoogleConnectionStatus,
    GoogleRequiredScopes,
    WorkspaceOverview,
} from "@/lib/googleWorkspace";
import {
    disconnectGoogle,
    fetchGoogleRequiredScopes,
    fetchGoogleStatus,
    fetchWorkspaceOverview,
    googleErrorMessage,
    startGoogleAuthorization,
} from "@/lib/googleWorkspace";
import logger from "@/lib/logger";

/**
 * The page the OAuth callback lands on.
 *
 * `UI_RETURN_PATH` in `api/routes/integrations_google.py` points the browser
 * here with `?google_status=connected|error&reason=<code>` — the callback has
 * no response body the UI can read, so the outcome travels in the query string.
 * The parameters are consumed once and stripped with `replaceState`, otherwise
 * a refresh replays a stale "connected!" banner over a connection that has since
 * been revoked.
 */
export default function GoogleIntegrationPage() {
    const { user, loading: authLoading, redirectToLogin } = useAuth();
    const hasFetched = useRef(false);

    const [status, setStatus] = useState<GoogleConnectionStatus | null>(null);
    const [requiredScopes, setRequiredScopes] = useState<GoogleRequiredScopes | null>(null);
    const [overview, setOverview] = useState<WorkspaceOverview | null>(null);

    const [loading, setLoading] = useState(true);
    const [overviewLoading, setOverviewLoading] = useState(false);
    const [connecting, setConnecting] = useState(false);
    const [disconnecting, setDisconnecting] = useState(false);

    const [error, setError] = useState<string | null>(null);
    const [overviewError, setOverviewError] = useState<string | null>(null);
    const [oauthReturn, setOauthReturn] = useState<OAuthReturn | null>(null);

    useEffect(() => {
        if (!authLoading && !user) {
            redirectToLogin();
        }
    }, [authLoading, user, redirectToLogin]);

    // Read the callback outcome straight off the URL rather than through
    // useSearchParams: the value is needed exactly once, and reading it here
    // keeps the page out of the Suspense boundary that hook would demand.
    useEffect(() => {
        const params = new URLSearchParams(window.location.search);
        const googleStatus = params.get("google_status");
        if (!googleStatus) return;

        setOauthReturn({ status: googleStatus, reason: params.get("reason") ?? undefined });

        params.delete("google_status");
        params.delete("reason");
        const query = params.toString();
        window.history.replaceState(
            null,
            "",
            `${window.location.pathname}${query ? `?${query}` : ""}`,
        );
    }, []);

    const loadOverview = useCallback(async () => {
        setOverviewLoading(true);
        setOverviewError(null);
        try {
            setOverview(await fetchWorkspaceOverview());
        } catch (err) {
            logger.error(`[GoogleIntegrationPage] overview failed: ${String(err)}`);
            setOverview(null);
            setOverviewError(googleErrorMessage(err, "Failed to load your Google workspace"));
        } finally {
            setOverviewLoading(false);
        }
    }, []);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            // The scope registry is public and static; losing it costs the
            // permissions panel, not the page, so it must not fail the load.
            const [nextStatus, scopes] = await Promise.all([
                fetchGoogleStatus(),
                fetchGoogleRequiredScopes().catch(() => null),
            ]);
            setStatus(nextStatus);
            setRequiredScopes(scopes);

            if (nextStatus.connected) {
                await loadOverview();
            } else {
                setOverview(null);
                setOverviewError(null);
            }
        } catch (err) {
            logger.error(`[GoogleIntegrationPage] status failed: ${String(err)}`);
            setError(googleErrorMessage(err, "Failed to read the Google connection status"));
        } finally {
            setLoading(false);
        }
    }, [loadOverview]);

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        void load();
    }, [authLoading, user, load]);

    const handleConnect = useCallback(async () => {
        setConnecting(true);
        setError(null);
        try {
            const authorization = await startGoogleAuthorization();
            // Leaving the app: `connecting` is intentionally not reset — the
            // browser is on its way to Google's consent screen.
            window.location.href = authorization.authorization_url;
        } catch (err) {
            setConnecting(false);
            setError(googleErrorMessage(err, "Failed to start the Google connection"));
        }
    }, []);

    const handleDisconnect = useCallback(async () => {
        setDisconnecting(true);
        setError(null);
        try {
            await disconnectGoogle();
            setOverview(null);
            setOverviewError(null);
            await load();
        } catch (err) {
            setError(googleErrorMessage(err, "Failed to disconnect the Google account"));
        } finally {
            setDisconnecting(false);
        }
    }, [load]);

    if (authLoading || !user) {
        return null;
    }

    // Which scopes each family still lacks, straight from the server's own
    // comparison — the browser cannot recompute it, because a broad scope
    // implies narrower ones.
    const missingByFamily = overview
        ? {
              sheets: overview.sheets.missing_scopes,
              calendar: overview.calendars.missing_scopes,
              drive: overview.drive.missing_scopes,
              gmail: overview.gmail.missing_scopes,
              docs: overview.docs.missing_scopes,
          }
        : undefined;

    const connected = Boolean(status?.connected);

    return (
        <div className="container mx-auto px-4 py-8">
            <div className="mx-auto max-w-6xl space-y-6">
                <div>
                    <h1 className="mb-2 text-3xl font-bold">Google Workspace</h1>
                    <p className="text-muted-foreground">
                        Connect a Google account so your agents can work with your
                        spreadsheets, documents, calendars and mailbox.
                    </p>
                </div>

                {oauthReturn && (
                    <OAuthReturnBanner
                        result={oauthReturn}
                        onDismiss={() => setOauthReturn(null)}
                    />
                )}

                <div className="grid gap-6 lg:grid-cols-12">
                    <div className="space-y-6 lg:col-span-7">
                        <GoogleConnectionCard
                            status={status}
                            loading={loading}
                            connecting={connecting}
                            disconnecting={disconnecting}
                            error={error}
                            onConnect={handleConnect}
                            onDisconnect={handleDisconnect}
                        />
                    </div>
                    <div className="lg:col-span-5">
                        <GooglePermissionsPanel
                            requiredScopes={requiredScopes}
                            loading={loading}
                            connected={connected}
                            missingByFamily={missingByFamily}
                        />
                    </div>
                </div>

                {connected && (
                    <WorkspaceResources
                        overview={overview}
                        loading={overviewLoading}
                        error={overviewError}
                        onReconnect={handleConnect}
                        onRetry={loadOverview}
                    />
                )}
            </div>
        </div>
    );
}
