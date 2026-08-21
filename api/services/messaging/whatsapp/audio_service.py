"""WhatsApp voice-note support: inbound STT + outbound TTS via Gemini.

WhatsApp users often send voice notes instead of typing. Without handling
them the assistant only sees a placeholder and tells the user it "cannot
read audio". This module closes that gap:

* Inbound audio (voice notes and attached audio files) is transcribed to
  text with a Gemini multimodal model, so the existing text-chat turn
  machinery answers it exactly like a typed message.
* When the inbound message was audio, the assistant's text reply is
  synthesized back to a WhatsApp-compatible OGG/Opus voice note (falling
  back to a plain text reply on any failure).

Credentials and voice are the workflow's OWN configured Google (Gemini)
key and realtime voice — no new provider, key, or model is introduced.
Everything heavy (google-genai, ffmpeg) is imported/executed lazily and
off the event loop so importing this module stays cheap for the webhook
path.
"""

from __future__ import annotations

import asyncio
import subprocess

from loguru import logger

from api.db import db_client

# Transcription: the org's standard text model already answers turns, so it
# is guaranteed available for this key. TTS: Gemini's dedicated speech model.
_STT_MODEL = "gemini-3.5-flash"
_TTS_MODEL = "gemini-2.5-flash-preview-tts"
_DEFAULT_VOICE = "Aoede"

# Above this many characters a spoken reply is awkward and slow to
# synthesize, so we send it as text instead.
_MAX_TTS_CHARS = 2000


async def resolve_audio_ai(
    run_id: int, organization_id: int
) -> tuple[str | None, str]:
    """Return ``(google_api_key, tts_voice)`` for the run's workflow.

    Reuses the workflow's effective AI model configuration. The api_key is a
    direct Google (Gemini) key taken from the realtime config (preferred, it
    also carries the chosen voice) or the LLM config. Returns ``(None, voice)``
    when no direct Google key is configured (e.g. a proxy-only provider), in
    which case the caller keeps the text-only behaviour.
    """
    from api.services.configuration.ai_model_configuration import (
        get_effective_ai_model_configuration_for_workflow,
    )

    workflow_configurations: dict = {}
    org_id = organization_id
    try:
        run = await db_client.get_workflow_run_by_id(run_id)
        workflow = None
        if run is not None and getattr(run, "workflow_id", None):
            workflow = await db_client.get_workflow_by_id(run.workflow_id)
        if workflow is not None:
            workflow_configurations = workflow.workflow_configurations or {}
            org_id = workflow.organization_id or organization_id
    except Exception:
        logger.exception(
            f"WhatsApp audio: failed to load workflow config for run {run_id}"
        )

    cfg = await get_effective_ai_model_configuration_for_workflow(
        organization_id=org_id,
        workflow_configurations=workflow_configurations,
    )
    data = cfg.model_dump(exclude_none=True)
    realtime = data.get("realtime") or {}
    llm = data.get("llm") or {}

    voice = realtime.get("voice") or _DEFAULT_VOICE

    for section in (realtime, llm):
        provider = str(section.get("provider") or "").lower()
        api_key = section.get("api_key")
        if api_key and "google" in provider:
            return api_key, voice
    return None, voice


async def transcribe_audio(api_key: str, audio: bytes, mime_type: str | None) -> str:
    """Transcribe audio bytes to text with Gemini. Returns "" on failure."""

    def _run() -> str:
        from google import genai
        from google.genai import types

        clean_mime = (mime_type or "audio/ogg").split(";")[0].strip() or "audio/ogg"
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=_STT_MODEL,
            contents=[
                types.Part.from_bytes(data=audio, mime_type=clean_mime),
                "Transcris fidèlement cet audio dans sa langue d'origine. "
                "Réponds uniquement avec le texte transcrit, sans commentaire.",
            ],
        )
        return (resp.text or "").strip()

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        logger.exception("WhatsApp audio: transcription failed")
        return ""


async def synthesize_voice_note(api_key: str, text: str, voice: str) -> bytes:
    """Synthesize ``text`` to WhatsApp OGG/Opus voice-note bytes.

    Returns ``b""`` on any failure so the caller can fall back to text.
    """

    def _run() -> bytes:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=_TTS_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice or _DEFAULT_VOICE
                        )
                    )
                ),
            ),
        )
        part = resp.candidates[0].content.parts[0]
        pcm = part.inline_data.data
        # Gemini returns raw signed-16 LE mono PCM; the sample rate rides in
        # the mime (e.g. "audio/L16;codec=pcm;rate=24000").
        rate = 24000
        for token in (part.inline_data.mime_type or "").split(";"):
            token = token.strip()
            if token.startswith("rate="):
                try:
                    rate = int(token.split("=", 1)[1])
                except ValueError:
                    pass
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1",
            ],
            input=pcm,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "ffmpeg PCM->OGG failed: "
                + proc.stderr[-200:].decode("utf-8", "ignore")
            )
        return proc.stdout

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        logger.exception("WhatsApp audio: speech synthesis failed")
        return b""


async def send_assistant_reply(
    client,
    wa_id: str,
    assistant_text: str,
    *,
    reply_with_audio: bool,
    api_key: str | None,
    voice: str,
) -> str:
    """Send the assistant reply, as a voice note when the inbound was audio.

    Falls back to a plain text message when audio is disabled, unavailable,
    the reply is too long to speak, or synthesis/upload fails.
    """
    if (
        reply_with_audio
        and api_key
        and assistant_text
        and len(assistant_text) <= _MAX_TTS_CHARS
    ):
        ogg = await synthesize_voice_note(api_key, assistant_text, voice)
        if ogg:
            try:
                media_id = await client.upload_media(ogg, "audio/ogg")
                return await client.send_audio(to=wa_id, media_id=media_id)
            except Exception:
                logger.exception(
                    "WhatsApp audio: voice-note send failed; falling back to text"
                )
    return await client.send_text(to=wa_id, body=assistant_text)
