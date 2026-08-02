"""Google integration package — OAuth2 + Sheets API foundation.

This package holds the plumbing every Google-backed feature needs:

- ``oauth`` — the per-organization OAuth2 grant: consent URL, code exchange,
  refresh with revocation handling, and encrypted token storage in
  ``ExternalCredentialModel``.
- ``sheets_client`` — a thin async Sheets API v4 client (read a range, read the
  header row, append a row, update cells of one row, find a row, describe the
  spreadsheet).

Nothing here is workflow-aware on purpose. The feature that infers what each
column of a bound sheet means, caches that inference against a fingerprint of
the header row, and turns it into named LLM function parameters builds on top of
these two modules — it does not belong in them.

Self-registers on import via ``register_package`` and is auto-discovered by
``api/services/integrations/loader.py``. The spec carries no nodes and no
routers yet: this is a credential + API-access foundation, so a Sheets node or
an OAuth callback router registers itself here when it lands.
"""

from __future__ import annotations

from api.services.integrations.base import IntegrationPackageSpec
from api.services.integrations.registry import register_package

from .oauth import (
    GOOGLE_PROVIDER,
    GoogleConnection,
    GoogleOAuthError,
    GoogleOAuthState,
    GoogleTokens,
    build_authorization_url,
    build_oauth_state,
    disconnect,
    exchange_code,
    get_connection,
    get_valid_access_token,
    google_oauth_public_config,
    is_google_oauth_configured,
    mark_connection_revoked,
    parse_oauth_state,
    refresh_access_token,
    store_credentials,
)
from .sheets_client import (
    GoogleSheetsClient,
    GoogleSheetsError,
    build_a1_range,
    column_index,
    column_letter,
    normalize_column_letter,
)

PACKAGE = register_package(IntegrationPackageSpec(name="google"))

__all__ = [
    "GOOGLE_PROVIDER",
    "PACKAGE",
    "GoogleConnection",
    "GoogleOAuthError",
    "GoogleOAuthState",
    "GoogleSheetsClient",
    "GoogleSheetsError",
    "GoogleTokens",
    "build_a1_range",
    "build_authorization_url",
    "build_oauth_state",
    "column_index",
    "column_letter",
    "disconnect",
    "exchange_code",
    "get_connection",
    "get_valid_access_token",
    "google_oauth_public_config",
    "is_google_oauth_configured",
    "mark_connection_revoked",
    "normalize_column_letter",
    "parse_oauth_state",
    "refresh_access_token",
    "store_credentials",
]
