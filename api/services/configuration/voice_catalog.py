"""Catalogue des voix proposées à l'utilisateur final.

La configuration modèle (fournisseur, modèle, clés) est un réglage administrateur.
L'utilisateur, lui, ne choisit que deux choses : une **langue** et une **voix**.
Ce module est la source de vérité de ce qu'on lui propose.

Pour l'instant, seules les voix Gemini Live (`google_realtime`) sont exposées.

Deux règles de conception, parce qu'elles sont la raison d'être de ce fichier :

1. **Ajouter une voix doit être une ligne de données.** Gemini Live publie plus de
   cinq voix selon le modèle, mais on ne déclare ici que celles que le dépôt connaît
   déjà (`GOOGLE_REALTIME_VOICES`). Le script de génération d'échantillons est ce qui
   prouvera lesquelles répondent réellement — on n'invente pas une liste de noms
   qu'on ne peut pas vérifier.

2. **La disponibilité d'un échantillon est déduite du disque, jamais déclarée.**
   Un booléen écrit à la main dans les données mentirait dès qu'un fichier manque
   ou n'a pas encore été généré. `sample_url()` regarde si le fichier attendu est
   présent ; sinon elle renvoie None, et l'interface doit dire « échantillon
   indisponible » plutôt que d'afficher un bouton lecture qui ne produit rien.

Schéma de nommage des fichiers d'échantillon (contrat partagé — le script de
génération et l'interface doivent en déduire exactement le même chemin) :

    ui/public/voice-samples/<provider>/<voice_id>.<language_code>.<ext>

    - tout en minuscules ASCII ;
    - `<provider>` : l'identifiant fournisseur du catalogue, ex. `google_realtime` ;
    - `<voice_id>` : l'id de la voix en minuscules, ex. `puck` (l'API Google, elle,
      attend la casse d'origine : « Puck ») ;
    - `<language_code>` : le code **présenté à l'utilisateur**, donc `ary` et `ar`
      donnent bien deux fichiers distincts (voir la note darija plus bas) ;
    - `<ext>` : `mp3`.

    Exemples : `google_realtime/puck.fr.mp3`, `google_realtime/kore.ary.mp3`.

Next sert `ui/public/` à la racine du site, donc l'URL publique correspondante est
`/voice-samples/google_realtime/puck.fr.mp3`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from api.schemas.voice_catalog import (
    VoiceCatalogResponse,
    VoiceLanguageOption,
    VoiceOption,
    VoiceSample,
)
from api.services.configuration.options.google import GOOGLE_REALTIME_VOICES

# Identifiant fournisseur du catalogue. Il coïncide volontairement avec
# ServiceProviders.GOOGLE_REALTIME du registre : c'est ce provider que la
# configuration admin utilise, et c'est aussi le nom du dossier d'échantillons.
PROVIDER_GOOGLE_REALTIME = "google_realtime"

SAMPLE_AUDIO_EXTENSION = "mp3"
# Préfixe de l'URL publique servie par Next (contenu de `ui/public/`).
SAMPLE_URL_PREFIX = "/voice-samples"
# Chemin du dossier d'échantillons, relatif à la racine du dépôt.
SAMPLES_DIR_RELATIVE_TO_REPO = Path("ui") / "public" / "voice-samples"

# Le conteneur API ne contient pas `ui/` (son image ne copie que `api/` et `docs/`).
# Cette variable d'environnement permet donc de pointer vers le dossier monté à
# côté, sans quoi l'API répondra « aucun échantillon » alors que Next les sert.
SAMPLES_DIR_ENV_VAR = "VOICE_SAMPLES_DIR"

# api/services/configuration/voice_catalog.py -> configuration -> services -> api -> dépôt
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Un id de voix / code de langue ne doit jamais pouvoir fabriquer un chemin qui
# sorte du dossier d'échantillons. Les valeurs viennent du catalogue (donc du code),
# mais une future ligne de données mal formée ne doit pas devenir une traversée.
_SAFE_SEGMENT = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class CatalogLanguage:
    """Une langue proposée à l'utilisateur.

    `code` est ce que l'utilisateur choisit et ce qui nomme les échantillons ;
    `provider_code` est ce qu'on envoie réellement à l'API. Les deux diffèrent
    pour la darija (voir CATALOG_LANGUAGES).
    """

    code: str
    provider_code: str
    label: str
    native_label: str
    # True quand `provider_code` n'est qu'une approximation du dialecte demandé :
    # le rendu doit être écouté avant d'être promis à un client.
    is_approximated: bool = False
    note: str | None = None


@dataclass(frozen=True)
class CatalogVoice:
    """Une voix proposée à l'utilisateur.

    `id` est le nom exact attendu par l'API Google (« Puck », « Charon »…) :
    on le transmet tel quel dans la configuration, casse comprise.
    `offered_languages` liste les langues pour lesquelles on veut un échantillon ;
    savoir s'il existe vraiment est une question de disque, pas de déclaration.
    """

    id: str
    display_name: str
    description: str
    provider: str = PROVIDER_GOOGLE_REALTIME
    offered_languages: tuple[str, ...] = field(default=())


# Les quatre langues proposées pour l'instant, dans l'ordre d'affichage.
#
# NOTE DARIJA — le point à ne pas perdre : Gemini Live n'a PAS de locale darija.
# `ary` est le code ISO 639-3 de l'arabe marocain, mais côté API la langue envoyée
# reste `ar` : la darija se rend en faisant prononcer du *texte* darija avec le
# réglage arabe. D'où deux entrées distinctes ici avec le même `provider_code`,
# et deux fichiers d'échantillon distincts (`*.ar.mp3` et `*.ary.mp3`) puisque ce
# qui change est le texte prononcé, pas le réglage. Tant que ces échantillons
# n'ont pas été écoutés, le rendu darija n'est pas garanti — c'est ce que dit
# `is_approximated` / `note`, et l'interface doit le montrer.
CATALOG_LANGUAGES: tuple[CatalogLanguage, ...] = (
    CatalogLanguage(
        code="fr",
        provider_code="fr",
        label="Français",
        native_label="Français",
    ),
    CatalogLanguage(
        code="en",
        provider_code="en",
        label="Anglais",
        native_label="English",
    ),
    CatalogLanguage(
        code="ar",
        provider_code="ar",
        label="Arabe standard",
        native_label="العربية الفصحى",
    ),
    CatalogLanguage(
        code="ary",
        provider_code="ar",
        label="Darija marocaine",
        native_label="الدارجة المغربية",
        is_approximated=True,
        note=(
            "Gemini Live n'a pas de locale darija : le réglage envoyé est l'arabe "
            "standard (ar) et la darija repose sur le texte prononcé. Le rendu "
            "n'est pas garanti tant que l'échantillon n'a pas été écouté."
        ),
    ),
)

# Toutes les langues du catalogue : valeur par défaut de `offered_languages`.
ALL_LANGUAGE_CODES: tuple[str, ...] = tuple(lang.code for lang in CATALOG_LANGUAGES)

# Les cinq voix Gemini Live que le dépôt déclare déjà
# (api/services/configuration/options/google.py). Les descriptions reprennent le
# grain annoncé par Google, en français : c'est ce que l'utilisateur lit avant
# d'écouter. Ajouter une voix = ajouter une ligne ici (+ ses échantillons).
CATALOG_VOICES: tuple[CatalogVoice, ...] = (
    CatalogVoice(
        id="Puck",
        display_name="Puck",
        description="Timbre clair et enjoué, débit vif.",
        offered_languages=ALL_LANGUAGE_CODES,
    ),
    CatalogVoice(
        id="Charon",
        display_name="Charon",
        description="Voix grave et posée, ton informatif.",
        offered_languages=ALL_LANGUAGE_CODES,
    ),
    CatalogVoice(
        id="Kore",
        display_name="Kore",
        description="Voix assurée et ferme, articulation nette.",
        offered_languages=ALL_LANGUAGE_CODES,
    ),
    CatalogVoice(
        id="Fenrir",
        display_name="Fenrir",
        description="Voix énergique et très expressive.",
        offered_languages=ALL_LANGUAGE_CODES,
    ),
    CatalogVoice(
        id="Aoede",
        display_name="Aoede",
        description="Voix légère et fluide, ton détendu.",
        offered_languages=ALL_LANGUAGE_CODES,
    ),
)

# Garde-fou : le catalogue ne doit proposer que des voix que le registre connaît,
# sinon la configuration compilée serait refusée par l'API Google à l'exécution.
_UNKNOWN_VOICES = {v.id for v in CATALOG_VOICES} - set(GOOGLE_REALTIME_VOICES)
if _UNKNOWN_VOICES:  # pragma: no cover - garde de cohérence au chargement du module
    raise RuntimeError(
        "Voix du catalogue absentes de GOOGLE_REALTIME_VOICES : "
        f"{sorted(_UNKNOWN_VOICES)}"
    )

_LANGUAGES_BY_CODE = {lang.code: lang for lang in CATALOG_LANGUAGES}
_VOICES_BY_KEY = {(voice.provider, voice.id): voice for voice in CATALOG_VOICES}


def list_languages() -> tuple[CatalogLanguage, ...]:
    """Les langues proposées à l'utilisateur, dans l'ordre d'affichage."""
    return CATALOG_LANGUAGES


def get_language(code: str) -> CatalogLanguage | None:
    return _LANGUAGES_BY_CODE.get(code)


def list_voices(provider: str | None = None) -> tuple[CatalogVoice, ...]:
    """Les voix du catalogue, optionnellement filtrées par fournisseur."""
    if provider is None:
        return CATALOG_VOICES
    return tuple(voice for voice in CATALOG_VOICES if voice.provider == provider)


def get_voice(
    voice_id: str, provider: str = PROVIDER_GOOGLE_REALTIME
) -> CatalogVoice | None:
    return _VOICES_BY_KEY.get((provider, voice_id))


def provider_language_code(code: str) -> str | None:
    """Code de langue réellement envoyé au fournisseur pour un code utilisateur.

    Renvoie `ar` pour `ary` : c'est toute la subtilité darija. None si le code
    n'appartient pas au catalogue — l'appelant doit refuser plutôt que deviner.
    """
    language = _LANGUAGES_BY_CODE.get(code)
    return language.provider_code if language else None


def samples_root() -> Path:
    """Dossier où les fichiers d'échantillon sont attendus sur disque.

    `VOICE_SAMPLES_DIR` prime (déploiement conteneurisé, où `ui/` n'existe pas à
    côté d'`api/`) ; sinon on retombe sur le dossier du dépôt, ce qui est le cas
    en développement et pour le script de génération.
    """
    override = os.getenv(SAMPLES_DIR_ENV_VAR)
    if override:
        return Path(override)
    return _REPO_ROOT / SAMPLES_DIR_RELATIVE_TO_REPO


def sample_relative_path(
    voice_id: str, language_code: str, provider: str
) -> str | None:
    """Chemin relatif de l'échantillon, ex. `google_realtime/puck.fr.mp3`.

    None si un segment n'est pas un identifiant sûr : un nom de fichier ne doit
    jamais pouvoir sortir du dossier d'échantillons.
    """
    provider_segment = provider.lower()
    voice_segment = voice_id.lower()
    language_segment = language_code.lower()
    if not all(
        _SAFE_SEGMENT.match(segment)
        for segment in (provider_segment, voice_segment, language_segment)
    ):
        return None
    return (
        f"{provider_segment}/"
        f"{voice_segment}.{language_segment}.{SAMPLE_AUDIO_EXTENSION}"
    )


def sample_file_path(voice_id: str, language_code: str, provider: str) -> Path | None:
    """Chemin absolu attendu de l'échantillon sur disque."""
    relative = sample_relative_path(voice_id, language_code, provider)
    if relative is None:
        return None
    return samples_root().joinpath(*relative.split("/"))


def sample_url(voice_id: str, language_code: str, provider: str) -> str | None:
    """URL statique de l'échantillon, ou None s'il n'existe pas encore.

    On interroge le disque à chaque appel, sans cache : les fichiers apparaissent
    quand le script de génération tourne, et un cache négatif ferait mentir
    l'interface (« échantillon indisponible ») bien après leur arrivée. Le coût
    est de quelques dizaines de `stat()` par requête.
    """
    path = sample_file_path(voice_id, language_code, provider)
    if path is None or not path.is_file():
        return None
    relative = sample_relative_path(voice_id, language_code, provider)
    return f"{SAMPLE_URL_PREFIX}/{relative}"


def build_voice_catalog() -> VoiceCatalogResponse:
    """Assemble le catalogue complet (langues + voix + disponibilité audio)."""
    voices: list[VoiceOption] = []
    for voice in CATALOG_VOICES:
        samples = [
            VoiceSample(
                language=language_code,
                url=sample_url(voice.id, language_code, voice.provider),
            )
            for language_code in voice.offered_languages
        ]
        voices.append(
            VoiceOption(
                id=voice.id,
                display_name=voice.display_name,
                description=voice.description,
                provider=voice.provider,
                samples=samples,
                available_languages=[s.language for s in samples if s.available],
            )
        )
    return VoiceCatalogResponse(languages=build_language_options(), voices=voices)


def build_language_options() -> list[VoiceLanguageOption]:
    """Les langues proposées, sous leur forme d'API."""
    return [
        VoiceLanguageOption(
            code=language.code,
            provider_code=language.provider_code,
            label=language.label,
            native_label=language.native_label,
            is_approximated=language.is_approximated,
            note=language.note,
        )
        for language in CATALOG_LANGUAGES
    ]
