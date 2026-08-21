"""Inbound WhatsApp Business Calling — bridge Meta voice calls to the agent.

A WhatsApp user placing a voice call to a business number arrives as a
``calls`` webhook change (``field="calls"``, ``event="connect"``) carrying the
caller's SDP offer. This module answers the call and runs the same voice
pipeline the browser "web call" uses:

    Meta connect webhook (SDP offer)
      -> pipecat WhatsAppClient: negotiate WebRTC, pre_accept + accept
      -> resolve the business number -> its inbound workflow (the agent)
      -> create a workflow run + quota check
      -> run_pipeline_smallwebrtc(connection, ...) in the background

Media rides the aiortc peer connection (WebRTC / DTLS-SRTP), not a websocket,
so calls are driven inline from the webhook route (a long-lived FastAPI
process), not from an ARQ task. The webhook signature is already verified by
the route, so the pipecat client is built without a secret and does not
re-verify.

Termination: Meta sends a ``calls`` terminate change, or the agent hangs up
and the peer connection closes. A process-local registry keyed by call id lets
a terminate webhook close the right connection; with several web workers a
terminate may land on a different worker than the one holding the call, in
which case aiortc's own media timeout ends it. Cross-worker teardown is a
follow-up (M2).
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import aiohttp
from loguru import logger

from api.db import db_client
from api.enums import CallType, WorkflowRunMode
from api.services.pipecat.run_pipeline import run_pipeline_smallwebrtc
from api.services.quota_service import authorize_workflow_run_start
from api.utils.crypto import decrypt_credentials

# call_id -> the live WebRTC connection, so a terminate webhook can close it.
_LIVE_CALLS: dict[str, Any] = {}

# One shared HTTP session for the calling Graph API actions (pre_accept /
# accept / terminate). It must outlive a single webhook: the accepted call
# keeps running long after the connect webhook returns.
_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def handle_call_change(change: dict) -> None:
    """Handle one WhatsApp ``calls`` webhook change (connect or terminate).

    ``change`` is ``{"kind": "calls", "phone_number_id", "value"}`` as produced
    by ``webhook_parser.parse_webhook_payload``. The X-Hub-Signature-256 was
    already verified by the webhook route before this runs.
    """
    phone_number_id = change.get("phone_number_id")
    value = change.get("value") or {}
    calls = value.get("calls") or []
    if not phone_number_id or not calls:
        logger.warning("WhatsApp calls change without phone_number_id/calls; ignoring")
        return

    event = (calls[0] or {}).get("event")
    if event == "terminate":
        await _handle_terminate(calls)
    elif event == "connect":
        await _handle_connect(phone_number_id, value)
    else:
        logger.debug(f"Ignoring WhatsApp call event: {event!r}")


async def _handle_connect(phone_number_id: str, value: dict) -> None:
    address = await db_client.get_messaging_address_by_external_id(phone_number_id)
    if not address or not address.configuration:
        logger.warning(
            f"No messaging address/config for phone_number_id={phone_number_id}; "
            "cannot answer WhatsApp call"
        )
        return
    if not address.inbound_workflow_id:
        logger.warning(
            f"Messaging address {address.id} has no inbound workflow; "
            "rejecting WhatsApp call"
        )
        return

    workflow = await db_client.get_workflow(
        address.inbound_workflow_id, organization_id=address.organization_id
    )
    if workflow is None:
        logger.warning(
            f"Inbound workflow {address.inbound_workflow_id} not found for "
            f"org {address.organization_id}; cannot answer WhatsApp call"
        )
        return

    try:
        credentials = decrypt_credentials(address.configuration.credentials or {})
    except ValueError:
        logger.exception("Failed to decrypt WhatsApp credentials for calling")
        return
    token = credentials.get("access_token")
    if not token:
        logger.warning("WhatsApp configuration has no access_token; cannot answer call")
        return

    # Lazy imports: the pipecat calling transport and the route-level ICE
    # helper (keeps this module importable without pulling FastAPI at load).
    from pipecat.transports.whatsapp.api import (
        WhatsAppChange,
        WhatsAppEntry,
        WhatsAppWebhookRequest,
    )
    from pipecat.transports.whatsapp.client import WhatsAppClient

    from api.routes.webrtc_signaling import get_ice_servers

    session = await _get_session()
    client = WhatsAppClient(
        whatsapp_token=token,
        phone_number_id=phone_number_id,
        session=session,
        ice_servers=get_ice_servers(user_id=str(workflow.user_id)),
        whatsapp_secret=None,  # the webhook route already verified the signature
    )

    request = WhatsAppWebhookRequest(
        object="whatsapp_business_account",
        entry=[
            WhatsAppEntry(
                id="", changes=[WhatsAppChange(value=value, field="calls")]
            )
        ],
    )

    async def _on_connected(connection: Any, call: Any) -> None:
        await _start_agent(connection, call, workflow, address, phone_number_id)

    # pipecat negotiates WebRTC, pre-accepts + accepts the call, then calls back
    # with the live SmallWebRTCConnection and the caller metadata.
    await client.handle_webhook_request(request, connection_callback=_on_connected)


async def _start_agent(
    connection: Any, call: Any, workflow: Any, address: Any, phone_number_id: str
) -> None:
    """Run the agent on an accepted call (invoked by pipecat once media is up)."""
    call_id = call.id
    connection._pc_id = call_id

    workflow_run = await db_client.create_workflow_run(
        name=f"WR-WACALL-{uuid4().hex[:6]}",
        workflow_id=address.inbound_workflow_id,
        mode=WorkflowRunMode.WHATSAPP_CALL.value,
        user_id=workflow.user_id,
        call_type=CallType.INBOUND,
        initial_context={
            "provider": "whatsapp_call",
            "wa_id": call.from_,
            "phone_number": f"+{call.from_}",
            "phone_number_id": phone_number_id,
            "call_id": call_id,
        },
        organization_id=address.organization_id,
    )

    # Actor-less quota check, mirroring the WhatsApp text and campaign paths.
    quota_result = await authorize_workflow_run_start(
        workflow_id=address.inbound_workflow_id,
        organization_id=address.organization_id,
        workflow_run_id=workflow_run.id,
    )
    if not quota_result.has_quota:
        logger.warning(
            f"WhatsApp call quota check failed for run {workflow_run.id}: "
            f"{quota_result.error_message}"
        )
        await connection.disconnect()
        return

    _LIVE_CALLS[call_id] = connection

    @connection.event_handler("closed")
    async def _on_closed(_conn: Any) -> None:
        _LIVE_CALLS.pop(call_id, None)

    asyncio.create_task(
        run_pipeline_smallwebrtc(
            connection,
            address.inbound_workflow_id,
            workflow_run.id,
            workflow.user_id,
            organization_id=address.organization_id,
        )
    )
    logger.info(
        f"WhatsApp call {call_id} from {call.from_} answered -> run "
        f"{workflow_run.id} (workflow {address.inbound_workflow_id})"
    )


async def _handle_terminate(calls: list) -> None:
    for call in calls:
        call_id = (call or {}).get("id")
        connection = _LIVE_CALLS.pop(call_id, None) if call_id else None
        if connection is None:
            logger.debug(
                f"WhatsApp terminate for unknown/other-worker call {call_id}"
            )
            continue
        try:
            await connection.disconnect()
        except Exception:
            logger.exception(f"Failed to close WhatsApp call {call_id}")
        logger.info(f"WhatsApp call {call_id} terminated")
