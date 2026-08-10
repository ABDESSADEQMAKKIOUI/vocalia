"use client";

import { AlertTriangle, CheckCircle2, X } from "lucide-react";

import { Button } from "@/components/ui/button";

export interface OAuthReturn {
    /** 'connected' or 'error', as the callback redirect spelled it. */
    status: string;
    /** Short error code; already reduced to [a-z0-9_.-] by the backend. */
    reason?: string;
}

/**
 * Wording for the codes the OAuth callback can redirect back with.
 *
 * The reason is a *code*, not a sentence: the backend squashes it precisely so
 * that a value partly chosen by whoever called the callback cannot become free
 * text on this page. Anything unrecognised is shown as the bare code rather
 * than guessed at.
 */
/**
 * The callback forwards the integration error's own `code` as `reason`, so this
 * map has to track the codes `api/services/integrations/google/oauth.py`
 * actually raises — plus `access_denied` and `missing_code`, which come from
 * Google itself rather than from our code. A reason with no entry here is shown
 * verbatim rather than guessed at, so an unmapped code degrades to something
 * true instead of something invented.
 */
const REASON_MESSAGES: Record<string, string> = {
    // From Google's redirect.
    access_denied: "You declined the permissions on Google's consent screen.",
    missing_code: "Google did not return an authorization code.",
    // Raised by the backend during the callback.
    invalid_state: "The connection request could not be verified. Start again from this page.",
    expired_state: "The connection request expired. Start again from this page.",
    not_configured: "Google is not configured on this deployment.",
    network_error: "Volira could not reach Google to finish the connection. Try again.",
    malformed_token_response: "Google's response could not be read. Try connecting again.",
    missing_refresh_token:
        "Google did not issue a long-lived token. Remove Volira from your Google account's third-party access, then connect again.",
    storage_failed: "The connection could not be saved. Try again, and contact support if it repeats.",
    credential_name_conflict:
        "Another Google connection already exists for this organization. Disconnect it first.",
    connection_revoked: "This Google connection was revoked. Connect again to restore it.",
};

export function OAuthReturnBanner({
    result,
    onDismiss,
}: {
    result: OAuthReturn;
    onDismiss: () => void;
}) {
    const connected = result.status === "connected";
    const message = connected
        ? "Your Google account is connected. Your resources are listed below."
        : (result.reason && REASON_MESSAGES[result.reason]) ||
          (result.reason
              ? `Google reported an error (${result.reason}).`
              : "The Google connection did not complete.");

    return (
        <div
            role="status"
            className={
                connected
                    ? "flex items-start gap-3 rounded-lg border border-primary/20 bg-primary/10 p-3 text-sm"
                    : "flex items-start gap-3 rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive"
            }
        >
            {connected ? (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
            ) : (
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            )}
            <p className="flex-1">{message}</p>
            <Button
                variant="ghost"
                size="sm"
                onClick={onDismiss}
                className="h-6 w-6 p-0"
                aria-label="Dismiss"
            >
                <X className="h-4 w-4" />
            </Button>
        </div>
    );
}

export default OAuthReturnBanner;
