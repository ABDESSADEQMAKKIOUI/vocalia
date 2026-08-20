import hashlib
import hmac
import os
import re
import tempfile
import time
import uuid
from typing import Annotated, Any, Dict, Optional, TypedDict
from urllib.parse import urlencode

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from api.constants import OSS_JWT_SECRET, PUBLIC_BASE_URL
from api.db import db_client
from api.enums import StorageBackend
from api.services.auth.depends import get_user
from api.services.storage import get_storage_for_backend, storage_fs


def _media_type(suffix: str) -> str:
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".txt": "text/plain; charset=utf-8",
        ".json": "application/json",
        ".csv": "text/csv",
    }.get(suffix.lower(), "application/octet-stream")


def _stream_signature(key: str, exp: int, backend: str) -> str:
    """HMAC over (key, expiry, backend) with the app's server secret.

    The stream endpoint has no session (an <audio> tag can't send a bearer
    token), so the signature IS the authorization: only /s3/signed-url, which
    checks the caller owns the resource, mints one. Binding the key means a
    valid link can't be repointed at another object; the expiry bounds it.
    """
    msg = f"{key}\n{exp}\n{backend}".encode("utf-8")
    return hmac.new(
        (OSS_JWT_SECRET or "").encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()


def _build_stream_url(
    key: str, expires_in: int, inline: bool, backend: str
) -> str:
    """A short-lived, self-authenticating URL to the streaming endpoint."""
    exp = int(time.time()) + int(expires_in)
    query = urlencode(
        {
            "key": key,
            "exp": exp,
            "inline": "1" if inline else "0",
            "backend": backend,
            "sig": _stream_signature(key, exp, backend),
        }
    )
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}/api/v1/s3/stream?{query}"


class S3SignedUrlResponse(TypedDict):
    url: str
    expires_in: int


class FileMetadataResponse(TypedDict):
    key: str
    metadata: Optional[Dict[str, Any]]


class PresignedUploadUrlRequest(BaseModel):
    file_name: str = Field(..., pattern=r".*\.csv$", description="CSV filename")
    file_size: int = Field(
        ..., gt=0, le=10_485_760, description="File size in bytes (max 10MB)"
    )
    content_type: str = Field(default="text/csv", description="File content type")


class PresignedUploadUrlResponse(BaseModel):
    upload_url: str
    file_key: str
    expires_in: int


router = APIRouter(prefix="/s3", tags=["s3"])


ORG_SCOPED_STORAGE_PREFIXES = ("campaigns", "knowledge_base")


def _extract_org_id_from_key(key: str) -> Optional[int]:
    """Try to extract an organization ID from a storage key.

    Matches known org-scoped keys of the form ``{prefix}/{org_id}/...`` where
    *org_id* is a positive integer. Returns ``None`` when the pattern does not
    match.
    """
    parts = key.split("/")
    if (
        len(parts) >= 3
        and parts[0] in ORG_SCOPED_STORAGE_PREFIXES
        and parts[1].isdigit()
    ):
        return int(parts[1])
    return None


def _extract_legacy_workflow_run_id(key: str) -> Optional[int]:
    """Extract a workflow_run_id from legacy key formats.

    Supports:
      - ``transcripts/{run_id}.txt``
      - ``recordings/{run_id}.wav``
      - ``recordings/{run_id}/user.wav``
      - ``recordings/{run_id}/bot.wav``

    Returns ``None`` when the key does not match a legacy pattern.
    """
    if key.startswith("transcripts/") and key.endswith(".txt"):
        run_id_str = key[len("transcripts/") : -4]
    else:
        recording_match = re.fullmatch(
            r"recordings/(\d+)(?:\.wav|/(?:user|bot)\.wav)", key
        )
        if not recording_match:
            return None
        run_id_str = recording_match.group(1)

    return int(run_id_str) if run_id_str.isdigit() else None


# Keep for backward compat with file-metadata endpoint
async def _validate_and_extract_workflow_run_id(
    key: str, allow_special_paths: bool = False
) -> Optional[int]:
    """Validate the S3 key format and extract workflow_run_id if present.

    Args:
        key: S3 object key
        allow_special_paths: If True, allows voicemail paths

    Returns:
        workflow_run_id if found, None for special paths (when allowed)

    Raises:
        HTTPException: If key format is invalid
    """
    if key.startswith("transcripts/") and key.endswith(".txt"):
        run_id_str = key[len("transcripts/") : -4]  # strip prefix & suffix
    elif key.startswith("recordings/"):
        run_id = _extract_legacy_workflow_run_id(key)
        if run_id is None:
            raise HTTPException(
                status_code=400, detail="Invalid workflow_run_id in key"
            )
        return run_id
    elif allow_special_paths and key.startswith("voicemail_detections/"):
        return None  # Skip validation for these paths
    else:
        raise HTTPException(status_code=400, detail="Invalid key format")

    if not run_id_str.isdigit():
        raise HTTPException(status_code=400, detail="Invalid workflow_run_id in key")

    return int(run_id_str)


async def _authorize_and_get_workflow_run(
    run_id: Optional[int], user, require_workflow_run: bool = True
) -> Optional[Any]:
    """Authorize access to workflow run and retrieve it.

    Args:
        run_id: Workflow run ID (can be None for special paths)
        user: Current user from auth
        require_workflow_run: If True, raises exception when run not found

    Returns:
        WorkflowRunModel or None

    Raises:
        HTTPException: If access is denied
    """
    if run_id is None:
        return None

    workflow_run = None
    if not user.is_superuser:
        # Regular users: Use organization_id to check access (security constraint)
        workflow_run = await db_client.get_workflow_run(
            run_id, organization_id=user.selected_organization_id
        )
        if not workflow_run and require_workflow_run:
            raise HTTPException(
                status_code=403, detail="Access denied for this workflow run"
            )
    else:
        # Superusers: Use get_workflow_run_by_id (no user/org constraint needed)
        workflow_run = await db_client.get_workflow_run_by_id(run_id)

    return workflow_run


@router.get(
    "/signed-url",
    response_model=S3SignedUrlResponse,
    summary="Generate a signed S3 URL",
)
async def get_signed_url(
    key: Annotated[str, Query(description="S3 object key")],
    expires_in: int = 3600,
    inline: bool = False,
    storage_backend: Annotated[
        Optional[str],
        Query(
            description="Storage backend to use (e.g. 'minio', 's3'). "
            "When omitted the backend is inferred from the resource."
        ),
    ] = None,
    user=Depends(get_user),
):
    """Return a short-lived signed URL for a file stored on S3 / MinIO.

    Access Control:
    * Known org-scoped keys (for example ``campaigns/{org_id}/...`` and
      ``knowledge_base/{org_id}/...``) are authorized by matching the org_id
      against the requesting user's organization.
    * Legacy keys (``recordings/{run_id}.wav``, ``transcripts/{run_id}.txt``)
      are authorized via the workflow run they belong to.
    * Superusers can request any key.
    """

    # ------------------------------------------------------------------
    # 1. Authorize
    # ------------------------------------------------------------------
    workflow_run = None

    org_id = _extract_org_id_from_key(key)
    if org_id is not None:
        # Generic org-based auth
        if not user.is_superuser and org_id != user.selected_organization_id:
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        # Legacy workflow-run-based auth
        run_id = _extract_legacy_workflow_run_id(key)
        if run_id is None:
            raise HTTPException(status_code=400, detail="Invalid key format")
        workflow_run = await _authorize_and_get_workflow_run(run_id, user)

    # ------------------------------------------------------------------
    # 2. Resolve storage backend
    # ------------------------------------------------------------------
    resolved_backend = ""
    if storage_backend:
        resolved_backend = storage_backend
    elif (
        workflow_run
        and hasattr(workflow_run, "storage_backend")
        and workflow_run.storage_backend
    ):
        resolved_backend = str(workflow_run.storage_backend)

    # ------------------------------------------------------------------
    # 3. Return a short-lived, self-authenticating URL to the stream
    #    endpoint. The object is served THROUGH the API (from the internal
    #    storage endpoint), so MinIO stays private and the anonymous-read
    #    bucket is never exposed to the internet.
    # ------------------------------------------------------------------
    url = _build_stream_url(key, expires_in, inline, resolved_backend)
    logger.info(f"Issued stream URL for key={key}, expires_in={expires_in}s")
    return {"url": url, "expires_in": expires_in}


@router.get("/stream", summary="Stream a stored file via a signed link")
async def stream_signed_file(
    key: Annotated[str, Query(description="S3 object key")],
    exp: Annotated[int, Query(description="Unix expiry timestamp")],
    sig: Annotated[str, Query(description="HMAC signature")],
    inline: bool = False,
    backend: str = "",
):
    """Serve a stored file if the signed link is valid and unexpired.

    No session: the signature (minted only by the authorized ``/signed-url``
    endpoint) is the authorization. The file is downloaded over the internal
    storage endpoint and returned with FileResponse, which supports Range
    requests so the browser audio player can seek.
    """
    if not hmac.compare_digest(_stream_signature(key, exp, backend), sig):
        raise HTTPException(status_code=403, detail="Invalid or tampered link")
    if exp < int(time.time()):
        raise HTTPException(status_code=403, detail="Link expired")

    try:
        storage = get_storage_for_backend(backend) if backend else storage_fs
    except ValueError:
        raise HTTPException(status_code=500, detail="Storage configuration error")

    suffix = os.path.splitext(key)[1] or ".bin"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="s3stream-")
    os.close(fd)
    try:
        ok = await storage.adownload_file(key, tmp_path)
    except Exception as exc:
        os.unlink(tmp_path)
        logger.error(f"Failed to read {key}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to read file")

    if not ok or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=404, detail="File not available")

    filename = os.path.basename(key)
    disposition = "inline" if inline else "attachment"
    return FileResponse(
        tmp_path,
        media_type=_media_type(suffix),
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
        background=BackgroundTask(os.unlink, tmp_path),
    )


@router.get(
    "/file-metadata",
    response_model=FileMetadataResponse,
    summary="Get file metadata for debugging",
)
async def get_file_metadata(
    key: Annotated[str, Query(description="S3 object key")],
    user=Depends(get_user),
):
    """Get file metadata including creation timestamp for debugging.

    Access Control:
    * Superusers can request any key.
    * Regular users can only request resources belonging to **their** workflow runs.
    """

    # Validate key and extract workflow_run_id (allow special paths for metadata)
    run_id = await _validate_and_extract_workflow_run_id(key, allow_special_paths=True)

    # Authorize and get workflow run (for special paths, run_id might be None)
    workflow_run = await _authorize_and_get_workflow_run(
        run_id, user, require_workflow_run=False
    )

    # ------------------------------------------------------------------
    # 3. Get file metadata using the correct storage backend
    # ------------------------------------------------------------------
    try:
        # Use the storage backend recorded when the file was uploaded
        if (
            workflow_run
            and hasattr(workflow_run, "storage_backend")
            and workflow_run.storage_backend
        ):
            backend = workflow_run.storage_backend
            storage = get_storage_for_backend(backend)
            logger.info(
                f"METADATA: Using stored {backend} for metadata request - key: {key}"
            )
        else:
            # Fallback to current storage for legacy records or voicemail files
            storage = storage_fs
            current_backend = StorageBackend.get_current_backend()
            logger.warning(
                f"METADATA: No storage_backend found, using current {current_backend.name} for metadata request - key: {key}"
            )

        metadata = await storage.aget_file_metadata(key)
        return {"key": key, "metadata": metadata}
    except Exception as exc:
        logger.error(f"Error getting file metadata: {exc}")
        raise HTTPException(status_code=500, detail="Failed to get file metadata")


@router.post(
    "/presigned-upload-url",
    response_model=PresignedUploadUrlResponse,
    summary="Generate a presigned URL for direct CSV upload",
)
async def get_presigned_upload_url(
    request: PresignedUploadUrlRequest,
    user=Depends(get_user),
):
    """Generate a presigned PUT URL for direct CSV file upload to S3/MinIO.

    This endpoint enables browser-to-storage uploads without passing through the backend

    Access Control:
    * All authenticated users can upload CSV files scoped to their organization.
    * Files are stored with organization-scoped keys for multi-tenancy.

    Returns:
    * upload_url: Presigned URL (valid for 15 minutes) for PUT request
    * file_key: Unique storage key to use as source_id in campaign creation
    * expires_in: URL expiration time in seconds
    """

    # Sanitize filename - remove special chars, keep only alphanumeric, dash, underscore, and dot
    sanitized_name = re.sub(r"[^a-zA-Z0-9._-]", "_", request.file_name)

    # Generate unique file key: campaigns/{org_id}/{uuid}_{filename}.csv
    file_key = (
        f"campaigns/{user.selected_organization_id}/{uuid.uuid4()}_{sanitized_name}"
    )

    try:
        # Generate presigned PUT URL using current storage backend
        upload_url = await storage_fs.aget_presigned_put_url(
            file_path=file_key,
            expiration=900,  # 15 minutes
            content_type=request.content_type,
            max_size=request.file_size,
        )

        if not upload_url:
            raise HTTPException(
                status_code=500, detail="Failed to generate presigned upload URL"
            )

        logger.info(
            f"Generated presigned upload URL for user {user.id}, org {user.selected_organization_id}, file_key: {file_key}"
        )

        return PresignedUploadUrlResponse(
            upload_url=upload_url,
            file_key=file_key,
            expires_in=900,
        )

    except Exception as exc:
        logger.error(f"Error generating presigned upload URL: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to generate presigned upload URL"
        )
