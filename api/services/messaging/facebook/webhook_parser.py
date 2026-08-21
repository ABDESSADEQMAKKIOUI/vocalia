"""Parse Meta Messenger (Facebook Page) webhook payloads.

A `page` webhook delivery groups events under ``entry[].messaging[]``. Each
messaging item is either an inbound user ``message`` (text / attachments /
quick reply), a ``postback`` (button tap), or bookkeeping (delivery, read,
echoes of our own sends) which we drop. ``parse_messenger_payload`` splits a
delivery into per-event change dicts for ARQ; ``parse_messaging_event`` turns
one such item into a typed message the conversation runtime consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedMessengerMessage:
    """One inbound Messenger event, normalized."""

    page_id: str = ""
    sender_id: str = ""  # the user's Page-Scoped ID (PSID)
    mid: str = ""
    text: str | None = None
    timestamp: int | None = None
    is_echo: bool = False
    attachments: list = field(default_factory=list)
    quick_reply_payload: str | None = None
    postback_payload: str | None = None
    postback_title: str | None = None
    profile_name: str | None = None


def _as_str(v) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def parse_messenger_payload(payload: dict) -> list[dict]:
    """Split a `page` webhook into per-event change dicts.

    Each returned change is ``{"kind", "page_id", "event"}``. Only genuine
    inbound user events are emitted; echoes of our own outbound messages and
    delivery/read receipts are skipped.
    """
    changes: list[dict] = []
    if _as_str(payload.get("object")) != "page":
        return changes
    for entry in _as_list(payload.get("entry")):
        entry = _as_dict(entry)
        page_id = _as_str(entry.get("id"))
        for event in _as_list(entry.get("messaging")):
            event = _as_dict(event)
            if "message" in event:
                if _as_dict(event.get("message")).get("is_echo"):
                    continue
                changes.append({"kind": "message", "page_id": page_id, "event": event})
            elif "postback" in event:
                changes.append({"kind": "postback", "page_id": page_id, "event": event})
            # delivery / read / reaction / other: ignored
    return changes


def parse_messaging_event(page_id: str, event: dict) -> ParsedMessengerMessage:
    """Turn one ``entry[].messaging[]`` item into a ParsedMessengerMessage."""
    event = _as_dict(event)
    sender = _as_dict(event.get("sender"))
    ts = event.get("timestamp")
    m = ParsedMessengerMessage(
        page_id=page_id,
        sender_id=_as_str(sender.get("id")),
        timestamp=ts if isinstance(ts, int) else None,
    )
    if "message" in event:
        msg = _as_dict(event.get("message"))
        m.mid = _as_str(msg.get("mid"))
        m.text = msg.get("text") if isinstance(msg.get("text"), str) else None
        m.is_echo = bool(msg.get("is_echo"))
        m.attachments = _as_list(msg.get("attachments"))
        qr = _as_dict(msg.get("quick_reply"))
        m.quick_reply_payload = _as_str(qr.get("payload")) or None
    elif "postback" in event:
        pb = _as_dict(event.get("postback"))
        m.mid = _as_str(pb.get("mid"))
        m.postback_payload = _as_str(pb.get("payload")) or None
        m.postback_title = _as_str(pb.get("title")) or None
    return m
