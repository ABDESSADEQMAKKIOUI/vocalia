"""Parsing of Meta WhatsApp Cloud API webhook payloads.

Pure functions — no I/O, no DB. The public webhook route splits a raw
payload into per-change work items (``parse_webhook_payload``) that are
enqueued to ARQ, and the inbound task turns a "messages" change value
into typed inbound messages plus raw status events
(``parse_messages_value``).

Parsing is defensive by design: Meta payload shapes vary by message type
and evolve over time, so missing or malformed keys never raise. Unknown
message types still yield a ``ParsedInboundMessage`` with
``message_type`` set and ``text=None``; the untouched payload is always
preserved on ``raw``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from loguru import logger

# Webhook change `field` -> internal change kind fanned out to the inbound task.
_FIELD_TO_KIND = {
    "messages": "messages",
    "message_template_status_update": "template_status",
    "message_template_quality_update": "template_quality",
    "template_category_update": "template_category",
}

# Message types whose payload is a media object stored under a key of the
# same name (with id / mime_type / optional caption).
_MEDIA_MESSAGE_TYPES = ("audio", "image", "video", "document", "sticker")


@dataclass
class ParsedInboundMessage:
    """One inbound end-user message extracted from a "messages" change.

    Every field defaults so ``from_dict`` round-trips partial dicts (the
    route serializes changes into ARQ jobs as plain JSON).
    """

    wamid: str = ""
    from_wa_id: str = ""
    profile_name: str | None = None
    phone_number_id: str | None = None
    display_phone_number: str | None = None
    message_type: str = "unknown"
    text: str | None = None
    media_id: str | None = None
    mime_type: str | None = None
    is_voice_note: bool = False
    interactive_reply_id: str | None = None
    interactive_reply_title: str | None = None
    quoted_wamid: str | None = None
    timestamp: int | None = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ParsedInboundMessage":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in _as_dict(data).items() if k in known})


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str | None:
    """Coerce an id-ish/text-ish payload value to a non-empty string."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _parse_timestamp(value: Any) -> int | None:
    """Meta sends epoch seconds as a string; tolerate ints and garbage."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def parse_webhook_payload(payload: dict) -> list[dict]:
    """Split a raw webhook payload into per-change work items.

    Returns one dict per recognized change across every entry:
    ``{"kind": <kind>, "phone_number_id": str | None, "value": <raw value>}``
    where kind is one of "messages", "template_status", "template_quality",
    "template_category". Unrecognized change fields are skipped. Template
    changes are WABA-level, so their phone_number_id is None.
    """
    changes_out: list[dict] = []
    for entry in _as_list(_as_dict(payload).get("entry")):
        for change in _as_list(_as_dict(entry).get("changes")):
            change = _as_dict(change)
            field_name = change.get("field")
            kind = _FIELD_TO_KIND.get(field_name)
            if kind is None:
                logger.debug(
                    f"Ignoring unhandled WhatsApp webhook change field: {field_name}"
                )
                continue
            value = _as_dict(change.get("value"))
            phone_number_id = _as_str(
                _as_dict(value.get("metadata")).get("phone_number_id")
            )
            changes_out.append(
                {"kind": kind, "phone_number_id": phone_number_id, "value": value}
            )
    return changes_out


def parse_messages_value(
    value: dict,
) -> tuple[list[ParsedInboundMessage], list[dict]]:
    """Parse the value of a "messages" change.

    Returns ``(inbound messages, raw status dicts)``. Statuses (sent /
    delivered / read / failed, incl. their ``errors``) are passed through
    untyped for the status handler.
    """
    value = _as_dict(value)
    metadata = _as_dict(value.get("metadata"))
    phone_number_id = _as_str(metadata.get("phone_number_id"))
    display_phone_number = _as_str(metadata.get("display_phone_number"))

    profile_names: dict[str, str] = {}
    fallback_profile_name: str | None = None
    for contact in _as_list(value.get("contacts")):
        contact = _as_dict(contact)
        wa_id = _as_str(contact.get("wa_id"))
        name = _as_str(_as_dict(contact.get("profile")).get("name"))
        if name and fallback_profile_name is None:
            fallback_profile_name = name
        if wa_id and name:
            profile_names[wa_id] = name

    messages = [
        _parse_message(
            raw_message,
            phone_number_id=phone_number_id,
            display_phone_number=display_phone_number,
            profile_names=profile_names,
            fallback_profile_name=fallback_profile_name,
        )
        for raw_message in _as_list(value.get("messages"))
        if isinstance(raw_message, dict)
    ]
    statuses = [s for s in _as_list(value.get("statuses")) if isinstance(s, dict)]
    return messages, statuses


def _parse_message(
    raw: dict,
    *,
    phone_number_id: str | None,
    display_phone_number: str | None,
    profile_names: dict[str, str],
    fallback_profile_name: str | None,
) -> ParsedInboundMessage:
    message_type = _as_str(raw.get("type")) or "unknown"
    from_wa_id = _as_str(raw.get("from")) or ""
    message = ParsedInboundMessage(
        wamid=_as_str(raw.get("id")) or "",
        from_wa_id=from_wa_id,
        profile_name=profile_names.get(from_wa_id) or fallback_profile_name,
        phone_number_id=phone_number_id,
        display_phone_number=display_phone_number,
        message_type=message_type,
        quoted_wamid=_as_str(_as_dict(raw.get("context")).get("id")),
        timestamp=_parse_timestamp(raw.get("timestamp")),
        raw=raw,
    )

    # Type-specific content lives under a key named after the type.
    content = _as_dict(raw.get(message_type))

    if message_type == "text":
        message.text = _as_str(content.get("body"))
    elif message_type in _MEDIA_MESSAGE_TYPES:
        message.media_id = _as_str(content.get("id"))
        message.mime_type = _as_str(content.get("mime_type"))
        message.text = _as_str(content.get("caption"))
        if message_type == "audio":
            # Meta marks recorded voice notes (vs. attached audio files)
            # with voice: true — the trigger for STT transcription.
            message.is_voice_note = bool(content.get("voice"))
    elif message_type == "interactive":
        reply = _as_dict(content.get("button_reply")) or _as_dict(
            content.get("list_reply")
        )
        message.interactive_reply_id = _as_str(reply.get("id"))
        message.interactive_reply_title = _as_str(reply.get("title"))
        message.text = message.interactive_reply_title
    elif message_type == "button":
        # Quick-reply button tapped on a template message.
        message.interactive_reply_id = _as_str(content.get("payload"))
        message.interactive_reply_title = _as_str(content.get("text"))
        message.text = message.interactive_reply_title
    elif message_type == "reaction":
        message.text = _as_str(content.get("emoji"))
        message.quoted_wamid = (
            _as_str(content.get("message_id")) or message.quoted_wamid
        )
    # location / contacts / order / system / unsupported / unknown: the type
    # is recorded and the details remain available on `raw`.

    return message
