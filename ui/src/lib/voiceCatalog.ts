/**
 * Catalogue des voix proposées à l'utilisateur final (GET /api/v1/voices).
 *
 * Écrit à la main parce que `src/client/` est généré depuis l'OpenAPI et ne
 * couvre pas encore ces routes. Même mécanique que `lib/platformAdminApi.ts` :
 * la requête passe par l'instance `client` partagée, donc elle hérite de la
 * base URL résolue par `createClientConfig` et de l'intercepteur d'auth
 * enregistré une fois l'auth chargée. Les appelants doivent donc attendre
 * `authLoading` avant d'appeler (voir ui/AGENTS.md).
 *
 * Le client généré ne lève pas sur une réponse HTTP d'erreur : il résout
 * `{ data, error }`. On vérifie donc `error` explicitement et on lève une
 * `Error` portant le message backend, normalisé par `detailFromError`.
 *
 * Source de vérité des formes : api/schemas/voice_catalog.py
 * Source de vérité du contenu : api/services/configuration/voice_catalog.py
 */

import { client } from "@/client/client.gen";
import { detailFromError } from "@/lib/apiError";

/**
 * Une langue proposée à l'utilisateur.
 *
 * `code` est ce que l'utilisateur choisit et ce qui nomme les échantillons ;
 * `provider_code` est ce qu'on écrit réellement dans la configuration modèle.
 * Les deux diffèrent pour la darija : `ary` part en `ar`, Gemini Live n'ayant
 * pas de locale darija.
 */
export interface VoiceLanguageOption {
    code: string;
    provider_code: string;
    label: string;
    native_label: string;
    is_approximated: boolean;
    note: string | null;
}

/**
 * Échantillon d'une voix dans une langue.
 *
 * `available` est calculé côté API à partir de la présence du fichier sur
 * disque : il n'est jamais déclaré à la main. Quand il vaut false, l'interface
 * doit annoncer l'indisponibilité plutôt que d'offrir une lecture muette.
 */
export interface VoiceSample {
    language: string;
    url: string | null;
    available: boolean;
}

/** Une voix. `id` porte la casse exacte attendue par Google (« Puck »). */
export interface VoiceOption {
    id: string;
    display_name: string;
    description: string;
    provider: string;
    samples: VoiceSample[];
    available_languages: string[];
}

export interface VoiceCatalogResponse {
    languages: VoiceLanguageOption[];
    voices: VoiceOption[];
}

/**
 * Charge le catalogue complet.
 *
 * L'ordre des langues et des voix renvoyé par l'API est l'ordre d'affichage
 * voulu : ne pas retrier.
 */
export async function fetchVoiceCatalog(): Promise<VoiceCatalogResponse> {
    const response = await client.get({ url: "/api/v1/voices" });
    if (response.error !== undefined) {
        throw new Error(
            detailFromError(response.error, "Échec du chargement du catalogue de voix"),
        );
    }
    return response.data as VoiceCatalogResponse;
}

/**
 * L'échantillon d'une voix pour une langue, ou null s'il n'est pas proposé.
 *
 * `null` et `{ available: false }` veulent dire la même chose pour l'interface
 * (« pas d'extrait à écouter »), mais on garde la distinction : un échantillon
 * absent de la liste signifie que la langue n'est pas proposée pour cette voix.
 */
export function findVoiceSample(
    voice: VoiceOption,
    languageCode: string | null,
): VoiceSample | null {
    if (!languageCode) return null;
    return voice.samples.find((sample) => sample.language === languageCode) ?? null;
}
