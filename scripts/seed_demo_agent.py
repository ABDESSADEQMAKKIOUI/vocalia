"""Seed the Lina demo agent, and wire it to the organization's Google Sheet.

Lina is a prospect call-back assistant whose qualification step reads the
prospect's row from a bound spreadsheet and writes the outcome back — the whole
point being to exercise the Google Sheets integration on a real call without
assembling a workflow by hand. Her graph lives in
`api/services/workflow/demo_agents.py`.

The sheet tool only exists once someone has connected Google and bound a
spreadsheet, so this script does whatever the install allows:

  * a bound sheet tool is found  → Lina is created (or rewired) with it attached;
  * none is found               → Lina is created anyway, without a tool, and
                                  the remaining steps are printed.

Idempotent by design: re-running it never creates a second Lina. It performs no
destructive write — it creates a workflow, and with --rewire saves a new draft
version and publishes it (the previous published version is kept as an archived
version, as with any edit in the editor).

Run from the repo root with the api environment available:

    python -m scripts.seed_demo_agent

Inside the SaaS docker stack (docker-compose.saas.yaml mounts this file into the
api container, like scripts/bootstrap_platform_admin.py — the image itself only
ships the shell entrypoints from scripts/):

    docker compose -f docker-compose.saas.yaml exec api \
        python -m scripts.seed_demo_agent

On a container started before that mount existed, copy the file in rather than
recreating the container — recreating it drops calls in progress:

    docker cp api/services/workflow/demo_agents.py         <container>:/app/api/services/workflow/
    docker cp scripts/seed_demo_agent.py <container>:/app/scripts/
    docker exec <container> python -m scripts.seed_demo_agent

Both files are needed: this script imports the graph from demo_agents, and an
image built before that module existed does not carry it either.

Once Google is connected and a sheet is bound, attach the tool to the Lina that
already exists:

    docker compose -f docker-compose.saas.yaml exec api \
        python -m scripts.seed_demo_agent --rewire
"""

import argparse
import asyncio
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
from api.db.models import (  # noqa: E402
    OrganizationModel,
    ToolModel,
    UserModel,
    WorkflowModel,
)
from api.enums import ToolStatus  # noqa: E402
from api.services.integrations.google.oauth import GOOGLE_PROVIDER  # noqa: E402
from api.services.workflow.demo_agents import (  # noqa: E402
    LINA_WORKFLOW_NAME,
    attach_tools,
    build_lina_workflow,
)
from api.services.workflow.dto import ReactFlowDTO  # noqa: E402
from api.services.workflow.tools.custom_tool import SHEETS_TOOL_TYPE  # noqa: E402
from api.services.workflow.workflow_graph import WorkflowGraph  # noqa: E402


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
    """The user the demo agent is created under, checked against the org.

    Membership is verified rather than assumed: `workflows.user_id` is the
    owner recorded on the agent, and pointing it at someone outside the
    organization would create a row nobody in that org owns.
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
                f"Organization {organization.id} has no member to own the demo "
                "agent.",
                file=sys.stderr,
            )
            return None
        if len(members) > 1:
            print(
                f"Organization {organization.id} has {len(members)} members; "
                "pass --user-email or --user-id to say who owns the demo agent:",
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
            f"User id={user.id} is not a member of organization "
            f"{organization.id}.",
            file=sys.stderr,
        )
        return None
    return user


async def _find_sheets_tools(organization_id: int) -> list[ToolModel]:
    """Active tools of this organization that are bound to a Google Sheet."""
    tools = await db_client.get_tools_for_organization(
        organization_id, status=ToolStatus.ACTIVE.value
    )
    return [
        tool
        for tool in tools
        if (tool.definition or {}).get("type") == SHEETS_TOOL_TYPE
    ]


def _describe_tool(tool: ToolModel) -> str:
    config = (tool.definition or {}).get("config", {}) or {}
    title = config.get("spreadsheet_title") or config.get("spreadsheet_id") or "?"
    sheet = config.get("sheet_name") or "?"
    return f"{tool.name} ({tool.tool_uuid}) → « {title} », onglet « {sheet} »"


async def _google_is_connected(organization_id: int) -> bool:
    """Whether the org holds a Google OAuth credential (no decryption needed)."""
    credentials = await db_client.get_credentials_for_organization(organization_id)
    return any(
        (credential.credential_data or {}).get("provider") == GOOGLE_PROVIDER
        for credential in credentials
    )


async def _find_existing(organization_id: int, name: str) -> WorkflowModel | None:
    """The already-seeded Lina, whatever her status — that is the idempotency."""
    workflows = await db_client.get_all_workflows(organization_id=organization_id)
    matches = [
        workflow for workflow in workflows if (workflow.name or "").strip() == name
    ]
    return matches[0] if matches else None


def _tools_present(definition: dict, tool_uuids: list[str]) -> bool:
    """Whether every wanted uuid is already carried by some node of the graph."""
    attached: set[str] = set()
    for node in definition.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        data = node.get("data")
        if isinstance(data, dict):
            attached.update(str(uuid) for uuid in (data.get("tool_uuids") or []))
    return all(uuid in attached for uuid in tool_uuids)


def _validate_definition(definition: dict) -> list[str]:
    """Run the definition through the same checks the editor and runtime use.

    An invalid graph would break the workflow editor for the owner, which is
    worse than shipping no demo agent at all — so this runs before any write.
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
# Output
# ─────────────────────────────────────────────────────────────────────────


def _print_next_steps(
    *,
    google_connected: bool,
    has_tool: bool,
    name: str,
    sheets_found: int = 0,
) -> None:
    """Tell the owner, in their own language, what is left to do."""
    if has_tool:
        print("\nPour tester :")
        print(
            "  1. Ouvrez l'agent dans Volira, puis lancez un appel de test "
            "(bouton « Test »)."
        )
        print(
            "  2. Vérifiez dans la feuille que la ligne du prospect a bien été "
            "mise à jour à la fin de l'appel."
        )
        return

    if sheets_found > 1:
        print(
            f"\n{sheets_found} feuilles sont liées à cette organisation ; le "
            "script n'en choisit aucune tout seul."
        )
        print(f"L'agent « {name} » est en place mais ne porte pas encore d'outil.")
        print("\nÉtape restante — désignez la feuille à utiliser :")
        print(
            "       docker compose -f docker-compose.saas.yaml exec api "
            "python -m scripts.seed_demo_agent --rewire --tool-uuid <uuid>"
        )
        print("  (les uuid sont listés ci-dessus)")
        return

    print("\nAucune feuille Google n'est reliée à cette organisation.")
    print(f"L'agent « {name} » est en place et déjà utilisable pour un appel de test,")
    print("mais il ne consulte ni n'enregistre encore quoi que ce soit.")
    print("\nÉtapes restantes :")
    if not google_connected:
        print(
            "  1. Connectez Google : dans Volira, ouvrez /integrations/google et "
            "autorisez le compte."
        )
    else:
        print(
            "  1. Google est déjà connecté pour cette organisation — rien à "
            "faire ici."
        )
    print(
        "  2. Liez une feuille : toujours sur /integrations/google, choisissez le "
        "classeur et l'onglet."
    )
    print(
        "     Les colonnes sont déduites automatiquement ; relisez-les avant de "
        "valider."
    )
    print("  3. Relancez ce script pour attacher l'outil à l'agent :")
    print(
        "       docker compose -f docker-compose.saas.yaml exec api "
        "python -m scripts.seed_demo_agent --rewire"
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

    # --- which sheet tool, if any -------------------------------------
    tool_uuids: list[str] = []
    sheets_tools = await _find_sheets_tools(organization.id)

    if args.tool_uuid:
        selected = [
            tool for tool in sheets_tools if str(tool.tool_uuid) == args.tool_uuid
        ]
        if not selected:
            print(
                f"No active Google Sheets tool with uuid {args.tool_uuid} in "
                f"organization {organization.id}.",
                file=sys.stderr,
            )
            return 1
        tool_uuids = [str(selected[0].tool_uuid)]
        print(f"Sheet tool: {_describe_tool(selected[0])}")
    elif len(sheets_tools) == 1:
        tool_uuids = [str(sheets_tools[0].tool_uuid)]
        print(f"Sheet tool detected: {_describe_tool(sheets_tools[0])}")
    elif len(sheets_tools) > 1:
        # Attaching the wrong spreadsheet would have the agent read and write
        # someone else's data, so this never guesses.
        print(
            f"{len(sheets_tools)} Google Sheets tools found; pass --tool-uuid to "
            "say which one:"
        )
        for tool in sheets_tools:
            print(f"  {_describe_tool(tool)}")
        print("  (continuing without attaching a tool)")

    google_connected = await _google_is_connected(organization.id)

    existing = await _find_existing(organization.id, name)

    # --- already seeded ------------------------------------------------
    if existing is not None:
        print(
            f"Agent « {name} » already exists (id={existing.id}, "
            f"uuid={existing.workflow_uuid}, status={existing.status})."
        )

        if not args.rewire:
            if tool_uuids:
                print(
                    "  A Google Sheets tool is available — re-run with --rewire "
                    "to attach it to this agent."
                )
            else:
                _print_next_steps(
                    google_connected=google_connected,
                    has_tool=False,
                    name=name,
                    sheets_found=len(sheets_tools),
                )
            return 0

        if not tool_uuids:
            print("  --rewire: no Google Sheets tool to attach.")
            _print_next_steps(
                google_connected=google_connected,
                has_tool=False,
                name=name,
                sheets_found=len(sheets_tools),
            )
            return 0

        current = existing.workflow_definition or {}
        if not current.get("nodes"):
            released = existing.released_definition_id
            print(
                "  This agent has no usable definition to rewire "
                f"(released_definition_id={released}). Leaving it untouched.",
                file=sys.stderr,
            )
            return 1

        updated, changed = attach_tools(current, tool_uuids)
        if not changed:
            # attach_tools declines for two very different reasons: the tool is
            # already there, or it could not tell which node should carry it.
            # Only one of those is success.
            if _tools_present(current, tool_uuids):
                print("  Tool already attached — nothing to change.")
                return 0
            print(
                "  Could not find the node that should carry the sheet tool — "
                "this agent's graph is no longer the one the seeder builds. "
                "Attach the tool from the editor instead. Nothing was written.",
                file=sys.stderr,
            )
            return 1

        errors = _validate_definition(updated)
        if errors:
            print(
                "  Rewired definition is invalid; nothing was written:",
                file=sys.stderr,
            )
            for error in errors:
                print(f"    {error}", file=sys.stderr)
            return 1

        # Read the draft BEFORE the dry-run exit, or --dry-run under-reports the
        # one surprising effect of a real run: publishing someone's work.
        draft = await db_client.get_draft_version(existing.id)

        if args.dry_run:
            print("  --dry-run: the tool would be attached to the qualification node.")
            if draft is not None:
                print(
                    f"  --dry-run: an unpublished draft (v{draft.version_number}) "
                    "exists, so a real run would save the tool onto it and leave "
                    "publishing to you."
                )
            return 0

        await db_client.update_workflow(
            workflow_id=existing.id,
            name=None,
            workflow_definition=updated,
            template_context_variables=None,
            workflow_configurations=None,
            organization_id=organization.id,
        )
        print("  Draft saved with the sheet tool attached.")

        # An existing draft is work in progress that its author has not chosen
        # to release. Publishing it as a side effect of attaching a tool would
        # push a half-edited agent live — so the draft wins over the default and
        # the decision goes back to whoever was editing.
        if draft is not None:
            print(
                f"  An unpublished draft (v{draft.version_number}) was already in "
                "the editor, so it was NOT published: the tool was added on top of "
                "your edits. Publish from the editor when your changes are ready."
            )
        elif args.draft_only:
            print(
                "  --draft-only: the published version is unchanged. Publish from "
                "the editor to make calls use it."
            )
        else:
            published = await db_client.publish_workflow_draft(existing.id)
            print(f"  Published as version {published.version_number}.")

        _print_next_steps(google_connected=google_connected, has_tool=True, name=name)
        return 0

    # --- first run -----------------------------------------------------
    definition = build_lina_workflow(tool_uuids)
    errors = _validate_definition(definition)
    if errors:
        # A bug in demo_agents.py, not in the install. Say so and write nothing.
        print("Generated definition is invalid; nothing was written:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(
            f"--dry-run: agent « {name} » would be created for organization "
            f"{organization.id} with {len(tool_uuids)} tool(s) attached."
        )
        return 0

    workflow = await db_client.create_workflow(
        name=name,
        workflow_definition=definition,
        user_id=owner.id,
        organization_id=organization.id,
    )
    print(
        f"Created agent « {name} » (id={workflow.id}, "
        f"uuid={workflow.workflow_uuid}), published as version 1."
    )
    print(f"  Open it at /workflow/{workflow.id}")
    # Re-running is safe because this script recognises the agent by name. That
    # is also its one blind spot: renaming the agent in the UI makes the next
    # run create a second one. Said here rather than discovered later.
    print(
        f'  Re-running this script is safe. It finds the agent by name, so if '
        f'you rename it, pass --name "<new name>" next time.'
    )
    if tool_uuids:
        print(f"  Sheet tool attached to the qualification node: {tool_uuids[0]}")

    _print_next_steps(
        google_connected=google_connected,
        has_tool=bool(tool_uuids),
        name=name,
        sheets_found=len(sheets_tools),
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create the Lina demo agent for an organization and attach its "
            "Google Sheets tool when one is bound. Idempotent."
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
        help="Owner of the demo agent. Inferred when the org has one member.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Owner of the demo agent, by id (alternative to --user-email).",
    )
    parser.add_argument(
        "--tool-uuid",
        default=None,
        help=(
            "Google Sheets tool to attach. Auto-detected when the organization "
            "has exactly one bound sheet."
        ),
    )
    parser.add_argument(
        "--rewire",
        action="store_true",
        help="Attach the sheet tool to an agent that already exists.",
    )
    parser.add_argument(
        "--draft-only",
        action="store_true",
        help="With --rewire, stop at the draft instead of publishing it.",
    )
    parser.add_argument(
        "--name",
        default=LINA_WORKFLOW_NAME,
        help="Agent name used to create it and to detect it on re-runs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and validate everything, write nothing.",
    )
    args = parser.parse_args()

    if args.user_email and args.user_id is not None:
        parser.error("pass either --user-email or --user-id, not both")
    # A first run always publishes v1, so --draft-only on its own promises a
    # restraint the script cannot honour. Refusing beats silently ignoring it.
    if args.draft_only and not args.rewire:
        parser.error("--draft-only only applies together with --rewire")

    raise SystemExit(asyncio.run(seed(args)))


if __name__ == "__main__":
    main()
