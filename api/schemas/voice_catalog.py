"""Formes d'API du catalogue de voix.

Voir `api/services/configuration/voice_catalog.py` pour la source de vérité et
pour le schéma de nommage des fichiers d'échantillon. Ce module ne contient que
la représentation transportée : aucune donnée de locataire n'y transite.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field


class VoiceLanguageOption(BaseModel):
    """Une langue proposée à l'utilisateur."""

    code: str = Field(
        description="Code présenté à l'utilisateur et utilisé pour nommer les échantillons (en, fr, ar, ary).",
    )
    provider_code: str = Field(
        description=(
            "Code réellement envoyé au fournisseur. Diffère de `code` pour la "
            "darija : `ary` est envoyé comme `ar`, Gemini Live n'ayant pas de "
            "locale darija."
        ),
    )
    label: str = Field(description="Libellé en français.")
    native_label: str = Field(description="Libellé dans la langue elle-même.")
    is_approximated: bool = Field(
        default=False,
        description=(
            "True quand `provider_code` n'est qu'une approximation du dialecte "
            "demandé : le rendu doit être écouté avant d'être promis."
        ),
    )
    note: str | None = Field(
        default=None,
        description="Explication à afficher quand `is_approximated` est True.",
    )


class VoiceSample(BaseModel):
    """Échantillon d'une voix dans une langue donnée."""

    language: str = Field(description="Code de langue présenté à l'utilisateur.")
    url: str | None = Field(
        default=None,
        description=(
            "URL statique servie par Next, ou null quand le fichier n'existe pas "
            "encore sur disque. L'interface ne doit pas proposer de lecture dans "
            "ce cas."
        ),
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> bool:
        """Dérivé de `url` : jamais déclaré à la main, pour ne pas mentir."""
        return self.url is not None


class VoiceOption(BaseModel):
    """Une voix proposée à l'utilisateur."""

    id: str = Field(
        description="Nom exact attendu par l'API du fournisseur, casse comprise (« Puck »).",
    )
    display_name: str
    description: str = Field(description="Grain de la voix, en français.")
    provider: str = Field(description="Fournisseur, ex. `google_realtime`.")
    samples: list[VoiceSample] = Field(
        default_factory=list,
        description="Un échantillon par langue proposée, disponible ou non.",
    )
    available_languages: list[str] = Field(
        default_factory=list,
        description="Codes de langue dont l'échantillon existe réellement sur disque.",
    )


class VoiceCatalogResponse(BaseModel):
    languages: list[VoiceLanguageOption]
    voices: list[VoiceOption]


class VoiceLanguagesResponse(BaseModel):
    languages: list[VoiceLanguageOption]
