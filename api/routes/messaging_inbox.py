"""Org-facing WhatsApp inbox endpoints.

Browse conversations, read a thread, send a human reply while the 24h
service window is open, and pause/resume the agent (human takeover).
Thin routes per AGENTS.md: auth + shaping here, domain logic in
``api.services.messaging.whatsapp.inbox_service``.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db.models import UserModel
from api.schemas.messaging_inbox import (
    WhatsAppAgentPausedRequest,
    WhatsAppAgentPausedResponse,
    WhatsAppConversationDetailResponse,
    WhatsAppConversationListResponse,
    WhatsAppHumanReplyRequest,
)
from api.services.auth.depends import get_user_with_selected_organization
from api.services.messaging.whatsapp import inbox_service

router = APIRouter(prefix="/messaging/whatsapp/conversations", tags=["messaging"])


@router.get("", response_model=WhatsAppConversationListResponse)
async def list_whatsapp_conversations(
    state: Literal["open", "closed", "all"] = Query(default="all"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """The org's WhatsApp conversations, most recently active first."""
    conversations = await inbox_service.list_conversations(
        user.selected_organization_id,
        state=state,
        limit=limit,
        offset=offset,
    )
    return WhatsAppConversationListResponse(conversations=conversations)


@router.get(
    "/{conversation_id}", response_model=WhatsAppConversationDetailResponse
)
async def get_whatsapp_conversation(
    conversation_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """One conversation plus its chronological message thread."""
    detail = await inbox_service.get_conversation_detail(
        user.selected_organization_id, conversation_id
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return WhatsAppConversationDetailResponse(**detail)


@router.post(
    "/{conversation_id}/send", response_model=WhatsAppConversationDetailResponse
)
async def send_whatsapp_human_reply(
    conversation_id: int,
    request: WhatsAppHumanReplyRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """Send a free-form operator reply; 400 when the 24h window is closed
    or the Cloud API send fails."""
    try:
        detail = await inbox_service.send_human_reply(
            user.selected_organization_id, conversation_id, request.text
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if detail is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return WhatsAppConversationDetailResponse(**detail)


@router.post(
    "/{conversation_id}/agent", response_model=WhatsAppAgentPausedResponse
)
async def set_whatsapp_agent_paused(
    conversation_id: int,
    request: WhatsAppAgentPausedRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """Pause (human takeover) or resume the agent for one conversation."""
    updated = await inbox_service.set_agent_paused(
        user.selected_organization_id, conversation_id, request.paused
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return WhatsAppAgentPausedResponse(
        id=conversation_id, agent_paused=request.paused
    )
