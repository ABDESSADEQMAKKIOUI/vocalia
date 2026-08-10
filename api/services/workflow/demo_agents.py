"""Ready-made demo agents — currently Lina, the Google Sheets walkthrough.

An "agent" in this product is a workflow: a `{nodes, edges, viewport}` graph
persisted on `workflows.workflow_definition`. This module builds one such graph
by hand so an owner can try the Google Sheets integration on a real call without
first assembling a workflow node by node.

Why the tool is a parameter and not a constant
----------------------------------------------
The Sheets tool only exists after an organization has connected Google *and*
bound a spreadsheet — `integrations/google/sheets_binding.py::bind_sheet` is
what mints the `ToolModel` and its `tool_uuid`. A demo agent therefore cannot
ship pre-wired to a tool that does not exist yet. `build_lina_workflow` takes
the uuids it should attach and produces a graph that is valid either way: with
tools, Lina reads and writes the sheet; without, she still runs a plain
qualification call and is told, in her own prompt, not to pretend otherwise.

Why no column names appear in the prompts
-----------------------------------------
The tool's LLM parameters *are* the bound sheet's own columns, inferred at bind
time by `sheet_schema.infer_sheet_schema`. Hard-coding "Nom" or "Téléphone" in a
prompt would break on the next customer's spreadsheet — exactly the failure the
inference removed. The prompts therefore tell Lina to read the parameter names
and descriptions the tool hands her, and to never invent one.

Prompts are in French: that is the market this demo is written for.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

# Name the seeding script looks for to stay idempotent. Changing it makes an
# already-seeded install look un-seeded, so treat it as a stable identifier.
LINA_WORKFLOW_NAME = "Lina — Rappel de prospects (démo)"

# Numeric-looking ids on purpose: the editor derives the next node id with
# `parseInt` over the existing ones (ui/src/lib/utils.ts::getNextNodeId), and
# non-numeric ids would make it restart at 1 and collide.
NODE_ID_GLOBAL = "100"
NODE_ID_START = "101"
NODE_ID_AGENT = "102"
NODE_ID_END = "103"

# The one node that carries `tool_uuids`: it is where the prospect's row is read
# and where the outcome of the call is written back.
LINA_TOOL_NODE_ID = NODE_ID_AGENT


# ─────────────────────────────────────────────────────────────────────────
# Prompts
#
# Kept as module constants rather than inlined so the seeding script can tell
# an untouched prompt from one the owner has edited before rewriting it.
# ─────────────────────────────────────────────────────────────────────────

GLOBAL_PROMPT = (
    "Tu es Lina, l'assistante téléphonique de {{company_name | Volira}}.\n"
    "Ton mode d'interaction est la voix : parle en phrases courtes, dans un "
    "français simple et naturel, et n'utilise aucun caractère spécial qui ne se "
    "prononce pas.\n"
    "Laisse la personne répondre : ne dis jamais plus de deux phrases d'affilée.\n"
    "N'invente aucune information. Si tu ne sais pas, dis-le.\n"
    "Si la personne demande à ne plus être appelée, accepte immédiatement, "
    "n'argumente pas et conclus l'appel."
)

START_GREETING = (
    "Bonjour, ici Lina, de la part de {{company_name | Volira}}. "
    "Je vous appelle au sujet de votre demande d'information. "
    "Est-ce que je parle bien à la bonne personne ?"
)

START_PROMPT = (
    "Tu ouvres l'appel. Ton seul objectif à cette étape est de vérifier que tu "
    "parles bien à la personne qui a laissé une demande d'information, et "
    "qu'elle a un moment devant elle.\n"
    "Annonce qui tu es et d'où tu appelles, puis pose la question de "
    "vérification. Ne parle ni de l'offre, ni des besoins de la personne pour "
    "l'instant.\n"
    "Si elle confirme son identité et accepte de parler, enchaîne sur la "
    "qualification.\n"
    "Si ce n'est pas la bonne personne, si le moment est mal choisi ou si elle "
    "refuse, n'insiste pas et conclus l'appel."
)

# The qualification prompt has two halves: what Lina does on every call, and
# what she does with the spreadsheet. The second half only appears when a tool
# is actually attached — telling an agent to "consult the sheet" when it holds
# no sheet tool is an invitation to invent the answer.
_AGENT_PROMPT_BASE = (
    "Tu qualifies le besoin de la personne.\n"
    "Pose tes questions une par une, écoute la réponse et reformule brièvement "
    "pour confirmer que tu as bien compris.\n"
    "Cherche à établir trois choses : ce que la personne veut faire, dans quel "
    "délai, et qui décide.\n"
    "Ne promets ni prix, ni délai, ni engagement : tu prépares seulement la "
    "reprise de contact par un conseiller.\n"
    "Quand tu as ces éléments, ou si la personne ne souhaite pas aller plus "
    "loin, conclus l'appel."
)

_AGENT_PROMPT_WITH_TOOL = (
    "\n\n"
    "Tu disposes d'un outil relié à la feuille de calcul de l'entreprise. "
    "Sers-t'en deux fois pendant cet appel.\n"
    "1. Au début de cette étape, consulte la fiche du prospect : lance une "
    "lecture de la feuille pour retrouver la ligne de cette personne, et "
    "sers-t'en pour ne pas redemander ce qui est déjà connu.\n"
    "2. Avant de conclure, enregistre le résultat de l'appel dans la feuille, "
    "sur la ligne de cette personne.\n"
    "Les paramètres de l'outil sont exactement les colonnes de la feuille : lis "
    "leurs noms et leurs descriptions au moment de l'appeler, puis choisis "
    "toi-même celle qui identifie la personne et celles où consigner le "
    "résultat. N'invente jamais un nom de colonne, et ne remplis pas une "
    "colonne dont tu n'as pas la valeur.\n"
    "Si l'outil renvoie une erreur, ne la répète pas mot pour mot : poursuis "
    "l'appel normalement et signale simplement que tu n'as pas pu enregistrer."
)

_AGENT_PROMPT_WITHOUT_TOOL = (
    "\n\n"
    "Aucune feuille de calcul n'est reliée à cet agent pour le moment. Ne "
    "prétends donc ni consulter une fiche, ni enregistrer quoi que ce soit : "
    "mène simplement la conversation."
)

END_PROMPT = (
    "Tu conclus l'appel.\n"
    "Si la qualification a eu lieu, récapitule en une phrase ce que tu as "
    "retenu, annonce qu'un conseiller reprendra contact, remercie et raccroche.\n"
    "Si l'appel s'arrête parce que ce n'était pas la bonne personne ou pas le "
    "bon moment, excuse-toi du dérangement, remercie et raccroche.\n"
    "Reste brève : deux phrases suffisent."
)

EXTRACTION_PROMPT = (
    "Relis l'échange et note uniquement ce que la personne a réellement dit. "
    "Laisse vide ce qui n'a pas été abordé ; n'extrapole pas."
)

# Extraction variables are this workflow's own names for what it captured. They
# are deliberately NOT sheet columns: the sheet's columns belong to the customer
# and are resolved by the tool at call time.
EXTRACTION_VARIABLES: List[Dict[str, Any]] = [
    {
        "name": "besoin_exprime",
        "type": "string",
        "prompt": "Ce que la personne veut faire, dans ses propres mots.",
    },
    {
        "name": "echeance",
        "type": "string",
        "prompt": "Le délai dans lequel elle souhaite avancer.",
    },
    {
        "name": "resultat_appel",
        "type": "string",
        "prompt": (
            "Issue de l'appel : intéressé, à rappeler, pas intéressé, ou "
            "mauvais interlocuteur."
        ),
    },
]


def agent_prompt(has_tool: bool) -> str:
    """The qualification prompt, with or without the spreadsheet instructions."""
    return _AGENT_PROMPT_BASE + (
        _AGENT_PROMPT_WITH_TOOL if has_tool else _AGENT_PROMPT_WITHOUT_TOOL
    )


# ─────────────────────────────────────────────────────────────────────────
# Graph
# ─────────────────────────────────────────────────────────────────────────


def _edge(source: str, target: str, label: str, condition: str) -> Dict[str, Any]:
    """One ReactFlow edge, shaped like the ones the editor writes."""
    return {
        "id": f"xy-edge__{source}-{target}",
        "type": "custom",
        "animated": True,
        "source": source,
        "target": target,
        "data": {"label": label, "condition": condition},
    }


def build_lina_workflow(tool_uuids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build Lina's workflow definition.

    Args:
        tool_uuids: Tool uuids the qualification node may invoke — in practice
            the Google Sheets tool minted by `bind_sheet`. Empty or None
            produces the same graph without any tool attached; it stays valid
            and usable for a plain test call.

    Returns:
        A `{nodes, edges, viewport}` dict that validates against
        `api.services.workflow.dto.ReactFlowDTO` and satisfies the graph
        constraints enforced by `WorkflowGraph`.
    """
    attached = [str(uuid) for uuid in (tool_uuids or []) if str(uuid).strip()]

    agent_data: Dict[str, Any] = {
        "name": "Qualification du besoin",
        "prompt": agent_prompt(bool(attached)),
        "allow_interrupt": True,
        "add_global_prompt": True,
        "extraction_enabled": True,
        "extraction_prompt": EXTRACTION_PROMPT,
        "extraction_variables": deepcopy(EXTRACTION_VARIABLES),
    }
    if attached:
        agent_data["tool_uuids"] = attached

    nodes: List[Dict[str, Any]] = [
        {
            # Persona shared by every prompted node with add_global_prompt=true.
            "id": NODE_ID_GLOBAL,
            "type": "globalNode",
            "position": {"x": 120, "y": 40},
            "data": {
                "name": "Persona Lina",
                "prompt": GLOBAL_PROMPT,
            },
        },
        {
            "id": NODE_ID_START,
            "type": "startCall",
            "position": {"x": 620, "y": 40},
            "data": {
                "name": "Prise de contact",
                "is_start": True,
                "prompt": START_PROMPT,
                "greeting_type": "text",
                "greeting": START_GREETING,
                "allow_interrupt": True,
                "add_global_prompt": True,
                # Outbound: give the called party a beat to settle before
                # speaking, otherwise the greeting lands on a half-raised phone.
                "delayed_start": True,
                "delayed_start_duration": 2.0,
            },
        },
        {
            "id": NODE_ID_AGENT,
            "type": "agentNode",
            "position": {"x": 620, "y": 320},
            "data": agent_data,
        },
        {
            "id": NODE_ID_END,
            "type": "endCall",
            "position": {"x": 620, "y": 620},
            "data": {
                "name": "Conclusion",
                "is_end": True,
                "prompt": END_PROMPT,
                "add_global_prompt": True,
            },
        },
    ]

    edges = [
        _edge(
            NODE_ID_START,
            NODE_ID_AGENT,
            "Bon interlocuteur",
            "La personne confirme son identité et accepte de parler maintenant.",
        ),
        _edge(
            NODE_ID_START,
            NODE_ID_END,
            "Mauvais interlocuteur",
            (
                "Ce n'est pas la bonne personne, le moment est mal choisi, ou "
                "elle ne souhaite pas poursuivre."
            ),
        ),
        _edge(
            NODE_ID_AGENT,
            NODE_ID_END,
            "Qualification terminée",
            (
                "Le besoin a été qualifié, ou la personne ne souhaite pas aller "
                "plus loin."
            ),
        ),
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "viewport": {"x": 0, "y": 0, "zoom": 0.85},
    }


def attach_tools(
    definition: Dict[str, Any], tool_uuids: List[str]
) -> tuple[Dict[str, Any], bool]:
    """Attach tools to an existing Lina without discarding the owner's edits.

    Used by the seeding script when a sheet gets bound *after* Lina was created.
    Rewriting the whole graph would silently undo any prompt the owner tuned in
    the editor, so only two things change:

      * `tool_uuids` on the qualification node (union with what is already
        there — an operator may have attached something else on purpose);
      * that node's prompt, and only if it is still byte-for-byte the toolless
        prompt this module generated. An edited prompt is left alone: a demo
        agent is not worth overwriting someone's work.

    Returns the (new definition, changed?) pair; the input is not mutated.
    """
    wanted = [str(uuid) for uuid in (tool_uuids or []) if str(uuid).strip()]
    if not wanted:
        return definition, False

    updated = deepcopy(definition)
    nodes = updated.get("nodes")
    if not isinstance(nodes, list):
        return definition, False

    target = _find_tool_node(nodes)
    if target is None:
        return definition, False

    data = target.setdefault("data", {})
    existing = [str(uuid) for uuid in (data.get("tool_uuids") or [])]
    merged = existing + [uuid for uuid in wanted if uuid not in existing]

    changed = merged != existing
    data["tool_uuids"] = merged

    if data.get("prompt") == agent_prompt(False):
        data["prompt"] = agent_prompt(True)
        changed = True

    return (updated, True) if changed else (definition, False)


def _find_tool_node(nodes: List[Any]) -> Optional[Dict[str, Any]]:
    """The node that should carry the sheet tool.

    Prefers Lina's own qualification node by id; falls back to the single agent
    node when the ids were renumbered (a duplicated agent, an export/import
    round trip). Several agent nodes and no id match means the graph is no
    longer the one this module built — better to attach nothing than to guess.
    """
    # The type check is not redundant with the id. The editor hands out the next
    # id as max+1, so deleting this node and adding another one gives the new
    # node the same id — and it may well be an endCall, whose data model has no
    # tool_uuids field at all. Writing the tool there would be dropped on the
    # next save while the seeding script reported success.
    by_id = [
        node
        for node in nodes
        if isinstance(node, dict)
        and node.get("id") == LINA_TOOL_NODE_ID
        and node.get("type") == "agentNode"
    ]
    if by_id:
        return by_id[0]

    agents = [
        node
        for node in nodes
        if isinstance(node, dict) and node.get("type") == "agentNode"
    ]
    return agents[0] if len(agents) == 1 else None


__all__ = [
    "LINA_TOOL_NODE_ID",
    "LINA_WORKFLOW_NAME",
    "agent_prompt",
    "attach_tools",
    "build_lina_workflow",
]
