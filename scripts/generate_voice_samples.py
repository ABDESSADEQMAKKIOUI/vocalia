"""Génère les échantillons audio des voix proposées à l'utilisateur.

Un utilisateur ne choisit plus qu'une langue et une voix (la configuration modèle
est un réglage administrateur). Pour choisir une voix, il doit pouvoir l'écouter.
Ce script produit ces extraits, un par couple (voix, langue), au chemin exact que
`api.services.configuration.voice_catalog` va inspecter :

    ui/public/voice-samples/google_realtime/<voix_en_minuscules>.<langue>.mp3

Usage :

    export GEMINI_API_KEY=...            # ou GOOGLE_API_KEY
    python -m scripts.generate_voice_samples --dry-run   # ce qui serait généré
    python -m scripts.generate_voice_samples             # génère ce qui manque
    python -m scripts.generate_voice_samples --force     # régénère tout

================================================================================
QUELLE API — DÉCISION ET JUSTIFICATION (à lire avant de la changer)
================================================================================

Google offre deux chemins pour faire parler « Puck », « Kore », etc. :

1. **Gemini Live** (`gemini-3.1-flash-live-preview`, websocket bidirectionnel).
   C'est EXACTEMENT ce que l'appel téléphonique utilise : même modèle, même
   `speech_config`, même `language_code`, même moteur audio natif
   (cf. `api/services/pipecat/service_factory.py` -> `DograhGeminiLiveLLMService`,
   et `pipecat/src/pipecat/services/google/gemini_live/llm.py`).

2. **Gemini TTS** (`gemini-2.5-flash-preview-tts`, `generateContent` one-shot).
   Même *inventaire de noms* de voix préconstruites, mais un AUTRE modèle : le
   rendu est proche, pas identique. Le texte, lui, est lu mot pour mot.

**Le défaut est Live**, parce que la promesse faite à l'utilisateur est « la voix
que tu écoutes est celle que ton client entendra ». Un échantillon produit par le
modèle TTS serait un à-peu-près, et un à-peu-près sur un timbre s'entend.

Ce que coûte ce choix, et qu'il faut assumer : Gemini Live est un modèle
conversationnel, pas un lecteur de texte. On lui demande (par `system_instruction`)
de répéter la phrase telle quelle, mais **rien ne garantit le mot à mot** : il peut
reformuler légèrement. Pour un échantillon de timbre c'est acceptable — et même
représentatif de ce que le modèle produit en appel. Si un jour le mot à mot devient
la contrainte (mention légale, script imposé), `--api tts` bascule sur le chemin
TTS : le texte sera exact, le timbre seulement approché. Les deux chemins ne
garantissent donc PAS la même chose, et c'est pour ça que le drapeau existe au lieu
d'un choix silencieux.

Note darija : le modèle TTS n'accepte pas de `language_code`, il déduit la langue
du texte. Le chemin Live, lui, envoie le `language_code` du catalogue — `ary` part
en `ar` (puis `ar-XA` après la table pipecat), exactement comme en appel. C'est une
raison de plus de préférer Live : l'échantillon darija reproduit le compromis réel
(réglage arabe standard + texte darija) au lieu de le contourner.

================================================================================
CE QUE CE SCRIPT NE FAIT PAS
================================================================================

Il n'invente rien. S'il n'obtient pas d'audio pour un couple (voix, langue), il
n'écrit PAS de fichier : pas de silence, pas de fichier vide. Le catalogue déduit
la disponibilité de la présence du fichier, donc un fichier muet se présenterait à
l'utilisateur comme un échantillon valide et le convaincrait que la voix est cassée.
Mieux vaut « échantillon indisponible » que « échantillon menteur ».
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

# Le script s'exécute depuis la racine du dépôt (`python -m scripts.…`), mais on
# accepte aussi `python scripts/generate_voice_samples.py` : sans ça l'import
# `api.…` échoue avec un ModuleNotFoundError obscur.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.services.configuration.options.google import (  # noqa: E402
    GOOGLE_REALTIME_MODELS,
)
from api.services.configuration.voice_catalog import (  # noqa: E402
    PROVIDER_GOOGLE_REALTIME,
    SAMPLE_AUDIO_EXTENSION,
    CatalogVoice,
    list_languages,
    list_voices,
    provider_language_code,
    sample_relative_path,
    samples_root,
)

# =============================================================================
# TEXTES PRONONCÉS — relisibles et corrigeables sans lire le reste du fichier.
# =============================================================================
#
# Une phrase courte (5 à 8 secondes), dans le registre d'un agent d'appel sortant :
# l'utilisateur doit entendre la voix dans la situation où il va s'en servir, pas
# lire un alphabet phonétique. Clé = code de langue *utilisateur* du catalogue.
#
# La ligne `ary` est de la VRAIE darija marocaine en caractères arabes
# (« ديال », « كنعيط ليك », « واش », « دابا »), pas de l'arabe standard déguisé :
# c'est tout l'intérêt d'avoir un échantillon `ary` distinct de `ar`. Si elle sonne
# faux à l'écoute, c'est cette chaîne qu'il faut corriger — rien d'autre.
SAMPLE_TEXTS: dict[str, str] = {
    "fr": (
        "Bonjour, je suis l'assistante virtuelle de Volira. "
        "Je vous appelle au sujet de votre demande. "
        "Est-ce que c'est le bon moment pour en parler ?"
    ),
    "en": (
        "Hello, I'm the virtual assistant from Volira. "
        "I'm calling about the request you sent us. "
        "Is now a good time to talk?"
    ),
    "ar": (
        "مرحباً، أنا المساعدة الافتراضية لشركة فوليرا. "
        "أتصل بك بخصوص الطلب الذي أرسلته إلينا. "
        "هل هذا وقت مناسب للحديث؟"
    ),
    "ary": (
        "السلام عليكم، أنا المساعدة الرقمية ديال فوليرا. "
        "كنعيط ليك على ود الطلب اللي صيفطي لينا. "
        "واش الوقت مناسب باش نهضرو دابا؟"
    ),
}

# Gemini Live répond, il ne lit pas. Cette consigne le pousse au mot à mot ; elle
# ne le garantit pas (voir l'en-tête du fichier).
LIVE_SYSTEM_INSTRUCTION = (
    "You are a text-to-speech engine. Read the user's message out loud, "
    "word for word, in the language it is written in. Do not translate it, "
    "do not answer it, do not add or remove anything, do not comment."
)

# =============================================================================
# MODÈLES ET CODES DE LANGUE
# =============================================================================

# Le modèle Live que la plateforme utilise réellement en appel : on le lit dans le
# registre plutôt que de le recopier, pour que le jour où il change l'échantillon
# généré change avec lui.
DEFAULT_LIVE_MODEL = GOOGLE_REALTIME_MODELS[0]

# Chemin de repli seulement (voir --api). Modèle TTS one-shot, sortie PCM.
DEFAULT_TTS_MODEL = "gemini-2.5-flash-preview-tts"

# Gemini Live attend du BCP-47, alors que la configuration de l'agent stocke un
# code ISO 639-1 (« fr »). La traduction est faite en appel par pipecat
# (`language_to_gemini_language`, pipecat/.../google/gemini_live/llm.py) ; on la
# reproduit ici à l'identique, sinon l'échantillon serait généré dans une locale
# que l'appel n'utilise jamais.
GEMINI_LIVE_LANGUAGE_CODES: dict[str, str] = {
    "fr": "fr-FR",
    "en": "en-US",
    "ar": "ar-XA",
}

# Sortie audio de Gemini (Live et TTS) : PCM 16 bits, mono, 24 kHz. La valeur est
# annoncée dans le `mime_type` de chaque réponse ; celle-ci n'est qu'un repli.
DEFAULT_SAMPLE_RATE = 24000
PCM_SAMPLE_WIDTH_BYTES = 2

# En dessous de ce seuil, ce n'est pas un échantillon : c'est un bout de silence ou
# une réponse tronquée. On refuse de l'écrire — un fichier présent vaut « voix
# disponible » pour le catalogue, donc écrire ça reviendrait à mentir à l'écoute.
MIN_SAMPLE_SECONDS = 0.8

STATUS_OK = "ok"
STATUS_EXISTING = "deja la"
STATUS_PLANNED = "a generer"
STATUS_FAILED = "echec"
STATUS_SKIPPED = "ignoree"


@dataclass
class SampleResult:
    """Une case du tableau final : ce couple (voix, langue) a donné quoi."""

    voice_id: str
    language_code: str
    status: str
    detail: str = ""
    path: Path | None = None


class SynthesisError(RuntimeError):
    """Échec de synthèse pour un couple (voix, langue), jamais fatal en soi."""


# =============================================================================
# CLÉ, ENCODEUR, CHEMINS
# =============================================================================


def resolve_api_key() -> str | None:
    """Clé Gemini, depuis GEMINI_API_KEY puis GOOGLE_API_KEY.

    Les deux noms circulent dans la doc Google et dans les déploiements ; refuser
    le second ferait perdre du temps à quelqu'un dont la clé est pourtant là.
    """
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.getenv(var)
        if value and value.strip():
            return value.strip()
    return None


def find_mp3_encoder() -> list[str] | None:
    """Commande capable d'encoder du PCM brut en MP3, ou None.

    Aucun encodeur MP3 utilisable n'existe en Python pur : on délègue à ffmpeg
    (ou à lame), qu'on cherche sur le PATH. C'est vérifié AVANT la première
    requête réseau — découvrir l'absence d'encodeur après vingt synthèses
    facturées serait une perte sèche.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return [ffmpeg]
    lame = shutil.which("lame")
    if lame:
        return [lame]
    return None


def target_path(
    voice: CatalogVoice, language_code: str, out_dir: Path, audio_format: str
) -> Path:
    """Chemin du fichier d'échantillon, dérivé du catalogue et non recalculé ici.

    On passe par `sample_relative_path()` pour que le nommage (minuscules,
    segments validés, ordre voix/langue) ait une seule implémentation : si le
    contrat bouge, il bouge des deux côtés en même temps.
    """
    relative = sample_relative_path(voice.id, language_code, voice.provider)
    if relative is None:
        raise SynthesisError(
            f"Nom de fichier impossible pour {voice.id}/{language_code} "
            "(identifiant non conforme à ^[a-z0-9_-]+$)"
        )
    if audio_format != SAMPLE_AUDIO_EXTENSION:
        relative = relative.rsplit(".", 1)[0] + f".{audio_format}"
    return out_dir.joinpath(*relative.split("/"))


def write_audio(
    pcm: bytes,
    destination: Path,
    sample_rate: int,
    audio_format: str,
    encoder: list[str] | None,
) -> None:
    """Écrit le PCM à `destination`, en MP3 ou en WAV, de façon atomique.

    Atomique parce que la disponibilité d'un échantillon se déduit de l'existence
    du fichier : un `ffmpeg` interrompu qui laisserait un MP3 tronqué sur le
    chemin final ferait apparaître dans l'interface un bouton lecture qui grésille.
    On écrit à côté, puis on renomme.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        if audio_format == "wav":
            with wave.open(str(temporary), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
                handle.setframerate(sample_rate)
                handle.writeframes(pcm)
        else:
            _encode_mp3(pcm, temporary, sample_rate, encoder)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _encode_mp3(
    pcm: bytes, destination: Path, sample_rate: int, encoder: list[str] | None
) -> None:
    if not encoder:
        raise SynthesisError("Aucun encodeur MP3 disponible (ffmpeg/lame introuvable)")
    program = Path(encoder[0]).stem.lower()
    if program == "lame":
        command = [
            *encoder,
            "-r",  # entrée PCM brute
            "-s",
            str(sample_rate / 1000),
            "--bitwidth",
            str(PCM_SAMPLE_WIDTH_BYTES * 8),
            "-m",
            "m",  # mono
            "--silent",
            "-",
            str(destination),
        ]
    else:
        command = [
            *encoder,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-i",
            "-",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "4",
            # Format de sortie explicite : on écrit dans un fichier temporaire
            # « …mp3.part » (voir write_audio), et ffmpeg déduit sinon le format
            # de l'extension — il refuserait « .part ».
            "-f",
            "mp3",
            str(destination),
        ]
    process = subprocess.run(command, input=pcm, capture_output=True, check=False)
    if process.returncode != 0:
        stderr = process.stderr.decode("utf-8", "replace").strip()
        raise SynthesisError(f"Encodage MP3 échoué ({program}) : {stderr[:300]}")


def sample_rate_from_mime_type(mime_type: str | None) -> int:
    """Lit `rate=24000` dans un mime type `audio/L16;codec=pcm;rate=24000`.

    Le taux est annoncé par la réponse ; le déduire évite un échantillon lu trop
    vite ou trop lentement le jour où Google change la cadence de sortie.
    """
    if not mime_type:
        return DEFAULT_SAMPLE_RATE
    for chunk in mime_type.split(";"):
        key, _, value = chunk.strip().partition("=")
        if key.strip().lower() == "rate":
            try:
                return int(value)
            except ValueError:
                break
    return DEFAULT_SAMPLE_RATE


# =============================================================================
# SYNTHÈSE
# =============================================================================


def load_genai():
    """Importe le SDK Google au dernier moment, avec un message utile s'il manque.

    Import tardif volontaire : `--help` et `--dry-run` doivent fonctionner sur une
    machine où le SDK n'est pas installé (c'est le cas de la plupart des postes,
    le backend ne l'a qu'en dépendance transitive de pipecat).
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - dépend de l'environnement
        raise SystemExit(
            "Le SDK Google GenAI est absent.\n"
            "  pip install google-genai\n"
            f"(détail : {exc})"
        ) from exc
    return genai, types


async def synthesize_live(
    client,
    types,
    model: str,
    voice_id: str,
    language_code: str,
    text: str,
    timeout: float,
) -> tuple[bytes, int]:
    """Une session Gemini Live, un tour de parole, l'audio récupéré.

    C'est le chemin fidèle : mêmes modèle, voix et `language_code` qu'en appel.
    """
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_id)
            ),
            language_code=language_code,
        ),
        system_instruction=types.Content(
            role="user", parts=[types.Part(text=LIVE_SYSTEM_INSTRUCTION)]
        ),
    )

    chunks: list[bytes] = []
    sample_rate = DEFAULT_SAMPLE_RATE

    async def _run() -> None:
        nonlocal sample_rate
        async with client.aio.live.connect(model=model, config=config) as session:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]),
                turn_complete=True,
            )
            async for message in session.receive():
                server_content = getattr(message, "server_content", None)
                if server_content is None:
                    continue
                model_turn = getattr(server_content, "model_turn", None)
                if model_turn and model_turn.parts:
                    for part in model_turn.parts:
                        inline = getattr(part, "inline_data", None)
                        if inline is not None and inline.data:
                            chunks.append(inline.data)
                            sample_rate = sample_rate_from_mime_type(inline.mime_type)
                # `turn_complete` est la seule fin de tour fiable : sans lui on
                # resterait bloqué sur un flux qui n'a plus rien à dire.
                if getattr(server_content, "turn_complete", False):
                    break

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise SynthesisError(f"Aucune fin de tour après {timeout:.0f}s") from exc

    return b"".join(chunks), sample_rate


async def synthesize_tts(
    client,
    types,
    model: str,
    voice_id: str,
    language_code: str,
    text: str,
    timeout: float,
) -> tuple[bytes, int]:
    """Repli one-shot par les modèles TTS Gemini.

    `language_code` n'est pas envoyé : ces modèles déduisent la langue du texte.
    Le paramètre reste dans la signature pour que les deux moteurs soient
    interchangeables à l'appel.
    """
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_id)
            )
        ),
    )
    response = await asyncio.wait_for(
        client.aio.models.generate_content(model=model, contents=text, config=config),
        timeout=timeout,
    )

    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and inline.data:
                return inline.data, sample_rate_from_mime_type(inline.mime_type)
    raise SynthesisError("Réponse sans audio (aucune partie inline_data)")


def looks_like_unsupported_voice(message: str) -> bool:
    """Cette erreur dit-elle « cette voix n'existe pas sur ce modèle » ?

    Découvrir quelles voix répondent vraiment est une des raisons d'être de ce
    script. Quand une voix est refusée pour ce qu'elle est, réessayer dans les
    trois autres langues ne fait que payer trois fois la même réponse : on saute
    le reste de la voix. Le filtre est volontairement étroit (400 / argument
    invalide / mention de la voix) pour qu'une panne passagère — 429, 503 — ne
    condamne pas une voix parfaitement valide.
    """
    lowered = message.lower()
    signals = ("invalid_argument", "400", "not found", "unsupported", "voice")
    return any(signal in lowered for signal in signals)


# =============================================================================
# PLAN, EXÉCUTION, RAPPORT
# =============================================================================


def build_plan(
    voice_filter: set[str], language_filter: set[str]
) -> list[tuple[CatalogVoice, str]]:
    """Les couples (voix, langue) à traiter, dans l'ordre d'affichage du catalogue."""
    plan: list[tuple[CatalogVoice, str]] = []
    for voice in list_voices(PROVIDER_GOOGLE_REALTIME):
        if voice_filter and voice.id.lower() not in voice_filter:
            continue
        for language_code in voice.offered_languages:
            if language_filter and language_code not in language_filter:
                continue
            plan.append((voice, language_code))
    return plan


async def run_generation(args: argparse.Namespace) -> list[SampleResult]:
    genai, types = load_genai()
    client = genai.Client(api_key=resolve_api_key())
    synthesize = synthesize_live if args.api == "live" else synthesize_tts
    model = args.model or (
        DEFAULT_LIVE_MODEL if args.api == "live" else DEFAULT_TTS_MODEL
    )
    encoder = find_mp3_encoder() if args.format == "mp3" else None
    out_dir = Path(args.out_dir) if args.out_dir else samples_root()

    results: list[SampleResult] = []
    # Voix déjà disqualifiées par une erreur qui les concerne elles, pas la langue.
    dead_voices: dict[str, str] = {}
    first_call = True

    for voice, language_code in build_plan(set(args.voice), set(args.language)):
        if voice.id in dead_voices:
            results.append(
                SampleResult(
                    voice.id,
                    language_code,
                    STATUS_SKIPPED,
                    "sautée après le premier échec de cette voix : "
                    f"{dead_voices[voice.id]}",
                )
            )
            continue

        try:
            destination = target_path(voice, language_code, out_dir, args.format)
        except SynthesisError as exc:
            results.append(
                SampleResult(voice.id, language_code, STATUS_FAILED, str(exc))
            )
            continue

        if destination.exists() and not args.force:
            results.append(
                SampleResult(voice.id, language_code, STATUS_EXISTING, "", destination)
            )
            continue

        provider_code = provider_language_code(language_code)
        api_language = GEMINI_LIVE_LANGUAGE_CODES.get(
            provider_code or "", provider_code or "en-US"
        )
        text = SAMPLE_TEXTS.get(language_code)
        if not text:
            results.append(
                SampleResult(
                    voice.id,
                    language_code,
                    STATUS_FAILED,
                    f"aucun texte défini pour « {language_code} » (voir SAMPLE_TEXTS)",
                )
            )
            continue

        # Une pause entre deux synthèses : les quotas Gemini se comptent à la
        # minute, et vingt sessions Live d'affilée les épuisent pour rien.
        if not first_call and args.delay > 0:
            await asyncio.sleep(args.delay)
        first_call = False

        print(f"  … {voice.id} / {language_code}", flush=True)
        try:
            pcm, sample_rate = await synthesize(
                client, types, model, voice.id, api_language, text, args.timeout
            )
            minimum_bytes = int(
                MIN_SAMPLE_SECONDS * sample_rate * PCM_SAMPLE_WIDTH_BYTES
            )
            if len(pcm) < minimum_bytes:
                raise SynthesisError(
                    f"audio trop court ({len(pcm)} octets < {minimum_bytes}) — "
                    "rien écrit, un échantillon muet est pire que pas d'échantillon"
                )
            write_audio(pcm, destination, sample_rate, args.format, encoder)
            results.append(
                SampleResult(voice.id, language_code, STATUS_OK, "", destination)
            )
        except Exception as exc:  # noqa: BLE001 - une voix qui tombe n'arrête pas les autres
            message = f"{type(exc).__name__}: {exc}".strip()
            results.append(
                SampleResult(voice.id, language_code, STATUS_FAILED, message)
            )
            if looks_like_unsupported_voice(message):
                dead_voices[voice.id] = message[:120]

    return results


def dry_run(args: argparse.Namespace) -> list[SampleResult]:
    out_dir = Path(args.out_dir) if args.out_dir else samples_root()
    results: list[SampleResult] = []
    for voice, language_code in build_plan(set(args.voice), set(args.language)):
        try:
            destination = target_path(voice, language_code, out_dir, args.format)
        except SynthesisError as exc:
            results.append(
                SampleResult(voice.id, language_code, STATUS_FAILED, str(exc))
            )
            continue
        if destination.exists() and not args.force:
            status, detail = STATUS_EXISTING, ""
        elif language_code not in SAMPLE_TEXTS:
            status, detail = STATUS_FAILED, "aucun texte défini (voir SAMPLE_TEXTS)"
        else:
            status, detail = STATUS_PLANNED, ""
        results.append(
            SampleResult(voice.id, language_code, status, detail, destination)
        )
    return results


def print_report(results: list[SampleResult], out_dir: Path, dry: bool) -> None:
    """Tableau voix × langue, puis le détail des échecs.

    Le tableau sert à voir d'un coup d'œil ce qui manque ; le détail sert à savoir
    pourquoi. Les deux sont nécessaires : « Fenrir a échoué » n'apprend rien.
    """
    languages = [language.code for language in list_languages()]
    voices_seen = list(dict.fromkeys(result.voice_id for result in results))
    by_key = {(r.voice_id, r.language_code): r for r in results}

    width = max([len("Voix")] + [len(v) for v in voices_seen]) if voices_seen else 4
    cell = max(len(STATUS_PLANNED), len(STATUS_EXISTING), len(STATUS_SKIPPED)) + 1

    print()
    print(f"Échantillons — {'simulation' if dry else 'génération'} — {out_dir}")
    header = (
        "Voix".ljust(width) + " | " + " | ".join(code.ljust(cell) for code in languages)
    )
    print(header)
    print("-" * len(header))
    for voice_id in voices_seen:
        row = [voice_id.ljust(width)]
        for code in languages:
            result = by_key.get((voice_id, code))
            row.append((result.status if result else "-").ljust(cell))
        print(" | ".join(row))

    failures = [r for r in results if r.status == STATUS_FAILED]
    skipped = [r for r in results if r.status == STATUS_SKIPPED]
    counts = {
        status: sum(1 for r in results if r.status == status)
        for status in (
            STATUS_OK,
            STATUS_EXISTING,
            STATUS_PLANNED,
            STATUS_FAILED,
            STATUS_SKIPPED,
        )
    }
    summary = ", ".join(
        f"{count} {status}" for status, count in counts.items() if count
    )
    print()
    print(f"Bilan : {summary or 'rien à faire'}")

    if failures or skipped:
        print()
        print("Détail :")
        for result in failures + skipped:
            print(f"  - {result.voice_id} / {result.language_code} : {result.detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_voice_samples",
        description=(
            "Génère les échantillons audio des voix Gemini Live proposées à "
            "l'utilisateur, au chemin attendu par le catalogue de voix."
        ),
        epilog=(
            "Clé lue dans GEMINI_API_KEY ou GOOGLE_API_KEY. "
            "Dossier de sortie : VOICE_SAMPLES_DIR, sinon ui/public/voice-samples."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--api",
        choices=("live", "tts"),
        default="live",
        help=(
            "live (défaut) : le modèle utilisé en appel, timbre fidèle, texte non "
            "garanti mot pour mot. tts : texte exact, timbre seulement approché."
        ),
    )
    parser.add_argument("--model", help="Force le modèle (défaut : celui du registre).")
    parser.add_argument(
        "--format",
        choices=("mp3", "wav"),
        default=SAMPLE_AUDIO_EXTENSION,
        help="mp3 (défaut, exige ffmpeg ou lame) ou wav (aucune dépendance).",
    )
    parser.add_argument(
        "--voice",
        action="append",
        default=[],
        metavar="ID",
        help="Limite à une voix (répétable, insensible à la casse). Ex. --voice puck",
    )
    parser.add_argument(
        "--language",
        action="append",
        default=[],
        metavar="CODE",
        help="Limite à une langue du catalogue (répétable). Ex. --language ary",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Régénère même les échantillons déjà présents.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Liste ce qui serait généré, sans appel réseau ni écriture.",
    )
    parser.add_argument(
        "--out-dir", help="Dossier de sortie (défaut : celui du catalogue)."
    )
    parser.add_argument(
        "--timeout", type=float, default=90.0, help="Délai max par échantillon (s)."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Pause entre deux synthèses, pour ménager les quotas (s).",
    )
    return parser


def validate_filters(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Rejette un filtre qui ne correspond à rien plutôt que de ne rien générer.

    Une faute de frappe (`--voice puk`) produirait sinon un tableau vide et un
    code de sortie 0 : l'opérateur croirait le travail fait.
    """
    args.voice = [value.lower() for value in args.voice]
    known_voices = {voice.id.lower() for voice in list_voices(PROVIDER_GOOGLE_REALTIME)}
    unknown = sorted(set(args.voice) - known_voices)
    if unknown:
        parser.error(
            f"voix inconnue(s) : {', '.join(unknown)} — connues : "
            f"{', '.join(sorted(known_voices))}"
        )
    known_languages = {language.code for language in list_languages()}
    unknown = sorted(set(args.language) - known_languages)
    if unknown:
        parser.error(
            f"langue(s) inconnue(s) : {', '.join(unknown)} — connues : "
            f"{', '.join(sorted(known_languages))}"
        )


def main(argv: list[str] | None = None) -> int:
    # Le rapport contient des accents (et le catalogue des libellés arabes) : sur
    # une console Windows en cp1252 un caractère non représentable ferait planter
    # l'affichage d'un travail par ailleurs réussi.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    validate_filters(args, parser)

    out_dir = Path(args.out_dir) if args.out_dir else samples_root()

    if args.dry_run:
        # Aucun appel, aucune écriture — mais on signale quand même les obstacles
        # qui feraient échouer la vraie exécution, c'est le moment utile pour ça.
        if not resolve_api_key():
            print(
                "Note : GEMINI_API_KEY / GOOGLE_API_KEY absente (sans effet en simulation)."
            )
        if args.format == "mp3" and not find_mp3_encoder():
            print("Note : ni ffmpeg ni lame sur le PATH — l'encodage MP3 échouerait.")
        print_report(dry_run(args), out_dir, dry=True)
        return 0

    # Contrôles préalables : on refuse de partir plutôt que d'échouer vingt fois.
    if not resolve_api_key():
        print(
            "Aucune clé Gemini.\n"
            "  export GEMINI_API_KEY=...   (ou GOOGLE_API_KEY)\n"
            "La clé se crée sur https://aistudio.google.com/apikey.\n"
            "Sans elle aucune synthèse n'est possible : relancez ensuite cette commande, "
            "elle ne régénère que ce qui manque.",
            file=sys.stderr,
        )
        return 2

    if args.format == "mp3" and not find_mp3_encoder():
        print(
            "Encodage MP3 impossible : ni ffmpeg ni lame sur le PATH.\n"
            "  - installez ffmpeg, ou\n"
            "  - relancez avec --format wav — mais dans ce cas SAMPLE_AUDIO_EXTENSION\n"
            "    (api/services/configuration/voice_catalog.py) doit passer à « wav »,\n"
            "    sinon l'API cherchera des .mp3 et déclarera les voix indisponibles.",
            file=sys.stderr,
        )
        return 2

    if args.format != SAMPLE_AUDIO_EXTENSION:
        print(
            f"ATTENTION : format « {args.format} » alors que le catalogue attend "
            f"« {SAMPLE_AUDIO_EXTENSION} ». Les fichiers générés ne seront PAS vus "
            "par l'API tant que SAMPLE_AUDIO_EXTENSION n'est pas aligné.",
            file=sys.stderr,
        )

    print(
        f"Moteur : {args.api} — modèle : "
        f"{args.model or (DEFAULT_LIVE_MODEL if args.api == 'live' else DEFAULT_TTS_MODEL)}"
    )
    results = asyncio.run(run_generation(args))
    print_report(results, out_dir, dry=False)

    # Code de sortie non nul dès qu'un échantillon manque : ce script est fait pour
    # être enchaîné, et « tout va bien » doit vouloir dire que tout est là.
    failed = any(r.status in (STATUS_FAILED, STATUS_SKIPPED) for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
