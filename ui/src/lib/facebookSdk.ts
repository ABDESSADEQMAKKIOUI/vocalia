/**
 * Facebook JavaScript SDK loader + WhatsApp Embedded Signup launcher.
 *
 * The Embedded Signup flow (Meta's hosted "Connect a WhatsApp account" popup)
 * needs the Facebook SDK loaded from connect.facebook.net and an FB.login()
 * call configured with the app's Login Configuration id. Meta posts the
 * selected WABA / phone-number ids back to the opener via window.postMessage
 * ("WA_EMBEDDED_SIGNUP"), while FB.login's callback returns the auth `code`.
 * We correlate both and hand them to the backend for token exchange.
 *
 * This module is browser-only: every DOM/window access happens inside a
 * function body (never at import time), so it stays safe to import from a
 * client component that may be evaluated during SSR.
 */

// ---------------------------------------------------------------------------
// Minimal Facebook SDK typings (avoids a dependency on @types/facebook-js-sdk)
// ---------------------------------------------------------------------------

interface FBInitOptions {
    appId: string;
    cookie?: boolean;
    xfbml?: boolean;
    version: string;
}

interface FBAuthResponse {
    code?: string;
    accessToken?: string;
    [key: string]: unknown;
}

interface FBLoginStatusResponse {
    status?: string;
    authResponse?: FBAuthResponse | null;
}

interface FBLoginOptions {
    config_id?: string;
    response_type?: string;
    override_default_response_type?: boolean;
    scope?: string;
    extras?: Record<string, unknown>;
}

interface FacebookSDK {
    init(options: FBInitOptions): void;
    login(
        callback: (response: FBLoginStatusResponse) => void,
        options?: FBLoginOptions,
    ): void;
}

declare global {
    interface Window {
        FB?: FacebookSDK;
        fbAsyncInit?: () => void;
    }
}

// ---------------------------------------------------------------------------
// SDK loader
// ---------------------------------------------------------------------------

const SDK_SCRIPT_ID = "facebook-jssdk";
const SDK_SRC = "https://connect.facebook.net/en_US/sdk.js";

/**
 * Cached across calls so the script is injected and FB.init() runs at most
 * once. A rejected attempt (script failed to load) clears the cache so a later
 * retry can start fresh.
 */
let sdkPromise: Promise<void> | null = null;

/**
 * Inject the Facebook SDK (once) and initialize it. Idempotent: concurrent or
 * repeated calls share a single promise. Resolves when `window.FB` is ready.
 */
export function loadFacebookSdk(appId: string, graphVersion: string): Promise<void> {
    if (typeof window === "undefined" || typeof document === "undefined") {
        return Promise.reject(
            new Error("Le SDK Facebook ne peut être chargé que dans le navigateur."),
        );
    }
    if (sdkPromise) {
        return sdkPromise;
    }

    sdkPromise = new Promise<void>((resolve, reject) => {
        const init = () => {
            const fb = window.FB;
            if (!fb) {
                sdkPromise = null;
                reject(new Error("Le SDK Facebook n'a pas pu s'initialiser."));
                return;
            }
            fb.init({
                appId,
                cookie: true,
                xfbml: false,
                version: graphVersion,
            });
            resolve();
        };

        // Already available (loaded by a previous call or another feature):
        // initialize immediately.
        if (window.FB) {
            init();
            return;
        }

        // The SDK calls fbAsyncInit once window.FB is ready — set it before the
        // script can finish loading.
        window.fbAsyncInit = init;

        // Script tag present but FB not ready yet: fbAsyncInit will fire.
        if (document.getElementById(SDK_SCRIPT_ID)) {
            return;
        }

        const script = document.createElement("script");
        script.id = SDK_SCRIPT_ID;
        script.src = SDK_SRC;
        script.async = true;
        script.defer = true;
        script.onerror = () => {
            sdkPromise = null;
            reject(new Error("Échec du chargement du SDK Facebook."));
        };
        document.body.appendChild(script);
    });

    return sdkPromise;
}

// ---------------------------------------------------------------------------
// Embedded Signup launcher
// ---------------------------------------------------------------------------

export interface EmbeddedSignupResult {
    code: string;
    waba_id?: string;
    phone_number_id?: string;
    business_id?: string;
}

/**
 * Open Meta's Embedded Signup popup and resolve with the auth `code` plus the
 * WABA / phone-number / business ids Meta posts back. Rejects if the user
 * cancels (no code) or the SDK is not ready.
 */
export function launchEmbeddedSignup(configId: string): Promise<EmbeddedSignupResult> {
    return new Promise<EmbeddedSignupResult>((resolve, reject) => {
        if (typeof window === "undefined" || !window.FB) {
            reject(new Error("Le SDK Facebook n'est pas prêt."));
            return;
        }
        const fb = window.FB;

        // Captured from the WA_EMBEDDED_SIGNUP postMessage, which Meta sends
        // before FB.login's callback fires with the code.
        const sessionInfo: {
            waba_id?: string;
            phone_number_id?: string;
            business_id?: string;
        } = {};

        const onMessage = (event: MessageEvent) => {
            if (
                typeof event.origin !== "string" ||
                !event.origin.endsWith("facebook.com")
            ) {
                return;
            }
            try {
                const payload =
                    typeof event.data === "string"
                        ? JSON.parse(event.data)
                        : event.data;
                if (
                    payload &&
                    payload.type === "WA_EMBEDDED_SIGNUP" &&
                    payload.data &&
                    typeof payload.data === "object"
                ) {
                    const data = payload.data as Record<string, unknown>;
                    if (typeof data.phone_number_id === "string") {
                        sessionInfo.phone_number_id = data.phone_number_id;
                    }
                    if (typeof data.waba_id === "string") {
                        sessionInfo.waba_id = data.waba_id;
                    }
                    if (typeof data.business_id === "string") {
                        sessionInfo.business_id = data.business_id;
                    }
                }
            } catch {
                // Non-JSON message from a facebook.com frame: ignore.
            }
        };

        window.addEventListener("message", onMessage);
        const cleanup = () => window.removeEventListener("message", onMessage);

        fb.login(
            (response) => {
                cleanup();
                const code = response?.authResponse?.code;
                if (!code) {
                    reject(new Error("Connexion annulée."));
                    return;
                }
                resolve({ code, ...sessionInfo });
            },
            {
                config_id: configId,
                response_type: "code",
                override_default_response_type: true,
                extras: { setup: {}, sessionInfoVersion: "3" },
            },
        );
    });
}
