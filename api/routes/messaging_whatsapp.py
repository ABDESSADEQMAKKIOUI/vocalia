"""Public webhook endpoints for the WhatsApp (Meta Cloud API) channel.

These endpoints are called directly by Meta and are mounted without
authentication (like public_embed). Security is provided by:

- GET: constant-time match of hub.verify_token against the deployment's
  WHATSAPP_VERIFY_TOKEN (Meta's one-time subscription handshake).
- POST: X-Hub-Signature-256 HMAC verification of the raw body against the
  Meta app secret — either the deployment-level WHATSAPP_APP_SECRET or the
  app_secret stored (encrypted) on the messaging configuration that owns
  the business number the delivery is addressed to.

The POST handler stays thin and fast: verify, split into per-change work
items, enqueue to ARQ, return 200. All real processing happens in the
process_whatsapp_inbound task.
"""

import json
import os
import secrets

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger

from api.db import db_client
from api.services.messaging.whatsapp.client import verify_webhook_signature
from api.services.messaging.whatsapp.webhook_parser import parse_webhook_payload
from api.tasks.arq import enqueue_job
from api.tasks.function_names import FunctionNames
from api.utils.crypto import decrypt_credentials

router = APIRouter(prefix="/messaging/whatsapp", tags=["messaging"])


@router.get("/webhook")
async def verify_whatsapp_webhook(request: Request):
    """Answer Meta's webhook subscription handshake (hub.challenge echo)."""
    # hub.* aren't valid python identifiers, so read the raw query params.
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    # compare_digest on bytes: constant-time, and never raises on
    # attacker-controlled non-ASCII input (str form would TypeError).
    if (
        mode == "subscribe"
        and expected
        and token
        and secrets.compare_digest(token.encode(), expected.encode())
    ):
        return PlainTextResponse(challenge or "")

    logger.warning("Rejected WhatsApp webhook verification handshake")
    return Response(status_code=403)


@router.post("/webhook")
async def receive_whatsapp_webhook(request: Request):
    """Authenticate a webhook delivery and fan its changes out to ARQ."""
    # The signature covers the exact bytes Meta sent — read them first.
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")

    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        payload = {}
    changes = parse_webhook_payload(payload if isinstance(payload, dict) else {})

    app_secret = os.environ.get("WHATSAPP_APP_SECRET") or (
        await _resolve_app_secret(changes)
    )
    if not app_secret or not verify_webhook_signature(
        app_secret, raw_body, signature_header
    ):
        logger.warning("Rejected WhatsApp webhook: signature verification failed")
        return Response(status_code=403)

    for change in changes:
        # Voice calls are answered inline, not enqueued: the media rides a
        # WebRTC connection that lives in this (long-running) web process, and
        # Meta's answer window is short. A failure here must never stop the 200
        # Meta requires, so it is logged and swallowed.
        if change.get("kind") == "calls":
            try:
                from api.services.messaging.whatsapp.calls_service import (
                    handle_call_change,
                )

                await handle_call_change(change)
            except Exception:
                logger.exception(
                    f"Failed to handle WhatsApp call webhook "
                    f"(phone_number_id={change.get('phone_number_id')})"
                )
            continue

        try:
            await enqueue_job(FunctionNames.PROCESS_WHATSAPP_INBOUND, change)
        except Exception:
            logger.exception(
                f"Failed to enqueue WhatsApp webhook change "
                f"(kind={change.get('kind')}, "
                f"phone_number_id={change.get('phone_number_id')})"
            )

    # Meta expects a fast 200 on every authenticated delivery; anything else
    # triggers redelivery and, eventually, subscription disablement.
    return {"status": "ok"}


async def _resolve_app_secret(changes: list[dict]) -> str | None:
    """App secret of the configuration owning the delivery's business number.

    Uses the first change carrying a phone_number_id (all changes in one
    delivery belong to the same Meta app). The id is attacker-controlled
    until the HMAC check passes, so it is only used to select which stored
    secret to verify against — never trusted beyond that.
    """
    phone_number_id = next(
        (c.get("phone_number_id") for c in changes if c.get("phone_number_id")),
        None,
    )
    if not phone_number_id:
        return None

    address = await db_client.get_messaging_address_by_external_id(phone_number_id)
    if not address or not address.configuration:
        logger.warning(
            f"No active messaging address for phone_number_id={phone_number_id}"
        )
        return None

    try:
        credentials = decrypt_credentials(address.configuration.credentials or {})
    except ValueError:
        logger.exception(
            "Failed to decrypt messaging configuration credentials "
            f"(configuration_id={address.messaging_configuration_id})"
        )
        return None
    return credentials.get("app_secret") or None
