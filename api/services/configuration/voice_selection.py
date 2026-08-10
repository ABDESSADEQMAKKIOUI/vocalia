"""The one part of the model configuration a non-admin user may change.

Model configuration belongs to the administrator: which provider, which model,
which API keys. What an agent's owner picks is narrower — the language the agent
speaks and the voice it speaks in — and that choice arrives through the same
``model_configuration_v2_override`` field as everything else.

That shared field is the problem this module exists to solve. Hiding the model
form in the browser is not access control: the update route accepts a whole
configuration object, so anyone who can save an agent can post a different
provider, a different model, or their own API key, and the administrator's
choice is gone. Rejecting the field outright is not an option either — it is
how the voice picker saves.

So the server stops trusting the shape it receives. For a non-privileged caller
it rebuilds the override itself from the configuration already in force and
grafts back exactly two values. Anything else in the payload is discarded, not
because it is malformed, but because it was never that caller's to send.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

# Where the speaking voice and its language live inside a v2 configuration.
# Realtime only for now: the user-facing picker offers Gemini Live voices, and a
# pipeline configuration spreads the same two ideas across separate TTS and STT
# sections whose "language" values are not interchangeable.
_REALTIME_PATH = ("byok", "realtime", "realtime")
_USER_EDITABLE_FIELDS = ("voice", "language")


def _dig(config: Any, path: tuple[str, ...]) -> dict[str, Any] | None:
    """Follow ``path`` through nested dicts, or ``None`` if it does not exist."""
    node: Any = config
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, dict) else None


def extract_voice_selection(configuration: Any) -> dict[str, Any]:
    """The voice and language carried by a configuration, if any.

    Missing keys are omitted rather than returned as ``None``: a caller who sent
    no voice is asking to keep the current one, not to clear it.
    """
    realtime = _dig(configuration, _REALTIME_PATH)
    if realtime is None:
        return {}
    return {
        field: realtime[field]
        for field in _USER_EDITABLE_FIELDS
        if realtime.get(field) is not None
    }


def apply_voice_selection(
    base: dict[str, Any], selection: dict[str, Any]
) -> dict[str, Any]:
    """Return ``base`` with the voice selection grafted onto its realtime block.

    ``base`` is not mutated — it is usually the organization's own configuration,
    shared with other callers in the same request.
    """
    if not selection:
        return base

    realtime = _dig(base, _REALTIME_PATH)
    if realtime is None:
        # No realtime block to graft onto. Silently creating one would invent a
        # provider and a model the administrator never chose, so the selection
        # is dropped and said out loud instead.
        logger.warning(
            "Ignoring a voice selection: the effective model configuration has no "
            "realtime block to apply it to."
        )
        return base

    merged = _deep_copy_dicts(base)
    target = _dig(merged, _REALTIME_PATH)
    assert target is not None  # the shape was just checked on `base`
    target.update(selection)
    return merged


def _deep_copy_dicts(value: Any) -> Any:
    """Copy nested dicts and lists, leaving leaves shared.

    ``copy.deepcopy`` would also duplicate the leaves; only the containers on the
    path being edited need to be private.
    """
    if isinstance(value, dict):
        return {key: _deep_copy_dicts(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy_dicts(item) for item in value]
    return value


def restrict_override_to_voice_selection(
    incoming: Any, base: dict[str, Any]
) -> dict[str, Any]:
    """What a non-privileged caller is actually allowed to have saved.

    ``base`` is the configuration already in force for this agent — its own
    override if it has one, otherwise the organization's. Everything the caller
    sent is discarded except the voice and the language.
    """
    selection = extract_voice_selection(incoming)
    restricted = apply_voice_selection(base, selection)
    if selection:
        logger.info(
            f"Restricted a non-superuser model override to the voice selection "
            f"{selection}; the rest of the configuration was taken from the one "
            f"already in force."
        )
    else:
        logger.info(
            "A non-superuser posted a model override carrying no voice selection; "
            "the configuration already in force was kept unchanged."
        )
    return restricted


__all__ = [
    "apply_voice_selection",
    "extract_voice_selection",
    "restrict_override_to_voice_selection",
]
