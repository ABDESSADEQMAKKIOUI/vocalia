"""API du catalogue de voix proposées à l'utilisateur.

L'utilisateur ne configure plus les modèles : il choisit une langue et une voix.
Ces deux endpoints alimentent ce choix.

    GET /api/v1/voices            → catalogue complet (voix, langues, disponibilité audio)
    GET /api/v1/voices/languages  → seulement les langues, avec leur libellé natif

Authentification requise : le catalogue ne contient aucun secret, mais ce n'est pas
une donnée publique. Aucune donnée de locataire n'en sort — le contenu est identique
pour toutes les organisations, `get_user_with_selected_organization` sert seulement
de porte d'entrée (et garantit un contexte d'organisation cohérent avec le reste
des écrans de configuration).
"""

from fastapi import APIRouter, Depends

from api.db.models import UserModel
from api.schemas.voice_catalog import VoiceCatalogResponse, VoiceLanguagesResponse
from api.services.auth.depends import get_user_with_selected_organization
from api.services.configuration.voice_catalog import (
    build_language_options,
    build_voice_catalog,
)

router = APIRouter(prefix="/voices", tags=["voices"])


@router.get("", response_model=VoiceCatalogResponse)
async def get_voice_catalog(
    _user: UserModel = Depends(get_user_with_selected_organization),
) -> VoiceCatalogResponse:
    """Catalogue complet : langues proposées, voix, et échantillons disponibles.

    `sample_url` vaut null quand l'échantillon n'a pas encore été généré ; dans ce
    cas l'interface annonce l'indisponibilité au lieu d'offrir une lecture muette.
    """
    return build_voice_catalog()


@router.get("/languages", response_model=VoiceLanguagesResponse)
async def get_voice_languages(
    _user: UserModel = Depends(get_user_with_selected_organization),
) -> VoiceLanguagesResponse:
    """Les langues proposées, avec leur libellé natif.

    `provider_code` peut différer de `code` (la darija `ary` est envoyée comme
    `ar`) : c'est `provider_code` qui doit finir dans la configuration modèle.
    """
    return VoiceLanguagesResponse(languages=build_language_options())
