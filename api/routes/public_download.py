"""Public download endpoints for workflow recordings and transcripts.

These endpoints provide secure, token-based public access to workflow artifacts
without requiring authentication. Tokens are generated on-demand during
post-call processing for runs that execute integrations, QA, or campaign
reporting.

The artifact is streamed THROUGH this endpoint (token-gated), not handed off as
a redirect. The bundled MinIO is reachable only on the host's loopback and its
public URL points at an internal address, so a redirect sent a browser to a
host it cannot reach; and the voice-audio bucket is anonymous-read, so exposing
it directly would publish every call recording at a guessable path
(recordings/<id>.wav). Reading the object over the internal endpoint and serving
it with FileResponse keeps the token as the only way in and gives the browser a
real file — with Range support, so the audio player can seek.
"""

import os
import tempfile

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from loguru import logger
from starlette.background import BackgroundTask

from api.db import db_client
from api.services.storage import get_storage_for_backend
from api.utils.recording_artifacts import (
    get_recording_storage_backend,
    get_recording_storage_key,
)

router = APIRouter(prefix="/public/download")


def _media_type(suffix: str) -> str:
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".txt": "text/plain; charset=utf-8",
        ".json": "application/json",
    }.get(suffix.lower(), "application/octet-stream")


@router.get("/workflow/{token}/{artifact_type}")
async def download_workflow_artifact(
    token: str,
    artifact_type: str,
    inline: bool = Query(
        default=False, description="Display inline in browser instead of download"
    ),
):
    """Stream a workflow recording or transcript via public access token.

    Args:
        token: The public access token (UUID format)
        artifact_type: "recording", "transcript", "user_recording", or
            "bot_recording"
        inline: If true, Content-Disposition is inline (browser preview / audio
            player); otherwise attachment (download).

    Raises:
        HTTPException 400: unsupported artifact type
        HTTPException 404: invalid token or artifact missing
        HTTPException 500: storage error
    """
    # 1. Lookup workflow run by token
    workflow_run = await db_client.get_workflow_run_by_public_token(token)
    if not workflow_run:
        logger.warning(f"Invalid public access token: {token[:8]}...")
        raise HTTPException(status_code=404, detail="Invalid or expired token")

    # 2. Resolve the storage key for the requested artifact
    artifact_storage_backend = None
    if artifact_type == "recording":
        file_path = workflow_run.recording_url
    elif artifact_type == "transcript":
        file_path = workflow_run.transcript_url
    elif artifact_type == "user_recording":
        file_path = get_recording_storage_key(workflow_run.extra, "user")
        artifact_storage_backend = get_recording_storage_backend(
            workflow_run.extra, "user"
        )
    elif artifact_type == "bot_recording":
        file_path = get_recording_storage_key(workflow_run.extra, "bot")
        artifact_storage_backend = get_recording_storage_backend(
            workflow_run.extra, "bot"
        )
    else:
        logger.warning(
            f"Unsupported artifact type: type={artifact_type}, "
            f"workflow_run_id={workflow_run.id}"
        )
        raise HTTPException(status_code=400, detail="Unsupported artifact type")

    if not file_path:
        logger.warning(
            f"Artifact not found: type={artifact_type}, "
            f"workflow_run_id={workflow_run.id}"
        )
        raise HTTPException(
            status_code=404,
            detail=f"No {artifact_type} available for this workflow run",
        )

    # 3. Storage backend for this run
    try:
        storage = get_storage_for_backend(
            artifact_storage_backend or workflow_run.storage_backend
        )
    except ValueError:
        logger.error(f"Invalid storage backend: {workflow_run.storage_backend}")
        raise HTTPException(status_code=500, detail="Storage configuration error")

    # 4. Pull the object to a temp file over the INTERNAL endpoint, then serve it.
    #    FileResponse handles Range requests (audio seeking) and Content-Length;
    #    the temp file is deleted after the response is sent.
    suffix = os.path.splitext(file_path)[1] or (
        ".wav" if "recording" in artifact_type else ".txt"
    )
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="artifact-")
    os.close(fd)
    try:
        ok = await storage.adownload_file(file_path, tmp_path)
    except Exception as e:
        os.unlink(tmp_path)
        logger.error(f"Failed to read artifact {file_path}: {e}")
        raise HTTPException(status_code=500, detail="Failed to read artifact")

    if not ok or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        logger.error(
            f"Artifact empty or unreadable: type={artifact_type}, path={file_path}, "
            f"workflow_run_id={workflow_run.id}"
        )
        raise HTTPException(status_code=404, detail="Artifact not available")

    filename = f"{artifact_type}-{workflow_run.id}{suffix}"
    disposition = "inline" if inline else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}

    logger.info(
        f"Serving {artifact_type} for workflow_run_id={workflow_run.id}, "
        f"token={token[:8]}..., {os.path.getsize(tmp_path)} bytes"
    )
    return FileResponse(
        tmp_path,
        media_type=_media_type(suffix),
        headers=headers,
        background=BackgroundTask(os.unlink, tmp_path),
    )
