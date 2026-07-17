"""Messaging (WhatsApp Cloud API) configuration schemas.

Request/response bodies for the org-facing configuration endpoints in
``api/routes/messaging_config.py``. Credentials in responses are always
masked — real secrets never leave the backend.
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class MessagingCredentialsRequest(BaseModel):
    """WhatsApp Cloud API credentials as submitted on create.

    ``app_secret`` and ``verify_token`` are optional — when absent, the
    webhook route falls back to the deployment-level WHATSAPP_APP_SECRET /
    WHATSAPP_VERIFY_TOKEN environment variables.
    """

    access_token: str = Field(..., min_length=1)
    app_secret: Optional[str] = None
    verify_token: Optional[str] = None
    waba_id: str = Field(..., min_length=1)


class MessagingCredentialsUpdateRequest(BaseModel):
    """Partial credentials update. Masked or omitted values keep the
    stored secret (see ``merge_masked_credentials`` in the routes module)."""

    access_token: Optional[str] = None
    app_secret: Optional[str] = None
    verify_token: Optional[str] = None
    waba_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Addresses (business phone numbers)
# ---------------------------------------------------------------------------


class MessagingAddressCreateRequest(BaseModel):
    """One WhatsApp business number attached to a configuration.

    ``external_id`` is Meta's phone_number_id — the inbound routing key.
    """

    address: str = Field(..., min_length=1, max_length=64)
    external_id: str = Field(..., min_length=1, max_length=64)
    account_id: Optional[str] = Field(default=None, max_length=64)
    inbound_workflow_id: Optional[int] = None


class MessagingAddressUpdateRequest(BaseModel):
    """Body for ``PUT /messaging/configurations/addresses/{address_id}``.

    ``clear_inbound_workflow=True`` detaches the inbound workflow; a
    non-null ``inbound_workflow_id`` re-attaches (org-ownership checked).
    """

    inbound_workflow_id: Optional[int] = None
    clear_inbound_workflow: bool = False
    is_active: Optional[bool] = None


class MessagingAddressResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    address: str
    external_id: str
    account_id: Optional[str] = None
    inbound_workflow_id: Optional[int] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Configurations
# ---------------------------------------------------------------------------


class MessagingConfigurationCreateRequest(BaseModel):
    """Body for ``POST /messaging/configurations``."""

    name: str = Field(..., min_length=1, max_length=64)
    credentials: MessagingCredentialsRequest
    addresses: List[MessagingAddressCreateRequest] = Field(default_factory=list)


class MessagingConfigurationUpdateRequest(BaseModel):
    """Body for ``PUT /messaging/configurations/{id}``. Partial update."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    is_active: Optional[bool] = None
    credentials: Optional[MessagingCredentialsUpdateRequest] = None


class MessagingConfigurationResponse(BaseModel):
    """One configuration with MASKED credentials and its addresses."""

    id: int
    name: str
    provider: str
    is_active: bool
    credentials: dict
    addresses: List[MessagingAddressResponse]
    created_at: datetime
    updated_at: datetime


class MessagingConfigurationListResponse(BaseModel):
    configurations: List[MessagingConfigurationResponse]


# ---------------------------------------------------------------------------
# Embedded Signup (Meta "Connect with WhatsApp" / Facebook Login popup)
# ---------------------------------------------------------------------------


class EmbeddedSignupConfigResponse(BaseModel):
    """Browser-safe config the UI needs to open the Facebook Login popup.

    ``enabled`` is False unless both the app id and the Facebook Login
    configuration id are set for the deployment; ``app_id`` and ``config_id``
    are public by design (no secret is ever returned here).
    """

    enabled: bool
    app_id: Optional[str] = None
    config_id: Optional[str] = None
    graph_version: str


class EmbeddedSignupCompleteRequest(BaseModel):
    """Body for ``POST /messaging/configurations/embedded-signup`` — the
    values the Facebook Login popup hands back to the browser.

    ``code`` is a short-lived OAuth code exchanged server-side for a
    long-lived token; ``waba_id`` and ``phone_number_id`` identify the
    account and business number that were just linked.
    """

    code: str = Field(..., min_length=1)
    waba_id: str = Field(..., min_length=1)
    phone_number_id: str = Field(..., min_length=1)
    business_id: Optional[str] = None
    name: Optional[str] = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Suppressions (do-not-message list)
# ---------------------------------------------------------------------------


class MessagingSuppressionCreateRequest(BaseModel):
    """Manually add an address to the do-not-message list."""

    address: str = Field(..., min_length=1, max_length=64)
    scope: Literal["marketing", "all"] = "marketing"
    reason: Literal["manual"] = "manual"
    expires_at: Optional[datetime] = None


class MessagingSuppressionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    address: str
    scope: str
    reason: str
    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class MessagingSuppressionListResponse(BaseModel):
    suppressions: List[MessagingSuppressionResponse]


# ---------------------------------------------------------------------------
# Webhook info
# ---------------------------------------------------------------------------


class MessagingWebhookInfoResponse(BaseModel):
    """What the UI shows the user to paste into the Meta App dashboard."""

    webhook_url: str
    verify_token_set: bool
    app_secret_set: bool
