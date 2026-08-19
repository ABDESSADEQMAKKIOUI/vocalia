"""Seed the JEB Training Center agent, and give it its own Gemini Live setup.

The agent answers candidates' questions about JEB Training Center's programs and
its published FAQ, in French, English and Moroccan darija. Its graph lives in
`api/services/workflow/jeb_agent.py`, its knowledge in
`api/services/workflow/jeb_knowledge.py`.

Idempotent by design: re-running it never creates a second agent — it finds the
existing one by name. It performs no destructive write. `--refresh-prompts`
rewrites the graph from the module (saving a new draft and publishing it, the
previous published version being kept as an archived version, exactly as an edit
in the editor would).

Why the model configuration is attached to this agent and not to the organization
--------------------------------------------------------------------------------
This agent must run on Gemini Live. The obvious place to say so would be the
organization's model configuration — and it is the wrong one: the organization
currently runs in "dograh" managed mode and other agents depend on it. Switching
the organization to BYOK would take them with it, silently, on their next call.

So the Gemini Live configuration is written to this agent's
`workflow_configurations.model_configuration_v2_override` instead. That field is
read per run (`services/pipecat/run_pipeline.py` resolves it from the run's own
definition through `get_effective_ai_model_configuration_for_workflow`), it
short-circuits the organization's configuration for this workflow only, and
nothing outside this agent changes. This script never writes an organization
configuration row — it only reads one, to tell you what the other agents are
still on.

The API key
-----------
Read from the environment (`GEMINI_API_KEY`, then `GOOGLE_API_KEY`) or from
`--api-key`. It is never hard-coded here and never printed back: a secret
committed to a repository is a published secret. When the agent already carries
an override and no key is supplied, the key already stored is kept.

Run from the repo root with the api environment available:

    python -m scripts.seed_jeb_agent

On the Volira production host (51.222.30.228), the api container is `volira-api`
and its image ships only the shell entrypoints from `scripts/`, so copy the four
files in rather than recreating the container — recreating it drops calls in
progress:

    docker cp api/services/workflow/jeb_knowledge.py      volira-api:/app/api/services/workflow/
    docker cp api/services/workflow/jeb_knowledge_data.py volira-api:/app/api/services/workflow/
    docker cp api/services/workflow/jeb_agent.py     volira-api:/app/api/services/workflow/
    docker cp scripts/seed_jeb_agent.py              volira-api:/app/scripts/
    docker exec -e GEMINI_API_KEY="$GEMINI_API_KEY" volira-api python -m scripts.seed_jeb_agent

All three are needed: the script imports the graph, and the graph imports the
knowledge base.

Useful variants:

    ... python -m scripts.seed_jeb_agent --dry-run              # resolve and validate, write nothing
    ... python -m scripts.seed_jeb_agent --refresh-prompts      # rewrite the prompts of an existing agent
    ... python -m scripts.seed_jeb_agent --voice Charon --language ary
"""

import argparse
import asyncio
import copy
import os
import sys

from loguru import logger

logger.remove()

_MISSING_ENV = [name for name in ("DATABASE_URL", "REDIS_URL") if not os.getenv(name)]
if _MISSING_ENV:
    print(
        f"Missing environment variable(s): {', '.join(_MISSING_ENV)}. "
        "Source the api environment first (e.g. `set -a && source api/.env && set +a`).",
        file=sys.stderr,
    )
    raise SystemExit(2)

from sqlalchemy import select  # noqa: E402

from api.db import db_client  # noqa: E402
from api.db.models import OrganizationModel, UserModel, WorkflowModel  # noqa: E402
from api.schemas.ai_model_configuration import (  # noqa: E402
    OrganizationAIModelConfigurationV2,
    compile_ai_model_configuration_v2,
)
from api.services.configuration.ai_model_configuration import (  # noqa: E402
    WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY,
    get_organization_ai_model_configuration_v2,
)
from api.services.configuration.masking import mask_key  # noqa: E402
from api.services.configuration.options.google import (  # noqa: E402
    GOOGLE_MODELS,
    GOOGLE_REALTIME_LANGUAGES,
    GOOGLE_REALTIME_MODELS,
    GOOGLE_REALTIME_VOICES,
)
try:  # noqa: E402
    from api.services.configuration.voice_catalog import get_language
except ModuleNotFoundError:
    # Installs that predate the voice catalog still seed fine: the catalog only
    # refines the language code, and the fallback below is the provider list.
    def get_language(code):  # type: ignore[misc]
        return None
from api.services.workflow.dto import ReactFlowDTO  # noqa: E402
from api.services.workflow.jeb_agent import (  # noqa: E402
    JEB_WORKFLOW_NAME,
    build_jeb_workflow,
    collect_tool_uuids,
)
from api.services.workflow.jeb_knowledge import knowledge_block_stats  # noqa: E402
from api.services.workflow.workflow_graph import WorkflowGraph  # noqa: E402

# Defaults for the Gemini Live side. The realtime model and the voice are the
# repo's own declared values (options/google.py); the LLM is the non-realtime
# Gemini used for the extraction pass, which is why a realtime configuration
# still carries one (see BYOKRealtimeAIModelConfiguration).
DEFAULT_REALTIME_MODEL = GOOGLE_REALTIME_MODELS[0]
DEFAULT_LLM_MODEL = GOOGLE_MODELS[0]
# Kore: "voix assurée et ferme, articulation nette" in the voice catalog — the
# register that suits an information line where numbers and names must land.
DEFAULT_VOICE = "Kore"
# The opening language hint of the session. French is what a Casablanca caller
# most often opens with; the agent switches to English or darija from the
# caller's first sentence regardless — that behaviour is in the prompt, not here.
DEFAULT_LANGUAGE = "fr"

_API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


# ─────────────────────────────────────────────────────────────────────────
# Resolution helpers
# ─────────────────────────────────────────────────────────────────────────


async def _list_organizations() -> list[OrganizationModel]:
    """Every organization, oldest first. No db_client helper lists these."""
    async with db_client.async_session() as session:
        result = await session.execute(
            select(OrganizationModel).order_by(OrganizationModel.id)
        )
        return list(result.scalars().all())


async def _resolve_organization(
    organization_id: int | None,
) -> OrganizationModel | None:
    """The target organization: the one asked for, or the only one that exists."""
    if organization_id is not None:
        organization = await db_client.get_organization_by_id(organization_id)
        if organization is None:
            print(f"No organization with id={organization_id}.", file=sys.stderr)
        return organization

    organizations = await _list_organizations()
    if not organizations:
        print(
            "This install has no organization yet. Create the owner account "
            "first (see scripts/bootstrap_platform_admin.py).",
            file=sys.stderr,
        )
        return None
    if len(organizations) > 1:
        print(
            f"{len(organizations)} organizations found; pass --organization-id "
            "to say which one:",
            file=sys.stderr,
        )
        for organization in organizations:
            print(
                f"  id={organization.id} provider_id={organization.provider_id}",
                file=sys.stderr,
            )
        return None

    return organizations[0]


async def _resolve_owner(
    organization: OrganizationModel, email: str | None, user_id: int | None
) -> UserModel | None:
    """The user the agent is created under, checked against the organization.

    Membership is verified rather than assumed: `workflows.user_id` is the owner
    recorded on the agent, and pointing it at someone outside the organization
    would create a row nobody in that org owns.
    """
    members = await db_client.get_organization_users(organization.id)
    members_by_id = {member.id: member for member in members}

    if email:
        user = await db_client.get_user_by_email(email)
        if user is None:
            print(f"No user found for {email}.", file=sys.stderr)
            return None
    elif user_id is not None:
        user = await db_client.get_user_by_id(user_id)
        if user is None:
            print(f"No user with id={user_id}.", file=sys.stderr)
            return None
    else:
        if not members:
            print(
                f"Organization {organization.id} has no member to own the agent.",
                file=sys.stderr,
            )
            return None
        if len(members) > 1:
            print(
                f"Organization {organization.id} has {len(members)} members; "
                "pass --user-email or --user-id to say who owns the agent:",
                file=sys.stderr,
            )
            for member in members:
                print(
                    f"  id={member.id} email={member.email or '(none)'}",
                    file=sys.stderr,
                )
            return None
        return members[0]

    if user.id not in members_by_id:
        print(
            f"User id={user.id} is not a member of organization {organization.id}.",
            file=sys.stderr,
        )
        return None
    return user


def _published_configurations(workflow) -> dict | None:
    """The configurations of the version that answers calls, not of the draft.

    ``workflows.workflow_configurations`` is kept in sync with the DRAFT, so
    reading it to decide whether the override is in place reports success while
    the released version still runs on the organization's configuration.
    """
    if workflow is None:
        return None
    released = getattr(workflow, "released_definition", None)
    if released is not None:
        return getattr(released, "workflow_configurations", None)
    return None


async def _find_existing(organization_id: int, name: str) -> WorkflowModel | None:
    """The already-seeded agent, whatever its status — that is the idempotency."""
    workflows = await db_client.get_all_workflows(organization_id=organization_id)
    matches = [
        workflow for workflow in workflows if (workflow.name or "").strip() == name
    ]
    if not matches:
        return None
    # The listing loads the draft only; re-fetch with the released definition
    # eagerly loaded, since that is the version whose override decides things
    # and the session is closed by the time it is read.
    return await db_client.get_workflow(
        matches[0].id, organization_id=organization_id
    )


def _validate_definition(definition: dict) -> list[str]:
    """Run the definition through the same checks the editor and runtime use.

    An invalid graph would break the workflow editor for the owner, which is
    worse than shipping no agent at all — so this runs before any write.
    """
    try:
        dto = ReactFlowDTO.model_validate(definition)
    except Exception as exc:
        return [f"DTO validation failed: {exc}"]

    try:
        WorkflowGraph(dto)
    except ValueError as exc:
        errors = exc.args[0] if exc.args else exc
        if isinstance(errors, list):
            return [str(error) for error in errors]
        return [str(errors)]
    except Exception as exc:
        return [f"Graph validation failed: {exc}"]

    return []


# ─────────────────────────────────────────────────────────────────────────
# Gemini Live override
# ─────────────────────────────────────────────────────────────────────────


def _resolve_api_key(explicit: str | None) -> tuple[str | None, str]:
    """The Gemini API key and where it came from.

    Both env var names circulate in Google's own docs and in deployments;
    refusing the second would waste the time of someone whose key is right there.
    """
    if explicit and explicit.strip():
        return explicit.strip(), "--api-key"
    for name in _API_KEY_ENV_VARS:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip(), name
    return None, ""


def _resolve_language(code: str) -> tuple[str | None, str | None]:
    """Translate a catalog language code into the one Gemini Live accepts.

    Returns (provider_code, note). The catalog exists because the two are not
    always the same: `ary` (Moroccan darija) is offered to users, but Gemini Live
    has no darija locale, so what is actually sent is `ar` and the darija comes
    from the text the model speaks — which is precisely what this agent's prompt
    handles.
    """
    catalog_language = get_language(code)
    if catalog_language is not None:
        return catalog_language.provider_code, catalog_language.note

    if code in GOOGLE_REALTIME_LANGUAGES:
        return code, None
    return None, None


def build_gemini_override(
    *,
    api_key: str,
    realtime_model: str,
    voice: str,
    language: str,
    llm_model: str,
) -> dict:
    """The per-agent `model_configuration_v2_override` value, validated.

    Built through the schema rather than as a hand-written dict so a typo fails
    here, before anything is written, instead of on the first call. The `llm`
    section is not optional padding: a realtime BYOK configuration also declares
    the text model used for the extraction pass that runs beside the live
    session.
    """
    configuration = OrganizationAIModelConfigurationV2.model_validate(
        {
            "version": 2,
            "mode": "byok",
            "byok": {
                "mode": "realtime",
                "realtime": {
                    "realtime": {
                        "provider": "google_realtime",
                        "api_key": api_key,
                        "model": realtime_model,
                        "voice": voice,
                        "language": language,
                    },
                    "llm": {
                        "provider": "google",
                        "api_key": api_key,
                        "model": llm_model,
                    },
                },
            },
        }
    )
    # The same compilation the runtime performs. It catches a configuration that
    # validates field by field but cannot be turned into services.
    compile_ai_model_configuration_v2(configuration)
    return configuration.model_dump(mode="json", exclude_none=True)


def _existing_override(workflow_configurations: dict | None) -> dict | None:
    override = (workflow_configurations or {}).get(
        WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY
    )
    return override if isinstance(override, dict) else None


def _override_realtime_section(override: dict | None) -> dict:
    """The `byok.realtime.realtime` block of an override, or an empty dict."""
    node = override or {}
    for key in ("byok", "realtime", "realtime"):
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def _describe_override(override: dict | None) -> str:
    """One readable line about an override — never its key in clear."""
    realtime = _override_realtime_section(override)
    if not realtime:
        return "aucune configuration modèle propre à cet agent"
    api_key = realtime.get("api_key")
    masked = mask_key(api_key) if isinstance(api_key, str) and api_key else "(aucune)"
    return (
        f"{realtime.get('provider')} / {realtime.get('model')} — voix "
        f"{realtime.get('voice')}, langue {realtime.get('language')}, clé {masked}"
    )


def _merge_override(configurations: dict | None, override: dict) -> dict:
    """This agent's configurations with the model override replaced.

    A copy, and a merge rather than a replacement: `workflow_configurations` also
    holds call settings (max_call_duration, idle timeouts, ambient noise) that
    belong to whoever tuned them.
    """
    merged = copy.deepcopy(configurations or {})
    merged[WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY] = override
    return merged


# ─────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────


def _print_knowledge_stats() -> None:
    """What the prompt costs. Printed because it is an operating cost.

    The knowledge block is sent as system instruction once per Gemini Live
    session; whoever deploys this agent should read that number here rather than
    discover it on a bill.
    """
    stats = knowledge_block_stats()
    print(
        f"Socle de connaissance : {stats['chars']:,} caractères, "
        f"~{stats['tokens_estimate']:,} tokens estimés, renvoyés à chaque "
        "transition de nœud (le moteur reconnecte la session Gemini Live et "
        "renvoie l'instruction entière, avec l'historique de la conversation)."
    )
    parts = ", ".join(f"{name} ~{tokens:,}" for name, tokens in stats["parts"].items())
    print(f"  Détail par partie (tokens) : {parts}")
    print(
        f"  FAQ officielle : {stats['faq_questions']} questions, langues rendues : "
        f"{', '.join(stats['faq_languages'])} ; {stats['sections']} sections de "
        "connaissance issues des pages du site."
    )


async def _print_organization_mode(organization_id: int) -> None:
    """Say what the organization runs on, and that this script leaves it alone."""
    try:
        organization_configuration = await get_organization_ai_model_configuration_v2(
            organization_id
        )
    except Exception as exc:  # pragma: no cover - diagnostics only
        print(f"  (configuration de l'organisation illisible : {exc})")
        return

    if organization_configuration is None:
        print(
            "  L'organisation n'a pas de configuration modèle v2 ; les autres "
            "agents utilisent la configuration héritée. Elle n'est pas modifiée."
        )
        return
    print(
        "  Configuration de l'organisation : mode « "
        f"{organization_configuration.mode} » — inchangée par ce script. "
        "Les autres agents continuent de l'utiliser."
    )


def _print_test_steps(workflow_id: int, has_override: bool) -> None:
    print("\nPour tester :")
    print(f"  1. Ouvrez l'agent dans Volira : /workflow/{workflow_id}")
    print("  2. Lancez un appel de test (bouton « Test »).")
    print(
        "  3. Commencez en français, puis basculez en darija en cours d'appel : "
        "l'agent doit suivre sans qu'on le lui demande."
    )
    print(
        "  4. Demandez-lui de garantir un visa, un emploi ou un délai : il doit "
        "refuser et proposer l'équipe des admissions."
    )
    if not has_override:
        print(
            "\nAttention : cet agent n'a pas encore sa configuration Gemini Live "
            "propre ; l'appel de test utilisera la configuration de "
            "l'organisation."
        )


# ─────────────────────────────────────────────────────────────────────────
# Main flow
# ─────────────────────────────────────────────────────────────────────────


async def seed(args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not name:
        print("--name must not be empty.", file=sys.stderr)
        return 2

    organization = await _resolve_organization(args.organization_id)
    if organization is None:
        return 1

    owner = await _resolve_owner(organization, args.user_email, args.user_id)
    if owner is None:
        return 1

    print(
        f"Organization id={organization.id} — owner id={owner.id} "
        f"({owner.email or 'no email'})"
    )
    await _print_organization_mode(organization.id)
    _print_knowledge_stats()

    tool_uuids = [uuid.strip() for uuid in (args.tool_uuid or []) if uuid.strip()]
    existing = await _find_existing(organization.id, name)

    # --- the Gemini Live override, resolved before any write ------------
    # Read it off the PUBLISHED version, not workflows.workflow_configurations:
    # that legacy column tracks the draft (api/db/workflow_client.py), so an
    # override left unpublished would read as "already configured" while the
    # version that actually answers calls has none.
    stored_override = _existing_override(_published_configurations(existing))
    override: dict | None = None
    if not args.no_model_override:
        api_key, key_source = _resolve_api_key(args.api_key)
        if api_key is None:
            # An override already in place carries its own key; re-running
            # without one must not wipe it.
            stored_key = _override_realtime_section(stored_override).get("api_key")
            if isinstance(stored_key, str) and stored_key:
                api_key, key_source = stored_key, "configuration déjà en place"

        if api_key is None:
            print(
                "\nAucune clé Gemini : ni --api-key, ni "
                f"{', '.join(_API_KEY_ENV_VARS)}."
            )
            print(
                "  L'agent sera créé ou mis à jour sans configuration modèle "
                "propre ; il tournera alors sur celle de l'organisation."
            )
            print(
                "  Relancez ce script avec la clé pour lui attacher Gemini Live :"
            )
            print(
                "       docker exec -e GEMINI_API_KEY=\"$GEMINI_API_KEY\" "
                "volira-api python -m scripts.seed_jeb_agent"
            )
        else:
            language, language_note = _resolve_language(args.language)
            if language is None:
                print(
                    f"Langue « {args.language} » inconnue. Valeurs acceptées : "
                    f"{', '.join(GOOGLE_REALTIME_LANGUAGES)} — ou « ary » pour la "
                    "darija marocaine.",
                    file=sys.stderr,
                )
                return 2
            if args.voice not in GOOGLE_REALTIME_VOICES:
                print(
                    f"Voix « {args.voice} » inconnue. Valeurs acceptées : "
                    f"{', '.join(GOOGLE_REALTIME_VOICES)}.",
                    file=sys.stderr,
                )
                return 2

            try:
                override = build_gemini_override(
                    api_key=api_key,
                    realtime_model=args.realtime_model,
                    voice=args.voice,
                    language=language,
                    llm_model=args.llm_model,
                )
            except Exception as exc:
                print(
                    f"Configuration Gemini Live invalide ; rien n'a été écrit : "
                    f"{exc}",
                    file=sys.stderr,
                )
                return 1

            print(f"\nConfiguration Gemini Live (clé lue depuis {key_source}) :")
            print(f"  {_describe_override(override)}")
            if language_note:
                print(f"  Note langue : {language_note}")
            # The registry accepts custom model ids on purpose (new Gemini
            # releases ship faster than this repo). Saying so beats a typo that
            # only surfaces as a provider error on the first call.
            if args.realtime_model not in GOOGLE_REALTIME_MODELS:
                print(
                    f"  Attention : « {args.realtime_model} » ne figure pas dans "
                    "les modèles Gemini Live connus du dépôt "
                    f"({', '.join(GOOGLE_REALTIME_MODELS)}). Accepté, mais "
                    "vérifiez l'orthographe."
                )
            if args.llm_model not in GOOGLE_MODELS:
                print(
                    f"  Attention : « {args.llm_model} » ne figure pas dans les "
                    f"modèles Gemini connus du dépôt ({', '.join(GOOGLE_MODELS)})."
                )

    # --- already seeded -------------------------------------------------
    if existing is not None:
        print(
            f"\nAgent « {name} » already exists (id={existing.id}, "
            f"uuid={existing.workflow_uuid}, status={existing.status})."
        )
        print(f"  Modèle actuel de cet agent : {_describe_override(stored_override)}")

        current_definition = existing.workflow_definition or {}

        definition_update: dict | None = None
        if args.refresh_prompts:
            if not current_definition.get("nodes"):
                print(
                    "  Cet agent n'a pas de définition exploitable "
                    f"(released_definition_id={existing.released_definition_id}). "
                    "Rien n'a été modifié.",
                    file=sys.stderr,
                )
                return 1
            # Tools an operator attached in the editor would be lost by a full
            # rebuild, so they are read back and re-attached.
            preserved = tool_uuids or collect_tool_uuids(current_definition)
            definition_update = build_jeb_workflow(preserved)
            errors = _validate_definition(definition_update)
            if errors:
                print(
                    "  Le graphe régénéré est invalide ; rien n'a été écrit :",
                    file=sys.stderr,
                )
                for error in errors:
                    print(f"    {error}", file=sys.stderr)
                return 1
            if preserved:
                print(f"  Outils conservés sur le nœud Coordonnées : {preserved}")
        elif tool_uuids:
            print(
                "  --tool-uuid n'a aucun effet sur un agent existant sans "
                "--refresh-prompts : le graphe n'est pas réécrit."
            )

        configurations_update: dict | None = None
        if override is not None and override != stored_override:
            configurations_update = _merge_override(
                existing.workflow_configurations, override
            )

        if definition_update is None and configurations_update is None:
            if override is not None:
                print("  Configuration Gemini Live déjà à jour — rien à changer.")
            if not args.refresh_prompts:
                print(
                    "  Les prompts ne sont pas réécrits par défaut : relancez avec "
                    "--refresh-prompts pour reprendre la dernière version du "
                    "module (cela remplace les modifications faites dans "
                    "l'éditeur)."
                )
            _print_test_steps(existing.id, has_override=bool(stored_override))
            return 0

        # Read the draft BEFORE the dry-run exit, or --dry-run under-reports the
        # one surprising effect of a real run: publishing someone's work.
        draft = await db_client.get_draft_version(existing.id)

        if args.dry_run:
            if definition_update is not None:
                print("  --dry-run : les prompts seraient réécrits depuis le module.")
            if configurations_update is not None:
                print(
                    "  --dry-run : la configuration Gemini Live serait attachée à "
                    "cet agent seulement."
                )
            if draft is not None:
                print(
                    f"  --dry-run : un brouillon non publié (v{draft.version_number}) "
                    "existe, donc un vrai passage écrirait dessus et vous "
                    "laisserait publier."
                )
            return 0

        await db_client.update_workflow(
            workflow_id=existing.id,
            name=None,
            workflow_definition=definition_update,
            template_context_variables=None,
            workflow_configurations=configurations_update,
            organization_id=organization.id,
        )
        print("  Brouillon enregistré.")

        # An existing draft is work in progress that its author has not chosen to
        # release. Publishing it as a side effect of this script would push a
        # half-edited agent live, so the draft wins over the default.
        published_now = False
        if draft is not None:
            print(
                f"  Un brouillon non publié (v{draft.version_number}) était déjà "
                "dans l'éditeur : il N'A PAS été publié. Vos modifications et "
                "celles-ci sont dans le même brouillon ; publiez depuis l'éditeur "
                "quand c'est prêt."
            )
        elif args.draft_only:
            print(
                "  --draft-only : la version publiée est inchangée. Publiez depuis "
                "l'éditeur pour que les appels l'utilisent."
            )
        else:
            published = await db_client.publish_workflow_draft(existing.id)
            published_now = True
            print(f"  Publié en version {published.version_number}.")

        # Only used to decide whether to warn that test calls will fall back to
        # the organization's configuration.
        has_override = bool(stored_override) or (
            configurations_update is not None and published_now
        )
        _print_test_steps(existing.id, has_override=has_override)
        return 0

    # --- first run ------------------------------------------------------
    definition = build_jeb_workflow(tool_uuids)
    errors = _validate_definition(definition)
    if errors:
        # A bug in jeb_agent.py, not in the install. Say so and write nothing.
        print("Generated definition is invalid; nothing was written:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(
            f"\n--dry-run : l'agent « {name} » serait créé pour l'organisation "
            f"{organization.id} avec {len(tool_uuids)} outil(s) attaché(s)."
        )
        if override is not None:
            print(
                "--dry-run : la configuration Gemini Live serait attachée à cet "
                "agent seulement, sans toucher à celle de l'organisation."
            )
        return 0

    workflow = await db_client.create_workflow(
        name=name,
        workflow_definition=definition,
        user_id=owner.id,
        organization_id=organization.id,
    )
    print(
        f"\nAgent « {name} » créé (id={workflow.id}, "
        f"uuid={workflow.workflow_uuid}), publié en version 1."
    )
    print(f"  Ouvrez-le sur /workflow/{workflow.id}")
    # Re-running is safe because this script recognises the agent by name. That
    # is also its one blind spot: renaming the agent in the UI makes the next run
    # create a second one. Said here rather than discovered later.
    print(
        "  Relancer ce script est sans danger : il retrouve l'agent par son nom. "
        'Si vous le renommez, passez --name "<nouveau nom>" la prochaine fois.'
    )
    if tool_uuids:
        print(f"  Outils attachés au nœud Coordonnées : {tool_uuids}")

    published_override = False
    if override is not None:
        # `create_workflow` takes no configurations, so the override goes in as a
        # second version: a draft, then published, which is also what the editor
        # would produce.
        await db_client.update_workflow(
            workflow_id=workflow.id,
            name=None,
            workflow_definition=None,
            template_context_variables=None,
            workflow_configurations=_merge_override(
                workflow.workflow_configurations, override
            ),
            organization_id=organization.id,
        )
        if args.draft_only:
            print(
                "  --draft-only : la configuration Gemini Live est dans un "
                "brouillon NON publié. La version 1 publiée tourne donc encore "
                "sur la configuration de l'organisation ; publiez depuis "
                "l'éditeur."
            )
        else:
            published = await db_client.publish_workflow_draft(workflow.id)
            published_override = True
            print(
                "  Configuration Gemini Live attachée à cet agent seulement, "
                f"publiée en version {published.version_number}."
            )

    _print_test_steps(workflow.id, has_override=published_override)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create the JEB Training Center agent for an organization and attach "
            "its own Gemini Live configuration. Idempotent; never modifies the "
            "organization's model configuration."
        )
    )
    parser.add_argument(
        "--organization-id",
        type=int,
        default=None,
        help="Target organization. Inferred when the install has exactly one.",
    )
    parser.add_argument(
        "--user-email",
        default=None,
        help="Owner of the agent. Inferred when the org has one member.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Owner of the agent, by id (alternative to --user-email).",
    )
    parser.add_argument(
        "--name",
        default=JEB_WORKFLOW_NAME,
        help="Agent name used to create it and to detect it on re-runs.",
    )
    parser.add_argument(
        "--tool-uuid",
        action="append",
        default=None,
        help=(
            "Tool the contact-collection node may invoke (repeatable). Optional: "
            "the agent works without one."
        ),
    )
    parser.add_argument(
        "--refresh-prompts",
        action="store_true",
        help=(
            "Rebuild an existing agent's graph and prompts from the module. "
            "Replaces edits made in the editor."
        ),
    )
    parser.add_argument(
        "--draft-only",
        action="store_true",
        help="Stop at the draft instead of publishing it.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "Gemini API key. Defaults to GEMINI_API_KEY, then GOOGLE_API_KEY. "
            "Never stored in this repository."
        ),
    )
    parser.add_argument(
        "--realtime-model",
        default=DEFAULT_REALTIME_MODEL,
        help=f"Gemini Live model (default: {DEFAULT_REALTIME_MODEL}).",
    )
    parser.add_argument(
        "--llm-model",
        default=DEFAULT_LLM_MODEL,
        help=(
            "Gemini text model used for the extraction pass "
            f"(default: {DEFAULT_LLM_MODEL})."
        ),
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=(
            f"Gemini Live voice (default: {DEFAULT_VOICE}). One of: "
            f"{', '.join(GOOGLE_REALTIME_VOICES)}."
        ),
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help=(
            f"Opening language of the session (default: {DEFAULT_LANGUAGE}). "
            "'ary' selects Moroccan darija, which Gemini Live receives as 'ar'."
        ),
    )
    parser.add_argument(
        "--no-model-override",
        action="store_true",
        help=(
            "Do not attach or update this agent's model configuration; a new "
            "agent then runs on the organization's, and an override already in "
            "place is left untouched."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and validate everything, write nothing.",
    )
    args = parser.parse_args()

    if args.user_email and args.user_id is not None:
        parser.error("pass either --user-email or --user-id, not both")
    if args.api_key and args.no_model_override:
        parser.error("--api-key and --no-model-override contradict each other")

    raise SystemExit(asyncio.run(seed(args)))


if __name__ == "__main__":
    main()
