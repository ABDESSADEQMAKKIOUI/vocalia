"""Org-facing messaging (WhatsApp Cloud API) configuration endpoints.

CRUD for messaging configurations, their business numbers (addresses) and
the do-not-message suppression list, plus the webhook info the UI shows
the user to paste into the Meta App dashboard.

Credentials are encrypted at rest (api.utils.crypto) and always masked in
responses. Updates follow the telephony masked round-trip convention: a
value that looks masked never overwrites the stored secret.
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy.exc import IntegrityError

from api.db import db_client
from api.db.models import MessagingConfigurationModel, UserModel
from api.schemas.messaging_config import (
    EmbeddedSignupCompleteRequest,
    EmbeddedSignupConfigResponse,
    MessagingAddressCreateRequest,
    MessagingAddressResponse,
    MessagingAddressUpdateRequest,
    MessagingConfigurationCreateRequest,
    MessagingConfigurationListResponse,
    MessagingConfigurationResponse,
    MessagingConfigurationUpdateRequest,
    MessagingSuppressionCreateRequest,
    MessagingSuppressionListResponse,
    MessagingSuppressionResponse,
    MessagingWebhookInfoResponse,
)
from api.services.auth.depends import get_user_with_selected_organization
from api.services.configuration.masking import (
    contains_masked_key,
    is_mask_of,
    mask_key,
)
from api.services.messaging.whatsapp.client import WhatsAppApiError
from api.services.messaging.whatsapp.embedded_signup import (
    embedded_signup_public_config,
    provision_from_embedded_signup,
)
from api.utils.common import get_backend_endpoints
from api.utils.crypto import decrypt_credentials, encrypt_credentials

router = APIRouter(prefix="/messaging/configurations", tags=["messaging"])

# Credential keys whose values are secrets and must be masked for display.
# waba_id is an identifier, not a secret — it is returned in full.
SECRET_CREDENTIAL_KEYS = ("access_token", "app_secret", "verify_token")


# ---------------------------------------------------------------------------
# Masking helpers (pure — unit-tested in tests/messaging)
# ---------------------------------------------------------------------------


def mask_messaging_credentials(credentials: dict) -> dict:
    """Return a display-safe copy of *decrypted* credentials.

    Secret keys keep only their last characters visible (same convention
    as the telephony masking helpers); identifier keys pass through.
    """
    out: dict = {}
    for key, value in credentials.items():
        if key in SECRET_CREDENTIAL_KEYS and isinstance(value, str) and value:
            out[key] = mask_key(value)
        else:
            out[key] = value
    return out


def merge_masked_credentials(incoming: dict, existing: dict) -> dict:
    """Merge an update payload against the stored (decrypted) credentials.

    A masked round-trip must never overwrite a real secret: incoming values
    that are empty/None or look masked keep the stored value; genuinely new
    values win. Keys absent from the payload are preserved unchanged.
    """
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None or value == "":
            continue
        if isinstance(value, str):
            stored = existing.get(key)
            looks_masked = contains_masked_key(value) or (
                isinstance(stored, str) and stored and is_mask_of(value, stored)
            )
            if looks_masked:
                continue
        merged[key] = value
    return merged


def _reject_masked_credentials(credentials: dict) -> None:
    """422 when a create payload contains masked placeholders — there is no
    stored secret to fall back to on create."""
    for key, value in credentials.items():
        if isinstance(value, str) and contains_masked_key(value):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Credential '{key}' appears to be masked. "
                    "Provide the actual value, not the masked value."
                ),
            )


# ---------------------------------------------------------------------------
# Response shaping / org-scoping helpers
# ---------------------------------------------------------------------------


def _configuration_response(
    row: MessagingConfigurationModel,
) -> MessagingConfigurationResponse:
    decrypted = decrypt_credentials(row.credentials or {})
    return MessagingConfigurationResponse(
        id=row.id,
        name=row.name,
        provider=row.provider,
        is_active=row.is_active,
        credentials=mask_messaging_credentials(decrypted),
        addresses=[
            MessagingAddressResponse.model_validate(a)
            for a in sorted(row.addresses, key=lambda a: a.id)
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _get_owned_configuration(
    configuration_id: int, organization_id: int
) -> MessagingConfigurationModel:
    row = await db_client.get_messaging_configuration(
        configuration_id, organization_id=organization_id
    )
    if not row:
        raise HTTPException(
            status_code=404, detail="Messaging configuration not found"
        )
    return row


async def _ensure_workflow_belongs_to_org(workflow_id: int, organization_id: int):
    workflow = await db_client.get_workflow(
        workflow_id, organization_id=organization_id
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


# ---------------------------------------------------------------------------
# Webhook info (registered before /{configuration_id} routes)
# ---------------------------------------------------------------------------


@router.get("/webhook-info", response_model=MessagingWebhookInfoResponse)
async def get_webhook_info(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """Return the webhook URL + env-fallback flags for the Meta dashboard."""
    backend_endpoint, _ = await get_backend_endpoints()
    return MessagingWebhookInfoResponse(
        webhook_url=f"{backend_endpoint}/api/v1/messaging/whatsapp/webhook",
        verify_token_set=bool(os.environ.get("WHATSAPP_VERIFY_TOKEN")),
        app_secret_set=bool(os.environ.get("WHATSAPP_APP_SECRET")),
    )


# ---------------------------------------------------------------------------
# Embedded Signup (registered before /{configuration_id} routes)
# ---------------------------------------------------------------------------


@router.get(
    "/embedded-signup/config", response_model=EmbeddedSignupConfigResponse
)
async def get_embedded_signup_config(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """Public config the UI needs to open the Meta Embedded Signup popup."""
    return EmbeddedSignupConfigResponse(**embedded_signup_public_config())


@router.post("/embedded-signup", response_model=MessagingConfigurationResponse)
async def complete_embedded_signup(
    request: EmbeddedSignupCompleteRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """Exchange the Embedded Signup code and auto-create the WhatsApp
    configuration (credentials encrypted at rest, masked on read) for the
    caller's organization."""
    try:
        config = await provision_from_embedded_signup(
            organization_id=user.selected_organization_id,
            created_by=user.id,
            code=request.code,
            waba_id=request.waba_id,
            phone_number_id=request.phone_number_id,
            business_id=request.business_id,
            name=request.name,
        )
    except (WhatsAppApiError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _configuration_response(config)


# ---------------------------------------------------------------------------
# Suppressions (registered before /{configuration_id} routes)
# ---------------------------------------------------------------------------


@router.get("/suppressions", response_model=MessagingSuppressionListResponse)
async def list_suppressions(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: UserModel = Depends(get_user_with_selected_organization),
):
    rows = await db_client.list_messaging_suppressions(
        user.selected_organization_id, limit=limit, offset=offset
    )
    return MessagingSuppressionListResponse(
        suppressions=[MessagingSuppressionResponse.model_validate(r) for r in rows]
    )


@router.post("/suppressions", response_model=MessagingSuppressionResponse)
async def create_suppression(
    request: MessagingSuppressionCreateRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    row = await db_client.upsert_messaging_suppression(
        organization_id=user.selected_organization_id,
        address=request.address,
        scope=request.scope,
        reason=request.reason,
        expires_at=request.expires_at,
    )
    return MessagingSuppressionResponse.model_validate(row)


@router.delete("/suppressions/{suppression_id}")
async def delete_suppression(
    suppression_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    deleted = await db_client.delete_messaging_suppression(
        suppression_id, organization_id=user.selected_organization_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Suppression not found")
    return {"message": "Suppression deleted"}


# ---------------------------------------------------------------------------
# Configurations CRUD
# ---------------------------------------------------------------------------


@router.get("/", response_model=MessagingConfigurationListResponse)
async def list_messaging_configurations(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """List the org's messaging configurations with masked credentials."""
    rows = await db_client.list_messaging_configurations(
        user.selected_organization_id
    )
    return MessagingConfigurationListResponse(
        configurations=[_configuration_response(r) for r in rows]
    )


@router.post("/", response_model=MessagingConfigurationResponse)
async def create_messaging_configuration(
    request: MessagingConfigurationCreateRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """Create a configuration (credentials encrypted at rest) and its numbers."""
    organization_id = user.selected_organization_id

    credentials = request.credentials.model_dump(exclude_none=True)
    _reject_masked_credentials(credentials)

    # Validate FK ownership before any write (see AGENTS.md org-scoping rule).
    for address in request.addresses:
        if address.inbound_workflow_id is not None:
            await _ensure_workflow_belongs_to_org(
                address.inbound_workflow_id, organization_id
            )

    try:
        config = await db_client.create_messaging_configuration(
            organization_id=organization_id,
            name=request.name,
            provider="whatsapp",
            credentials=encrypt_credentials(credentials),
        )
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A messaging configuration named '{request.name}' already "
                f"exists in this organization. Pick a different name."
            ),
        )

    for address in request.addresses:
        try:
            await db_client.create_messaging_address(
                organization_id=organization_id,
                messaging_configuration_id=config.id,
                address=address.address,
                external_id=address.external_id,
                account_id=address.account_id,
                inbound_workflow_id=address.inbound_workflow_id,
            )
        except IntegrityError:
            # external_id (Meta phone_number_id) is globally unique — roll the
            # whole create back so the client can retry with a clean slate.
            await db_client.delete_messaging_configuration(
                config.id, organization_id=organization_id
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Phone number id '{address.external_id}' is already "
                    f"registered to a messaging configuration."
                ),
            )

    logger.info(
        "Created messaging configuration {} for organization {}",
        config.id,
        organization_id,
    )
    row = await _get_owned_configuration(config.id, organization_id)
    return _configuration_response(row)


@router.put("/{configuration_id}", response_model=MessagingConfigurationResponse)
async def update_messaging_configuration(
    configuration_id: int,
    request: MessagingConfigurationUpdateRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """Update name/is_active/credentials. Masked credential values are
    placeholders and keep the stored secret."""
    organization_id = user.selected_organization_id
    existing = await _get_owned_configuration(configuration_id, organization_id)

    encrypted_credentials = None
    if request.credentials is not None:
        incoming = request.credentials.model_dump(exclude_none=True)
        stored = decrypt_credentials(existing.credentials or {})
        merged = merge_masked_credentials(incoming, stored)
        encrypted_credentials = encrypt_credentials(merged)

    row = await db_client.update_messaging_configuration(
        configuration_id,
        organization_id=organization_id,
        name=request.name,
        credentials=encrypted_credentials,
        is_active=request.is_active,
    )
    if not row:
        raise HTTPException(
            status_code=404, detail="Messaging configuration not found"
        )
    # Re-fetch with addresses eager-loaded for the response.
    row = await _get_owned_configuration(configuration_id, organization_id)
    return _configuration_response(row)


@router.delete("/{configuration_id}")
async def delete_messaging_configuration(
    configuration_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    deleted = await db_client.delete_messaging_configuration(
        configuration_id, organization_id=user.selected_organization_id
    )
    if not deleted:
        raise HTTPException(
            status_code=404, detail="Messaging configuration not found"
        )
    return {"message": "Messaging configuration deleted"}


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


@router.post(
    "/{configuration_id}/addresses", response_model=MessagingAddressResponse
)
async def create_messaging_address(
    configuration_id: int,
    request: MessagingAddressCreateRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    organization_id = user.selected_organization_id
    await _get_owned_configuration(configuration_id, organization_id)

    if request.inbound_workflow_id is not None:
        await _ensure_workflow_belongs_to_org(
            request.inbound_workflow_id, organization_id
        )

    try:
        row = await db_client.create_messaging_address(
            organization_id=organization_id,
            messaging_configuration_id=configuration_id,
            address=request.address,
            external_id=request.external_id,
            account_id=request.account_id,
            inbound_workflow_id=request.inbound_workflow_id,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Phone number id '{request.external_id}' is already "
                f"registered to a messaging configuration."
            ),
        )
    return MessagingAddressResponse.model_validate(row)


@router.put("/addresses/{address_id}", response_model=MessagingAddressResponse)
async def update_messaging_address(
    address_id: int,
    request: MessagingAddressUpdateRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    organization_id = user.selected_organization_id

    # `...` = leave the inbound workflow unchanged (db-client sentinel).
    inbound_workflow_id = ...
    if request.clear_inbound_workflow:
        inbound_workflow_id = None
    elif request.inbound_workflow_id is not None:
        await _ensure_workflow_belongs_to_org(
            request.inbound_workflow_id, organization_id
        )
        inbound_workflow_id = request.inbound_workflow_id

    row = await db_client.update_messaging_address(
        address_id,
        organization_id=organization_id,
        inbound_workflow_id=inbound_workflow_id,
        is_active=request.is_active,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Messaging address not found")
    return MessagingAddressResponse.model_validate(row)


@router.delete("/addresses/{address_id}")
async def delete_messaging_address(
    address_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    deleted = await db_client.delete_messaging_address(
        address_id, organization_id=user.selected_organization_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Messaging address not found")
    return {"message": "Messaging address deleted"}
