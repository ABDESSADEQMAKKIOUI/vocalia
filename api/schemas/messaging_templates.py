"""Request/response schemas for WhatsApp message template routes.

Component payloads are kept as loose dicts: the authoritative shape
validation (Meta creation-format rules) lives in
api.services.messaging.whatsapp.template_service.validate_template_definition,
so the same rules apply to every caller.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WhatsAppTemplateCreateRequest(BaseModel):
    """Body for ``POST /messaging/whatsapp/templates`` — creates a DRAFT."""

    messaging_configuration_id: int
    name: str = Field(..., min_length=1, max_length=512)
    language: str = Field(..., min_length=1, max_length=16)
    category: str
    components: list[dict] = Field(default_factory=list)
    parameter_format: str = "positional"


class WhatsAppTemplateUpdateRequest(BaseModel):
    """Body for ``PUT /messaging/whatsapp/templates/{id}``.

    Partial update of a locally-editable row; ``components`` replaces the
    stored list wholesale when provided.
    """

    name: str | None = Field(default=None, min_length=1, max_length=512)
    language: str | None = Field(default=None, min_length=1, max_length=16)
    category: str | None = None
    components: list[dict] | None = None
    parameter_format: str | None = None


class WhatsAppTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    messaging_configuration_id: int
    meta_template_id: str | None = None
    name: str
    language: str
    category: str
    parameter_format: str
    components: list[dict]
    status: str
    quality_score: str | None = None
    rejection_reason: str | None = None
    category_pending_change: str | None = None
    # Placeholder keys a sender must fill (["1", "2"] or named params) —
    # derived from components, not stored.
    placeholders: list[str] = Field(default_factory=list)
    last_synced_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class WhatsAppTemplateListResponse(BaseModel):
    templates: list[WhatsAppTemplateResponse]


class WhatsAppTemplateSyncResponse(BaseModel):
    """Counts from a reconciliation sync against Meta."""

    fetched: int
    created: int
    updated: int
    unchanged: int


class WhatsAppTemplateDeleteResponse(BaseModel):
    deleted: bool
    # True/False for the best-effort Meta deletion; None when the template
    # was never submitted to Meta.
    meta_deleted: bool | None = None
