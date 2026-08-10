import os
from pathlib import Path

from api.enums import Environment

ENVIRONMENT = os.getenv("ENVIRONMENT", Environment.LOCAL.value)
# Absolute path to the project root directory (i.e. the directory containing
# the top-level api/ package). Having a single canonical location helps
# when constructing file-system paths elsewhere in the codebase.
APP_ROOT_DIR: Path = Path(__file__).resolve().parent

FILLER_SOUND_PROBABILITY = 0.0

VOICEMAIL_RECORDING_DURATION = 5.0

# Langfuse Configuration
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")

# URLs for deployment
#
# PUBLIC_BASE_URL is the single canonical origin a deployment is reached at
# (scheme + host, e.g. https://203-0-113-10.sslip.io). For a standard single-host
# install it is the only endpoint value an operator sets — the per-subsystem URLs
# below derive from it (and from PUBLIC_HOST for the TURN/ICE host). Each derived
# var can still be set explicitly to override it for a split deployment.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or None
PUBLIC_HOST = os.getenv("PUBLIC_HOST") or None

# Public URL the backend builds webhook/callback/embed links from. Derives from
# PUBLIC_BASE_URL (public IP / domain), falling back to localhost for local dev.
# When this is a non-public address (localhost or a private/reserved IP) the host
# isn't reachable from the internet, so get_backend_endpoints() resolves a running
# Cloudflare tunnel's URL at runtime instead (see api/utils/common.py).
BACKEND_API_ENDPOINT = (
    os.getenv("BACKEND_API_ENDPOINT") or PUBLIC_BASE_URL or "http://localhost:8000"
)
# Public URL the backend redirects a browser back to after an external consent
# flow (Google OAuth today). Derives from PUBLIC_BASE_URL like the endpoint
# above: on a single-host install the UI and the API share one origin, so an
# operator who set PUBLIC_BASE_URL has already said where the browser should
# land. Left as a bare localhost default it silently sent every customer
# returning from Google's consent screen to a dead page on their own machine.
# Set UI_APP_URL explicitly only for a split deployment (UI on another domain).
UI_APP_URL = os.getenv("UI_APP_URL") or PUBLIC_BASE_URL or "http://localhost:3010"

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]

DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "oss")
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
AUTH_PROVIDER = os.getenv("AUTH_PROVIDER", "local")
ENABLE_SIGNUP = os.getenv("ENABLE_SIGNUP", "true").lower() == "true"
# Stack Auth public client config. These are safe to expose to the browser (the
# publishable client key is public by design, and the project id is non-sensitive),
# and are served to the UI at runtime via /api/v1/health so the frontend no longer
# needs them baked into the bundle at build time.
STACK_AUTH_PROJECT_ID = os.getenv("STACK_AUTH_PROJECT_ID")
STACK_PUBLISHABLE_CLIENT_KEY = os.getenv("STACK_PUBLISHABLE_CLIENT_KEY")
DOGRAH_MPS_SECRET_KEY = os.getenv("DOGRAH_MPS_SECRET_KEY", None)
MPS_API_URL = os.getenv("MPS_API_URL", "https://services.dograh.com")
DOGRAH_DEVOPS_SECRET = os.getenv("DOGRAH_DEVOPS_SECRET") or None

# Gates the SaaS subscription quota enforcement. When false, every check in
# api/services/subscription/enforcement.py short-circuits to allow.
SUBSCRIPTION_ENFORCEMENT_ENABLED = (
    os.getenv("SUBSCRIPTION_ENFORCEMENT_ENABLED", "true").lower() == "true"
)

# Storage Configuration
ENABLE_AWS_S3 = os.getenv("ENABLE_AWS_S3", "false").lower() == "true"

# MinIO Configuration
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
# Full URL (scheme + host) browsers use to reach object storage. Derives from
# PUBLIC_BASE_URL (remote nginx proxies /voice-audio/ to MinIO); set explicitly
# only to point object storage at a separate origin.
MINIO_PUBLIC_ENDPOINT = (
    os.getenv("MINIO_PUBLIC_ENDPOINT") or PUBLIC_BASE_URL or "http://localhost:9000"
)
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "voice-audio")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

# AWS S3 Configuration
S3_BUCKET = os.environ.get("S3_BUCKET")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
# Optional overrides for S3-compatible backends (e.g. MinIO, rustfs, Ceph).
# S3_ENDPOINT_URL: full URL of a custom S3 endpoint (e.g. "https://s3.example.com").
#   Leave unset to use AWS's default endpoint resolution.
# S3_SIGNATURE_VERSION: botocore signature version used to sign requests and
#   presigned URLs. Defaults to None (botocore's default, currently SigV2 for
#   presigned URLs). Set to "s3v4" for S3-compatible servers that require SigV4.
# S3_ADDRESSING_STYLE: "auto" (default), "path", or "virtual". Many S3-compatible
#   servers and TLS setups require "path".
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")
S3_SIGNATURE_VERSION = os.environ.get("S3_SIGNATURE_VERSION")
S3_ADDRESSING_STYLE = os.environ.get("S3_ADDRESSING_STYLE")

# Sentry configuration
SENTRY_DSN = os.getenv("SENTRY_DSN")

# PostHog configuration
POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")


ENABLE_ARI_STASIS = os.getenv("ENABLE_ARI_STASIS", "false").lower() == "true"
SERIALIZE_LOG_OUTPUT = os.getenv("SERIALIZE_LOG_OUTPUT", "false").lower() == "true"

# Logging configuration
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", None)
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()

# Log rotation configuration
LOG_ROTATION_SIZE = os.getenv("LOG_ROTATION_SIZE", "100 MB")
LOG_RETENTION = os.getenv("LOG_RETENTION", "7 days")
LOG_COMPRESSION = os.getenv("LOG_COMPRESSION", "gz")
ENABLE_TELEMETRY = os.getenv("ENABLE_TELEMETRY", "true").lower() == "true"


def _get_version() -> str:
    """Read version from pyproject.toml."""
    try:
        import tomllib

        pyproject_path = APP_ROOT_DIR / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)
        return pyproject.get("project", {}).get("version", "dev")
    except Exception:
        return "dev"


# Application version (read from pyproject.toml)
APP_VERSION = _get_version()

# Country code mapping: ISO country code -> international dialing prefix
COUNTRY_CODES = {
    "US": "1",  # United States
    "CA": "1",  # Canada
    "GB": "44",  # United Kingdom
    "IN": "91",  # India
    "AU": "61",  # Australia
    "DE": "49",  # Germany
    "FR": "33",  # France
    "BR": "55",  # Brazil
    "MX": "52",  # Mexico
    "IT": "39",  # Italy
    "ES": "34",  # Spain
    "NL": "31",  # Netherlands
    "SE": "46",  # Sweden
    "NO": "47",  # Norway
    "DK": "45",  # Denmark
    "FI": "358",  # Finland
    "CH": "41",  # Switzerland
    "AT": "43",  # Austria
    "BE": "32",  # Belgium
    "LU": "352",  # Luxembourg
    "IE": "353",  # Ireland
}

# Floor at 1 so a misconfigured env var (0 or negative) can't silently block
# every call in the deployment.
DEFAULT_ORG_CONCURRENCY_LIMIT = max(
    1, int(os.getenv("DEFAULT_ORG_CONCURRENCY_LIMIT", "10"))
)
DEFAULT_CAMPAIGN_RETRY_CONFIG = {
    "enabled": True,
    "max_retries": 1,
    "retry_delay_seconds": 120,
    "retry_on_busy": True,
    "retry_on_no_answer": True,
    "retry_on_voicemail": False,
}


# Outbound webhook delivery: bounded retry with exponential backoff.
# Delivery is persisted (see WebhookDeliveryModel) and retried by an ARQ task so a
# transient network error can't permanently drop a final webhook. After
# ``max_attempts`` transient failures the delivery is parked as ``dead_letter``.
DEFAULT_WEBHOOK_DELIVERY_CONFIG = {
    "max_attempts": int(os.getenv("WEBHOOK_DELIVERY_MAX_ATTEMPTS", 5)),
    "base_delay_seconds": int(os.getenv("WEBHOOK_DELIVERY_BASE_DELAY_SECONDS", 30)),
    "max_delay_seconds": int(os.getenv("WEBHOOK_DELIVERY_MAX_DELAY_SECONDS", 600)),
    "timeout_seconds": int(os.getenv("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", 30)),
}


# Circuit breaker defaults for campaign call failure detection
DEFAULT_CIRCUIT_BREAKER_CONFIG = {
    "enabled": True,
    "failure_threshold": 0.5,  # 50% failure rate trips the breaker
    "window_seconds": 120,  # 2-minute sliding window
    "min_calls_in_window": 5,  # Don't trip until at least 5 outcomes
}


TURN_SECRET = os.getenv("TURN_SECRET")
# Host browsers dial for TURN/ICE. Derives from PUBLIC_HOST; set explicitly only
# when the TURN server runs on a separate host from the app.
TURN_HOST = os.getenv("TURN_HOST") or PUBLIC_HOST or "localhost"
TURN_PORT = int(os.getenv("TURN_PORT", "3478"))
TURN_TLS_PORT = int(os.getenv("TURN_TLS_PORT", "5349"))
TURN_CREDENTIAL_TTL = int(os.getenv("TURN_CREDENTIAL_TTL", "86400"))
# Diagnostic flag: when true, strip all non-relay ICE candidates from the
# answer SDP so every media path must traverse the TURN server. Use for
# verifying TURN connectivity end-to-end; expect connection failures if
# TURN is misconfigured or unreachable.
FORCE_TURN_RELAY = os.getenv("FORCE_TURN_RELAY", "false").lower() == "true"

# OSS Email/Password Auth
OSS_JWT_SECRET = os.getenv("OSS_JWT_SECRET", "change-me-in-production")
OSS_JWT_EXPIRY_HOURS = int(os.getenv("OSS_JWT_EXPIRY_HOURS", "720"))  # 30 days

TUNER_BASE_URL = os.getenv("TUNER_BASE_URL", "https://api.usetuner.ai")

# WhatsApp Cloud API (Meta) — Embedded Signup + webhook signature verification.
#
# WHATSAPP_APP_ID and WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID drive the Facebook
# Login popup the UI opens for Embedded Signup; both must be set for Embedded
# Signup to be offered (see services/messaging/whatsapp/embedded_signup.py).
# WHATSAPP_APP_SECRET is used both to exchange the popup's OAuth code for a
# long-lived token and by the webhook route to verify Meta's
# X-Hub-Signature-256. WHATSAPP_GRAPH_VERSION pins the Graph API version.
#
# These are read live from os.environ where they are used so a running
# deployment reflects env changes without a restart (matching the webhook
# route); the values here document the deployment configuration surface.
WHATSAPP_APP_ID = os.getenv("WHATSAPP_APP_ID") or None
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET") or None
WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID = (
    os.getenv("WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID") or None
)
WHATSAPP_GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v23.0")

# Google OAuth2 + Sheets API — see services/integrations/google/.
#
# One Google Cloud "Web application" OAuth client backs the whole deployment;
# every organization consents separately and its tokens are stored per
# organization in ExternalCredentialModel (encrypted at rest).
#
# GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET come from the Google Cloud
# console (APIs & Services -> Credentials). GOOGLE_OAUTH_REDIRECT_URI must be
# registered verbatim as an authorized redirect URI on that client, and the same
# value must be replayed on the token exchange; it defaults to the backend's own
# callback path.
#
# GOOGLE_PICKER_API_KEY / GOOGLE_PICKER_APP_ID are browser-safe values the UI
# needs to open the Google Picker. The Picker is not optional: the drive.file
# scope only grants access to files the user explicitly picks (see the scope
# rationale in services/integrations/google/oauth.py).
#
# Secrets are read live from os.environ where they are used (matching the
# WhatsApp block above) so a running deployment reflects env changes without a
# restart; the values here document the deployment configuration surface.
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID") or None
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or None
GOOGLE_OAUTH_REDIRECT_URI = (
    os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
    or f"{BACKEND_API_ENDPOINT}/api/v1/integrations/google/oauth/callback"
)
GOOGLE_PICKER_API_KEY = os.getenv("GOOGLE_PICKER_API_KEY") or None
GOOGLE_PICKER_APP_ID = os.getenv("GOOGLE_PICKER_APP_ID") or None

GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# drive.file and spreadsheets are both NON-SENSITIVE scopes: no Google app
# verification, no paid annual security assessment (CASA). That is why they are
# the default, and why widening the set is an explicit, per-deployment decision
# rather than a code change:
#
#   - drive.readonly, gmail.readonly / gmail.modify  -> RESTRICTED: verification
#     plus a recurring, paid third-party security assessment, gating the whole
#     OAuth client (every organization already connected included).
#   - calendar.*, documents, gmail.send              -> SENSITIVE: verification
#     (~10 days) but no annual assessment.
#
# GOOGLE_OAUTH_SCOPES accepts a comma- or whitespace-separated list, so an
# operator who has passed Google's review for a wider set can request it without
# forking the code — and a deployment that has not stays non-sensitive by
# default. The classification of each scope lives in
# services/integrations/google/scopes.py, which also decides what a granted
# scope actually covers.
GOOGLE_OAUTH_DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
)


def _parse_google_oauth_scopes(raw: str | None) -> tuple[str, ...]:
    """Parse GOOGLE_OAUTH_SCOPES, de-duplicated and order-preserving.

    An empty or blank value keeps the non-sensitive default: a deployment never
    silently ends up requesting nothing, which Google answers with an error at
    the consent screen rather than at startup.
    """
    if not raw or not raw.strip():
        return GOOGLE_OAUTH_DEFAULT_SCOPES
    parsed = dict.fromkeys(
        chunk for chunk in raw.replace(",", " ").split() if chunk.strip()
    )
    return tuple(parsed) or GOOGLE_OAUTH_DEFAULT_SCOPES


GOOGLE_OAUTH_SCOPES = _parse_google_oauth_scopes(os.getenv("GOOGLE_OAUTH_SCOPES"))

# How long a signed OAuth `state` stays acceptable at the callback. Long enough
# for a real consent screen (account chooser + review), short enough that a
# leaked authorization URL goes stale quickly.
GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS = int(
    os.getenv("GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS", "900")
)
# Refresh an access token that expires within this window instead of letting a
# live call discover the expiry mid-request.
GOOGLE_TOKEN_REFRESH_LEEWAY_SECONDS = int(
    os.getenv("GOOGLE_TOKEN_REFRESH_LEEWAY_SECONDS", "300")
)
# Display name of the single per-organization Google connection row stored in
# external_credentials (the table has a UNIQUE(organization_id, name)).
GOOGLE_CREDENTIAL_NAME = "Google Sheets"

# Base URLs the API clients in services/integrations/google/ prepend to their
# paths. Each one already carries the version segment, and the segment that
# addresses the collection is part of the path the client passes:
#
#   Sheets    f"{GOOGLE_SHEETS_API_BASE_URL}/{spreadsheet_id}"
#   Drive     f"{GOOGLE_DRIVE_API_BASE_URL}/files"
#   Calendar  f"{GOOGLE_CALENDAR_API_BASE_URL}/users/me/calendarList"
#   Gmail     f"{GOOGLE_GMAIL_API_BASE_URL}/users/me/labels"
#   Docs      f"{GOOGLE_DOCS_API_BASE_URL}/documents/{document_id}"
GOOGLE_SHEETS_API_BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"
GOOGLE_DRIVE_API_BASE_URL = "https://www.googleapis.com/drive/v3"
GOOGLE_CALENDAR_API_BASE_URL = "https://www.googleapis.com/calendar/v3"
GOOGLE_GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_DOCS_API_BASE_URL = "https://docs.googleapis.com/v1"

GOOGLE_API_TIMEOUT_SECONDS = float(os.getenv("GOOGLE_API_TIMEOUT_SECONDS", "30"))
# Sheets quota is per user, per project, per minute (429 on burst), so retries
# are bounded and backed off rather than tight-looped.
GOOGLE_API_MAX_ATTEMPTS = max(1, int(os.getenv("GOOGLE_API_MAX_ATTEMPTS", "4")))
GOOGLE_API_RETRY_BASE_DELAY_SECONDS = 0.5
GOOGLE_API_RETRY_MAX_DELAY_SECONDS = 8.0

# Discovery listings (the "what does this account have?" screen) are bounded:
# they exist so an operator can pick a resource, not so the backend mirrors a
# Drive. A page of 100 covers every realistic picker without paging.
GOOGLE_DISCOVERY_PAGE_SIZE = max(
    1, min(1000, int(os.getenv("GOOGLE_DISCOVERY_PAGE_SIZE", "100")))
)
GOOGLE_DISCOVERY_RECENT_FILES = max(
    1, int(os.getenv("GOOGLE_DISCOVERY_RECENT_FILES", "10"))
)
GOOGLE_DISCOVERY_RECENT_MESSAGES = max(
    1, int(os.getenv("GOOGLE_DISCOVERY_RECENT_MESSAGES", "5"))
)
