"use client";

/**
 * Sélecteur « langue puis voix » côté utilisateur.
 *
 * Le propriétaire veut que la configuration des modèles reste un réglage
 * administrateur : dans la configuration d'un agent, l'utilisateur ne choisit
 * plus qu'une **langue** et une **voix**. Ce composant est ce choix-là, et rien
 * d'autre — aucun fournisseur, aucun modèle, aucune clé n'y est exposé.
 *
 * ── Où la valeur atterrit, et pourquoi c'est écrit comme ça ────────────────
 * Le moteur lit la voix dans `service_factory.py` (branche
 * `ServiceProviders.GOOGLE_REALTIME`), qui la reçoit de la configuration
 * modèle compilée. Pour un agent, cette configuration est résolue par
 * `get_effective_ai_model_configuration_for_workflow` :
 *
 *   workflow_configurations["model_configuration_v2_override"]  (si présent)
 *     → compile_ai_model_configuration_v2 → EffectiveAIModelConfiguration
 *     → service_factory lit .realtime.voice / .realtime.language
 *
 * On écrit donc directement dans `model_configuration_v2_override`, en partant
 * de la configuration déjà en vigueur (celle de l'agent si elle existe, sinon
 * celle de l'organisation) et en n'y remplaçant QUE `voice` et `language`.
 *
 * L'autre voie possible — poster `model_overrides: { realtime: { voice } }` —
 * aboutit au même endroit (la route de sauvegarde la convertit en
 * `model_configuration_v2_override`), mais en la recalculant à partir de la
 * configuration de l'organisation : un override modèle déjà posé sur l'agent
 * serait écrasé. On préfère donc partir de l'existant.
 *
 * Conséquence à connaître : dès le premier enregistrement, l'agent porte sa
 * propre copie de la configuration modèle. Les changements de modèle faits
 * ensuite côté organisation ne le suivront plus. C'est le seul mécanisme
 * par-agent qui existe aujourd'hui côté backend.
 *
 * Les clés API voyagent masquées dans cette copie ; la route de sauvegarde les
 * réconcilie avec les vraies valeurs stockées (`merge_ai_model_configuration_v2_secrets`),
 * exactement comme le fait déjà l'éditeur de configuration modèle.
 *
 * ── État nominal aujourd'hui : aucun extrait audio ─────────────────────────
 * Aucune clé Gemini n'existe dans ce déploiement, donc aucun échantillon n'a
 * encore été généré et `available` vaut false partout. Ce n'est pas un cas
 * limite : c'est ce que l'utilisateur verra. On l'annonce explicitement et on
 * n'affiche aucun bouton lecture qui ne produirait rien.
 *
 * Doit être monté sous `UnsavedChangesProvider` (il déclare sa section dirty).
 */

import { AudioLines, Loader2, Volume2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import type {
    ByokRealtimeAiModelConfiguration,
    OrganizationAiModelConfigurationResponse,
    OrganizationAiModelConfigurationV2,
} from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import {
    Card,
    CardContent,
    CardDescription,
    CardFooter,
    CardHeader,
    CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { useUnsavedChanges } from "@/context/UnsavedChangesContext";
import { useAuth } from "@/lib/auth";
import logger from "@/lib/logger";
import {
    fetchVoiceCatalog,
    findVoiceSample,
    type VoiceCatalogResponse,
    type VoiceLanguageOption,
} from "@/lib/voiceCatalog";
import type { WorkflowConfigurations } from "@/types/workflow-configurations";

/**
 * Clé de mémoire du code de langue **utilisateur** dans workflow_configurations.
 *
 * La configuration modèle ne connaît que le code fournisseur : la darija (`ary`)
 * y est enregistrée comme `ar`, indiscernable de l'arabe standard. Sans cette
 * clé, rouvrir la page afficherait « Arabe standard » à quelqu'un qui avait
 * choisi la darija. C'est une clé d'affichage : le runtime ne la lit jamais.
 */
export const VOICE_LANGUAGE_CODE_KEY = "voice_language_code";

type RealtimeConfiguration = ByokRealtimeAiModelConfiguration["realtime"];
type GeminiLiveConfiguration = Extract<
    RealtimeConfiguration,
    { provider: "google_realtime" }
>;

/**
 * La section Gemini Live d'une configuration v2, ou null si l'organisation ne
 * tourne pas sur Gemini Live.
 *
 * On se limite volontairement à `google_realtime`. `google_vertex_realtime`
 * propose les mêmes voix mais attend un code de langue BCP-47 (`en-US`), pas
 * les codes ISO 639-1 du catalogue : y écrire « fr » serait une supposition.
 */
function geminiLiveConfigurationOf(
    configuration: OrganizationAiModelConfigurationV2 | null,
): GeminiLiveConfiguration | null {
    if (!configuration || configuration.mode !== "byok") return null;
    const byok = configuration.byok;
    if (!byok || byok.mode !== "realtime" || !byok.realtime) return null;
    const realtime = byok.realtime.realtime;
    if (!realtime || realtime.provider !== "google_realtime") return null;
    return realtime;
}

/**
 * Recopie la configuration en n'y changeant que la voix et la langue.
 *
 * Tout le reste (fournisseur, modèle, clés masquées, section LLM, embeddings)
 * est transmis tel quel : l'utilisateur ne configure pas de modèle, il ne fait
 * que remplacer deux champs de ce que l'administrateur a posé.
 */
function withVoiceSelection(
    configuration: OrganizationAiModelConfigurationV2,
    gemini: GeminiLiveConfiguration,
    voiceId: string,
    providerLanguageCode: string,
): OrganizationAiModelConfigurationV2 {
    const byok = configuration.byok as NonNullable<OrganizationAiModelConfigurationV2["byok"]>;
    const realtimeSection = byok.realtime as ByokRealtimeAiModelConfiguration;
    return {
        ...configuration,
        byok: {
            ...byok,
            realtime: {
                ...realtimeSection,
                realtime: {
                    ...gemini,
                    voice: voiceId,
                    language: providerLanguageCode,
                },
            },
        },
    };
}

/**
 * Quelle langue présélectionner à l'ouverture.
 *
 * On fait confiance en priorité au code utilisateur mémorisé : c'est le seul
 * moyen de retrouver la darija, que la configuration modèle a aplatie en `ar`.
 * À défaut on remonte depuis le code fournisseur, et en dernier recours on
 * prend la première langue du catalogue.
 */
function resolveInitialLanguageCode(
    languages: VoiceLanguageOption[],
    savedUserCode: string | null,
    savedProviderCode: string | null,
): string | null {
    const rememberedByUser = languages.find((language) => language.code === savedUserCode);
    if (
        rememberedByUser
        && (!savedProviderCode || rememberedByUser.provider_code === savedProviderCode)
    ) {
        return rememberedByUser.code;
    }
    const byProviderCode = languages.find(
        (language) => language.provider_code === savedProviderCode,
    );
    if (byProviderCode) return byProviderCode.code;
    return languages[0]?.code ?? null;
}

interface LanguageVoicePickerProps {
    workflowConfigurations: WorkflowConfigurations;
    workflowName: string;
    /** Configuration modèle de l'organisation, base quand l'agent n'a pas la sienne. */
    organizationModelConfiguration: OrganizationAiModelConfigurationResponse | null;
    modelConfigurationLoading: boolean;
    modelConfigurationError: string | null;
    onSave: (
        configurations: WorkflowConfigurations,
        workflowName: string,
    ) => Promise<void>;
}

export function LanguageVoicePicker({
    workflowConfigurations,
    workflowName,
    organizationModelConfiguration,
    modelConfigurationLoading,
    modelConfigurationError,
    onSave,
}: LanguageVoicePickerProps) {
    const { user, loading: authLoading } = useAuth();

    const [catalog, setCatalog] = useState<VoiceCatalogResponse | null>(null);
    const [catalogLoading, setCatalogLoading] = useState(true);
    const [catalogError, setCatalogError] = useState<string | null>(null);
    const hasFetchedCatalog = useRef(false);

    const [languageCode, setLanguageCode] = useState<string | null>(null);
    const [voiceId, setVoiceId] = useState<string | null>(null);
    const hasInitialisedSelection = useRef(false);

    const [playingVoiceId, setPlayingVoiceId] = useState<string | null>(null);
    const [currentAudio, setCurrentAudio] = useState<HTMLAudioElement | null>(null);

    const [isSaving, setIsSaving] = useState(false);
    const [saveError, setSaveError] = useState<string | null>(null);

    // L'intercepteur qui pose le jeton n'est enregistré qu'une fois l'auth
    // chargée : fetcher avant enverrait une requête anonyme (voir ui/AGENTS.md).
    useEffect(() => {
        if (authLoading || !user || hasFetchedCatalog.current) return;
        hasFetchedCatalog.current = true;

        const loadCatalog = async () => {
            try {
                setCatalog(await fetchVoiceCatalog());
            } catch (error) {
                logger.error(`Failed to load the voice catalog: ${error}`);
                setCatalogError(
                    error instanceof Error
                        ? error.message
                        : "Échec du chargement du catalogue de voix",
                );
            } finally {
                setCatalogLoading(false);
            }
        };

        loadCatalog();
    }, [authLoading, user]);

    // La configuration qui fait foi pour cet agent : la sienne si elle existe,
    // sinon celle de l'organisation.
    const baseConfiguration = useMemo<OrganizationAiModelConfigurationV2 | null>(() => {
        const workflowOverride = workflowConfigurations.model_configuration_v2_override;
        if (workflowOverride) {
            return workflowOverride as OrganizationAiModelConfigurationV2;
        }
        const organizationConfiguration = organizationModelConfiguration?.configuration;
        return (organizationConfiguration as OrganizationAiModelConfigurationV2 | null) ?? null;
    }, [workflowConfigurations, organizationModelConfiguration]);

    const geminiConfiguration = useMemo(
        () => geminiLiveConfigurationOf(baseConfiguration),
        [baseConfiguration],
    );

    const savedVoiceId = geminiConfiguration?.voice ?? null;
    const savedProviderLanguage = geminiConfiguration?.language ?? null;
    const savedLanguageCode = typeof workflowConfigurations[VOICE_LANGUAGE_CODE_KEY] === "string"
        ? (workflowConfigurations[VOICE_LANGUAGE_CODE_KEY] as string)
        : null;

    useEffect(() => {
        if (hasInitialisedSelection.current || !catalog) return;
        hasInitialisedSelection.current = true;
        setLanguageCode(
            resolveInitialLanguageCode(
                catalog.languages,
                savedLanguageCode,
                savedProviderLanguage,
            ),
        );
        setVoiceId(savedVoiceId);
    }, [catalog, savedLanguageCode, savedProviderLanguage, savedVoiceId]);

    // Nettoyage au démontage : même motif que VoiceSelector.tsx, l'extrait ne
    // doit pas continuer à jouer après la disparition de la carte.
    useEffect(() => {
        return () => {
            if (currentAudio) {
                currentAudio.pause();
            }
        };
    }, [currentAudio]);

    const stopPlayback = useCallback(() => {
        if (currentAudio) {
            currentAudio.pause();
            currentAudio.currentTime = 0;
            setCurrentAudio(null);
        }
        setPlayingVoiceId(null);
    }, [currentAudio]);

    // Un seul extrait à la fois : lancer une écoute coupe la précédente, et
    // recliquer sur la voix en cours l'arrête.
    const togglePlayback = (url: string, id: string) => {
        const wasPlaying = playingVoiceId === id;
        stopPlayback();
        if (wasPlaying) return;

        setPlayingVoiceId(id);
        const audio = new Audio(url);
        setCurrentAudio(audio);
        audio.onended = () => {
            setPlayingVoiceId(null);
            setCurrentAudio(null);
        };
        audio.onerror = () => {
            setPlayingVoiceId(null);
            setCurrentAudio(null);
        };
        audio.play().catch(() => {
            setPlayingVoiceId(null);
            setCurrentAudio(null);
        });
    };

    // Changer de langue change l'extrait de chaque voix : ce qui joue encore
    // n'est plus la langue affichée, on le coupe.
    const handleLanguageChange = (nextLanguageCode: string) => {
        stopPlayback();
        setLanguageCode(nextLanguageCode);
    };

    const selectedLanguage = catalog?.languages.find(
        (language) => language.code === languageCode,
    ) ?? null;

    const isDirty = Boolean(
        selectedLanguage
        && voiceId
        && (
            voiceId !== savedVoiceId
            || selectedLanguage.provider_code !== savedProviderLanguage
            || languageCode !== savedLanguageCode
        ),
    );

    useUnsavedChanges("voice", isDirty);

    const handleSave = async () => {
        if (!baseConfiguration || !geminiConfiguration || !selectedLanguage || !voiceId) {
            return;
        }
        setIsSaving(true);
        setSaveError(null);
        try {
            await onSave(
                {
                    ...workflowConfigurations,
                    // Le code utilisateur est mémorisé à part : la config modèle
                    // reçoit provider_code, donc « ary » y part en « ar ».
                    [VOICE_LANGUAGE_CODE_KEY]: selectedLanguage.code,
                    model_configuration_v2_override: withVoiceSelection(
                        baseConfiguration,
                        geminiConfiguration,
                        voiceId,
                        selectedLanguage.provider_code,
                    ),
                },
                workflowName,
            );
            toast.success("Langue et voix enregistrées");
        } catch (error) {
            setSaveError(
                error instanceof Error
                    ? error.message
                    : "Échec de l'enregistrement de la langue et de la voix",
            );
        } finally {
            setIsSaving(false);
        }
    };

    const voices = catalog?.voices ?? [];
    const noSampleForThisLanguage = Boolean(languageCode)
        && voices.length > 0
        && voices.every((voice) => !findVoiceSample(voice, languageCode)?.available);

    const renderBody = () => {
        if (modelConfigurationLoading) {
            return (
                <div className="flex items-center gap-2 rounded-md border p-4 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Chargement de la configuration modèle
                </div>
            );
        }

        if (modelConfigurationError) {
            return (
                <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                    {modelConfigurationError}
                </div>
            );
        }

        // Pas de Gemini Live derrière cet agent : on ne propose rien plutôt que
        // d'écrire une voix dans une configuration qui ne la lira jamais.
        if (!geminiConfiguration) {
            return (
                <div className="rounded-md border bg-muted/30 p-4 text-sm text-muted-foreground">
                    Cet agent ne tourne pas sur Gemini Live. Le choix de la langue et de la
                    voix est proposé uniquement pour Gemini Live ; votre administrateur peut
                    changer le moteur vocal de l&apos;organisation.
                </div>
            );
        }

        if (catalogLoading) {
            return (
                <div className="flex items-center gap-2 rounded-md border p-4 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Chargement des voix
                </div>
            );
        }

        if (catalogError) {
            return (
                <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                    {catalogError}
                </div>
            );
        }

        return (
            <div className="space-y-6">
                {/* Étape 1 — la langue */}
                <div className="space-y-2">
                    <Label htmlFor="voice-language" className="text-sm font-medium">
                        1. Langue de l&apos;agent
                    </Label>
                    <Select value={languageCode ?? undefined} onValueChange={handleLanguageChange}>
                        <SelectTrigger id="voice-language">
                            <SelectValue placeholder="Choisir une langue" />
                        </SelectTrigger>
                        <SelectContent>
                            {catalog?.languages.map((language) => (
                                <SelectItem key={language.code} value={language.code}>
                                    {language.label}
                                    <span className="ml-2 text-muted-foreground">
                                        {language.native_label}
                                    </span>
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>

                    {selectedLanguage?.is_approximated && (
                        <div className="rounded-md border bg-muted/30 p-3 text-xs">
                            <p className="font-medium">
                                La darija n&apos;est pas une langue à part pour Gemini Live.
                            </p>
                            <p className="mt-1 text-muted-foreground">
                                L&apos;agent parlera avec le réglage arabe, et c&apos;est le texte de
                                votre scénario qui fait la darija. Écoutez un extrait avant de
                                promettre ce rendu à un client.
                            </p>
                            {selectedLanguage.note && (
                                <p className="mt-2 text-muted-foreground">{selectedLanguage.note}</p>
                            )}
                        </div>
                    )}
                </div>

                {/* Étape 2 — la voix */}
                <div className="space-y-3">
                    <div>
                        <p className="text-sm font-medium">2. Voix</p>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                            Écoutez chaque voix dans la langue choisie, puis sélectionnez-la.
                        </p>
                    </div>

                    {noSampleForThisLanguage && (
                        <p className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
                            Aucun extrait n&apos;a encore été enregistré pour cette langue. Vous
                            pouvez tout de même choisir une voix : elle sera utilisée pendant les
                            appels.
                        </p>
                    )}

                    <RadioGroup
                        value={voiceId ?? undefined}
                        onValueChange={setVoiceId}
                        aria-label="Voix de l'agent"
                        className="gap-2"
                    >
                        {voices.map((voice) => {
                            const sample = findVoiceSample(voice, languageCode);
                            // `available` est calculé côté API depuis le disque ;
                            // on n'ouvre la lecture que s'il y a vraiment une URL.
                            const sampleUrl = sample?.available ? sample.url : null;
                            const isPlaying = playingVoiceId === voice.id;
                            return (
                                <div
                                    key={voice.id}
                                    className="flex items-start gap-3 rounded-md border p-3"
                                >
                                    <RadioGroupItem
                                        value={voice.id}
                                        id={`voice-${voice.id}`}
                                        className="mt-1"
                                    />
                                    <div className="min-w-0 flex-1">
                                        <Label
                                            htmlFor={`voice-${voice.id}`}
                                            className="cursor-pointer text-sm font-medium"
                                        >
                                            {voice.display_name}
                                        </Label>
                                        <p className="mt-0.5 text-xs text-muted-foreground">
                                            {voice.description}
                                        </p>
                                        {!sampleUrl && (
                                            <p className="mt-1 text-xs italic text-muted-foreground">
                                                Extrait pas encore disponible
                                                {selectedLanguage ? ` en ${selectedLanguage.label.toLowerCase()}` : ""}.
                                            </p>
                                        )}
                                    </div>
                                    {/* Pas de bouton lecture quand il n'y a rien à lire :
                                        un bouton inerte ferait croire que la voix est muette. */}
                                    {sampleUrl && (
                                        <Button
                                            type="button"
                                            variant="ghost"
                                            size="sm"
                                            className="h-8 w-8 shrink-0 p-0"
                                            aria-pressed={isPlaying}
                                            aria-label={
                                                isPlaying
                                                    ? `Arrêter l'extrait de ${voice.display_name}`
                                                    : `Écouter ${voice.display_name}${selectedLanguage ? ` en ${selectedLanguage.label.toLowerCase()}` : ""}`
                                            }
                                            onClick={() => togglePlayback(sampleUrl, voice.id)}
                                        >
                                            <Volume2
                                                className={isPlaying ? "h-4 w-4 animate-pulse text-primary" : "h-4 w-4"}
                                            />
                                        </Button>
                                    )}
                                </div>
                            );
                        })}
                    </RadioGroup>
                </div>

                {saveError && (
                    <p className="text-sm text-destructive">{saveError}</p>
                )}
            </div>
        );
    };

    const canSave = Boolean(geminiConfiguration) && isDirty && !isSaving;

    return (
        <Card id="voice">
            <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                    <AudioLines className="h-4 w-4" />
                    Langue et voix
                </CardTitle>
                <CardDescription>
                    Choisissez la langue de l&apos;agent, écoutez les voix disponibles, puis
                    sélectionnez celle qu&apos;il utilisera pendant les appels.
                </CardDescription>
            </CardHeader>
            <CardContent>{renderBody()}</CardContent>
            {geminiConfiguration && !modelConfigurationLoading && !catalogLoading && (
                <CardFooter className="justify-end gap-3 border-t pt-6">
                    {isDirty && (
                        <span className="text-xs text-muted-foreground">
                            Modifications non enregistrées
                        </span>
                    )}
                    <Button onClick={handleSave} disabled={!canSave}>
                        {isSaving ? "Enregistrement..." : "Enregistrer la langue et la voix"}
                    </Button>
                </CardFooter>
            )}
        </Card>
    );
}
