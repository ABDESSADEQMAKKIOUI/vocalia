"""WhatsApp inbox schemas.

Request/response bodies for the org-facing inbox endpoints in
``api/routes/messaging_inbox.py`` (conversation list, thread detail,
human replies, agent pause). Shapes are pinned by the inbox HTTP
contract shared with the UI.
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# Meta caps free-form text message bodies at 4096 characters.
WHATSAPP_TEXT_MAX_CHARS = 4096


class WhatsAppInboxMessage(BaseModel):
    """One rendered thread entry (inbound user message, agent/human reply,
    or the campaign template that opened the conversation)."""

    direction: Literal["in", "out"]
    origin: Literal["user", "agent", "human", "template"]
    text: str
    # ISO-8601 string (turn timestamps are stored as strings in the text
    # session), null when the source turn carries none.
    timestamp: Optional[str] = None


class WhatsAppConversationSummary(BaseModel):
    """One inbox list item (same shape nested in the detail response)."""

    id: int
    wa_id: str
    profile_name: Optional[str] = None
    phone_number_id: str
    # The business number the conversation runs on (messaging_address.address).
    display_number: Optional[str] = None
    state: str
    agent_paused: bool
    service_window_expires_at: Optional[datetime] = None
    last_inbound_at: Optional[datetime] = None
    last_outbound_at: Optional[datetime] = None
    workflow_run_id: int
    campaign_id: Optional[int] = None
    # Name of the workflow behind the conversation's run, when resolvable.
    agent_name: Optional[str] = None
    updated_at: Optional[datetime] = None
    # Last message text truncated to 80 chars, null for empty threads.
    last_message_preview: Optional[str] = None


class WhatsAppConversationListResponse(BaseModel):
    conversations: List[WhatsAppConversationSummary]


class WhatsAppConversationDetailResponse(BaseModel):
    conversation: WhatsAppConversationSummary
    messages: List[WhatsAppInboxMessage]


class WhatsAppHumanReplyRequest(BaseModel):
    """Body for ``POST /messaging/whatsapp/conversations/{id}/send``."""

    text: str = Field(..., min_length=1, max_length=WHATSAPP_TEXT_MAX_CHARS)


class WhatsAppAgentPausedRequest(BaseModel):
    """Body for ``POST /messaging/whatsapp/conversations/{id}/agent``."""

    paused: bool


class WhatsAppAgentPausedResponse(BaseModel):
    id: int
    agent_paused: bool
