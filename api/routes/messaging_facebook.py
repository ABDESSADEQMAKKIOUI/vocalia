"""Public webhook endpoints for the Facebook Messenger channel.

Called directly by Meta, mounted without auth (like the WhatsApp webhook).
Security:

- GET: constant-time match of hub.verify_token against FACEBOOK_VERIFY_TOKEN
  (Meta's one-time `page` subscription handshake).
- POST: X-Hub-Signature-256 HMAC of the raw body against the Meta app secret
  (the same app as WhatsApp) — the deployment-level WHATSAPP_APP_SECRET or the
  app_secret stored (encrypted) on the messaging configuration owning the Page.

The POST handler stays thin: verify, split into per-event work items, enqueue
to ARQ, return 200. Real processing happens in process_facebook_inbound.
"""

import json
import os
import secrets

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger

from api.db import db_client
from api.services.messaging.facebook.webhook_parser import parse_messenger_payload
from api.services.messaging.whatsapp.client import verify_webhook_signature
from api.tasks.arq import enqueue_job
from api.tasks.function_names import FunctionNames
from api.utils.crypto import decrypt_credentials

router = APIRouter(prefix="/messaging/facebook", tags=["messaging"])

# Handshake token. Overridable via env; a fixed fallback keeps the webhook
# verifiable on this hot-deployed stack without an env change. This value is
# only a subscription handshake secret (not an access grant).
_DEFAULT_FB_VERIFY_TOKEN = "volira_fb_verify_2026"


@router.get("/webhook")
async def verify_facebook_webhook(request: Request):
    """Answer Meta's `page` webhook subscription handshake."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    expected = os.environ.get("FACEBOOK_VERIFY_TOKEN") or _DEFAULT_FB_VERIFY_TOKEN
    if (
        mode == "subscribe"
        and token
        and secrets.compare_digest(token.encode(), expected.encode())
    ):
        return PlainTextResponse(challenge or "")

    logger.warning("Rejected Facebook webhook verification handshake")
    return Response(status_code=403)


async def _resolve_app_secret(changes: list[dict]) -> str | None:
    """App secret of the configuration owning the delivery's Page id."""
    page_id = next((c.get("page_id") for c in changes if c.get("page_id")), None)
    if not page_id:
        return None
    address = await db_client.get_messaging_address_by_external_id(page_id)
    if not address or not address.configuration:
        logger.warning(f"No active messaging address for page_id={page_id}")
        return None
    try:
        credentials = decrypt_credentials(address.configuration.credentials or {})
    except ValueError:
        logger.exception(f"Failed to decrypt credentials for page_id={page_id}")
        return None
    return credentials.get("app_secret")


@router.post("/webhook")
async def receive_facebook_webhook(request: Request):
    """Authenticate a `page` webhook delivery and fan its events out to ARQ."""
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")

    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        payload = {}
    changes = parse_messenger_payload(payload if isinstance(payload, dict) else {})

    app_secret = os.environ.get("WHATSAPP_APP_SECRET") or (
        await _resolve_app_secret(changes)
    )
    if not app_secret or not verify_webhook_signature(
        app_secret, raw_body, signature_header
    ):
        logger.warning("Rejected Facebook webhook: signature verification failed")
        return Response(status_code=403)

    for change in changes:
        try:
            await enqueue_job(FunctionNames.PROCESS_FACEBOOK_INBOUND, change)
        except Exception:
            logger.exception(
                f"Failed to enqueue Facebook webhook change "
                f"(kind={change.get('kind')}, page_id={change.get('page_id')})"
            )

    # Meta expects a fast 200 on every authenticated delivery.
    return {"status": "ok"}
