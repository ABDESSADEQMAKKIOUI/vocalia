"""Meta WhatsApp Embedded Signup provisioning.

Embedded Signup lets a user link their WhatsApp Business account from the web
(a Facebook Login popup) instead of pasting a long-lived token. The popup
returns a WABA id, a phone number id and a short-lived OAuth ``code``; this
service exchanges the code for a long-lived business-integration token and
auto-creates the org's messaging configuration (plus its business number).

Deployment config is read live from ``os.environ`` where it is used — a
running deployment reflects env changes without a restart, matching the
webhook route (see ``api/routes/messaging_whatsapp.py``). The canonical list
of these vars is documented in ``api/constants.py``:

  WHATSAPP_APP_ID                     Meta app id (public; drives the popup)
  WHATSAPP_APP_SECRET                 Meta app secret (token exchange + webhook sig)
  WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID  Facebook Login "configuration id" (public)
  WHATSAPP_GRAPH_VERSION              Graph API version (default v23.0)
  WHATSAPP_VERIFY_TOKEN               webhook verify token (stored per-config)

Secrets are encrypted at rest (``encrypt_credentials``) and every DB call is
org-scoped.
"""

from __future__ import annotations

import os
import secrets

from loguru import logger
from sqlalchemy.exc import IntegrityError

from api.db import db_client
from api.db.models import MessagingConfigurationModel
from api.services.messaging.whatsapp.client import (
    DEFAULT_API_VERSION,
    WhatsAppApiError,
    WhatsAppClient,
)
from api.utils.crypto import encrypt_credentials


def _graph_version() -> str:
    """The Graph API version this deployment targets (default v23.0)."""
    return os.environ.get("WHATSAPP_GRAPH_VERSION") or DEFAULT_API_VERSION


def is_embedded_signup_enabled() -> bool:
    """Embedded Signup is only offered when both the app id and the Facebook
    Login configuration id are configured for the deployment."""
    return bool(
        os.environ.get("WHATSAPP_APP_ID")
        and os.environ.get("WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID")
    )


def embedded_signup_public_config() -> dict:
    """Browser-safe config the UI needs to open the Facebook Login popup.

    The app id and configuration id are public by design; no secret is
    exposed here.
    """
    return {
        "enabled": is_embedded_signup_enabled(),
        "app_id": os.environ.get("WHATSAPP_APP_ID") or None,
        "config_id": os.environ.get("WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID") or None,
        "graph_version": _graph_version(),
    }


async def provision_from_embedded_signup(
    *,
    organization_id: int,
    created_by: int,
    code: str,
    waba_id: str,
    phone_number_id: str,
    business_id: str | None = None,
    name: str | None = None,
) -> MessagingConfigurationModel:
    """Exchange an Embedded Signup OAuth code and provision a WhatsApp
    messaging configuration for the organization.

    Steps:
      1. Exchange the short-lived ``code`` for a long-lived access token
         (hard failure — raises WhatsAppApiError).
      2. Best-effort fetch of the number's display number / verified name
         (tolerated on failure — the token is valid without it).
      3. Persist the configuration (credentials encrypted at rest) and its
         business number, org-scoped. A duplicate name or already-connected
         phone number surfaces as a ValueError with a human message.
      4. Best-effort subscribe the app to the WABA and register the phone
         number (logged + skipped on failure — never blocks provisioning).

    Returns the created configuration with its addresses eager-loaded.
    """
    app_id = os.environ.get("WHATSAPP_APP_ID")
    app_secret = os.environ.get("WHATSAPP_APP_SECRET")
    if not app_id or not app_secret:
        raise WhatsAppApiError(
            "WhatsApp Embedded Signup is not configured on this deployment "
            "(missing WHATSAPP_APP_ID / WHATSAPP_APP_SECRET).",
            retry_kind="never",
        )

    graph_version = _graph_version()

    # 1. Exchange the code — hard failure if Meta rejects it.
    access_token = await WhatsAppClient.exchange_oauth_code(
        app_id, app_secret, code, graph_version=graph_version
    )

    client = WhatsAppClient(
        access_token=access_token,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        api_version=graph_version,
    )

    # 2. Best-effort: resolve the display number + verified name for nicer
    #    defaults. Tolerate failures — provisioning still succeeds.
    display_number: str | None = None
    verified_name: str | None = None
    try:
        info = await client.get_phone_number_info(phone_number_id)
        display_number = info.get("display_phone_number") or None
        verified_name = info.get("verified_name") or None
    except WhatsAppApiError as e:
        logger.warning(
            "Embedded Signup: could not fetch phone number info for {}: {}",
            phone_number_id,
            e,
        )

    credentials = {
        "access_token": access_token,
        "app_secret": app_secret or "",
        "verify_token": os.environ.get("WHATSAPP_VERIFY_TOKEN") or "",
        "waba_id": waba_id,
    }

    # 3. Persist. Encrypt credentials at rest; keep every write org-scoped.
    try:
        config = await db_client.create_messaging_configuration(
            organization_id=organization_id,
            name=name or verified_name or f"WhatsApp {waba_id}",
            provider="whatsapp",
            credentials=encrypt_credentials(credentials),
        )
    except IntegrityError as e:
        raise ValueError(
            f"A WhatsApp configuration named "
            f"'{name or verified_name or f'WhatsApp {waba_id}'}' already "
            f"exists in this organization. Rename it or pass a different name."
        ) from e

    # The register PIN is generated server-side; stash it in the address'
    # metadata (never surfaced in API responses) so a later re-registration
    # can reuse it, rather than losing it or leaking it via credentials.
    register_pin = f"{secrets.randbelow(1_000_000):06d}"
    try:
        await db_client.create_messaging_address(
            organization_id=organization_id,
            messaging_configuration_id=config.id,
            address=display_number or phone_number_id,
            external_id=phone_number_id,
            account_id=waba_id,
            extra_metadata={"register_pin": register_pin},
        )
    except IntegrityError as e:
        # external_id (Meta phone_number_id) is globally unique — roll the
        # just-created config back so the caller starts from a clean slate.
        await db_client.delete_messaging_configuration(
            config.id, organization_id=organization_id
        )
        raise ValueError(
            f"This WhatsApp phone number ({phone_number_id}) is already "
            f"connected to a messaging configuration."
        ) from e

    # 4. Best-effort activation — log and continue on failure so a transient
    #    Meta hiccup can't lose the configuration we just provisioned.
    try:
        await client.subscribe_app_to_waba(waba_id)
    except WhatsAppApiError as e:
        logger.warning(
            "Embedded Signup: failed to subscribe app to WABA {}: {}", waba_id, e
        )

    try:
        await client.register_phone_number(phone_number_id, register_pin)
    except WhatsAppApiError as e:
        logger.warning(
            "Embedded Signup: failed to register phone number {}: {}",
            phone_number_id,
            e,
        )

    logger.info(
        "Provisioned WhatsApp configuration {} (waba={}, phone_number_id={}, "
        "business_id={}) for organization {} by user {} via Embedded Signup",
        config.id,
        waba_id,
        phone_number_id,
        business_id,
        organization_id,
        created_by,
    )

    # Re-fetch with addresses eager-loaded for the response shape. The row was
    # just created for this org, so this always resolves; guard defensively so
    # we never return a detached instance with an unloaded addresses relation.
    row = await db_client.get_messaging_configuration(
        config.id, organization_id=organization_id
    )
    if row is None:
        raise WhatsAppApiError(
            "Provisioned WhatsApp configuration could not be reloaded.",
            retry_kind="never",
        )
    return row
