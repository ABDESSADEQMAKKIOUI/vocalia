"""Org-scoped CRUD + Meta submission/sync for WhatsApp message templates.

Thin handlers: definition validation and all Graph API work live in
api.services.messaging.whatsapp.template_service. Every DB call is scoped
to user.selected_organization_id (tenant isolation — see api/AGENTS.md).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from api.db import db_client
from api.db.models import (
    MessagingConfigurationModel,
    UserModel,
    WhatsAppTemplateModel,
)
from api.schemas.messaging_templates import (
    WhatsAppTemplateCreateRequest,
    WhatsAppTemplateDeleteResponse,
    WhatsAppTemplateListResponse,
    WhatsAppTemplateResponse,
    WhatsAppTemplateSyncResponse,
    WhatsAppTemplateUpdateRequest,
)
from api.services.auth.depends import get_user_with_selected_organization
from api.services.messaging.whatsapp import template_service
from api.services.messaging.whatsapp.client import WhatsAppApiError

router = APIRouter(
    prefix="/messaging/whatsapp/templates", tags=["messaging-templates"]
)


def _template_response(row: WhatsAppTemplateModel) -> WhatsAppTemplateResponse:
    response = WhatsAppTemplateResponse.model_validate(row)
    response.placeholders = template_service.extract_template_placeholders(
        row.components or [], row.parameter_format
    )
    return response


async def _get_template_or_404(
    template_id: int, user: UserModel
) -> WhatsAppTemplateModel:
    row = await db_client.get_whatsapp_template(
        template_id, organization_id=user.selected_organization_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return row


async def _get_configuration_or_404(
    configuration_id: int, user: UserModel
) -> MessagingConfigurationModel:
    config = await db_client.get_messaging_configuration(
        configuration_id, organization_id=user.selected_organization_id
    )
    if config is None:
        raise HTTPException(
            status_code=404, detail="Messaging configuration not found"
        )
    return config


def _validate_or_422(
    name: str,
    language: str,
    category: str,
    components: list[dict],
    parameter_format: str,
) -> None:
    errors = template_service.validate_template_definition(
        name, language, category, components, parameter_format
    )
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})


@router.get("", response_model=WhatsAppTemplateListResponse)
async def list_templates(
    configuration_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WhatsAppTemplateListResponse:
    rows = await db_client.list_whatsapp_templates(
        user.selected_organization_id,
        messaging_configuration_id=configuration_id,
        status=status.upper() if status else None,
    )
    return WhatsAppTemplateListResponse(
        templates=[_template_response(row) for row in rows]
    )


@router.post("", response_model=WhatsAppTemplateResponse)
async def create_template(
    request: WhatsAppTemplateCreateRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WhatsAppTemplateResponse:
    # Ownership check: the FK alone doesn't prove the caller may reference
    # this configuration.
    await _get_configuration_or_404(request.messaging_configuration_id, user)
    _validate_or_422(
        request.name,
        request.language,
        request.category,
        request.components,
        request.parameter_format,
    )
    try:
        row = await db_client.create_whatsapp_template(
            organization_id=user.selected_organization_id,
            messaging_configuration_id=request.messaging_configuration_id,
            name=request.name,
            language=request.language,
            category=request.category.upper(),
            components=request.components,
            parameter_format=request.parameter_format.lower(),
            created_by=user.id,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="A template with this name and language already exists "
            "for this configuration",
        )
    return _template_response(row)


@router.post("/sync", response_model=WhatsAppTemplateSyncResponse)
async def sync_templates(
    configuration_id: int = Query(...),
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WhatsAppTemplateSyncResponse:
    config = await _get_configuration_or_404(configuration_id, user)
    try:
        counts = await template_service.sync_templates(
            config, user.selected_organization_id
        )
    except WhatsAppApiError as e:
        raise HTTPException(
            status_code=502, detail=e.message or "Template sync failed"
        )
    return WhatsAppTemplateSyncResponse(**counts)


@router.get("/{template_id}", response_model=WhatsAppTemplateResponse)
async def get_template(
    template_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WhatsAppTemplateResponse:
    row = await _get_template_or_404(template_id, user)
    return _template_response(row)


@router.put("/{template_id}", response_model=WhatsAppTemplateResponse)
async def update_template(
    template_id: int,
    request: WhatsAppTemplateUpdateRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WhatsAppTemplateResponse:
    row = await _get_template_or_404(template_id, user)
    if row.status not in template_service.LOCALLY_EDITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Template in status {row.status} cannot be edited locally",
        )

    name = request.name if request.name is not None else row.name
    language = request.language if request.language is not None else row.language
    category = (
        request.category.upper() if request.category is not None else row.category
    )
    components = (
        request.components if request.components is not None else row.components
    )
    parameter_format = (
        request.parameter_format.lower()
        if request.parameter_format is not None
        else row.parameter_format
    )
    _validate_or_422(name, language, category, components, parameter_format)

    try:
        updated = await db_client.update_whatsapp_template(
            row.id,
            organization_id=user.selected_organization_id,
            name=name,
            language=language,
            category=category,
            components=components,
            parameter_format=parameter_format,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="A template with this name and language already exists "
            "for this configuration",
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_response(updated)


@router.delete("/{template_id}", response_model=WhatsAppTemplateDeleteResponse)
async def delete_template(
    template_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WhatsAppTemplateDeleteResponse:
    row = await _get_template_or_404(template_id, user)
    meta_deleted = await template_service.delete_template_at_meta(row)
    deleted = await db_client.delete_whatsapp_template(
        row.id, organization_id=user.selected_organization_id
    )
    return WhatsAppTemplateDeleteResponse(deleted=deleted, meta_deleted=meta_deleted)


@router.post("/{template_id}/submit", response_model=WhatsAppTemplateResponse)
async def submit_template(
    template_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WhatsAppTemplateResponse:
    row = await _get_template_or_404(template_id, user)
    if row.status not in template_service.LOCALLY_EDITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Template in status {row.status} cannot be submitted",
        )
    _validate_or_422(
        row.name, row.language, row.category, row.components, row.parameter_format
    )
    try:
        updated = await template_service.submit_template(row)
    except WhatsAppApiError as e:
        raise HTTPException(
            status_code=502, detail=e.message or "Template submission failed"
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _template_response(updated)
