"""Local mirror of Meta WhatsApp message templates.

A mirror row (WhatsAppTemplateModel) is authored locally as a DRAFT,
submitted to the Graph API, and from then on Meta is the source of truth
for review state: status, quality_score and category only change through
template webhooks (handle_template_webhook) or the reconciliation sync
(sync_templates).

The pure helpers (validate_template_definition,
extract_template_placeholders, build_send_components) operate on Meta's
template *creation* component format and perform no I/O.

Placeholder keys: for positional templates the returned keys are the
placeholder numbers as strings ("1", "2", ...). Meta numbers placeholders
per component (a TEXT header variable is always {{1}}), so a header
variable shares its value with the body's {{1}} — campaign senders provide
one value per distinct number. Named templates use the parameter names as
keys.
"""

import re
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from api.db import db_client
from api.db.models import MessagingConfigurationModel, WhatsAppTemplateModel

VALID_CATEGORIES = {"MARKETING", "UTILITY", "AUTHENTICATION"}
VALID_PARAMETER_FORMATS = {"positional", "named"}

# DRAFT was never submitted; REJECTED/PAUSED templates may be edited
# locally and resubmitted. Every other status is Meta-controlled.
LOCALLY_EDITABLE_STATUSES = {"DRAFT", "REJECTED", "PAUSED"}

MEDIA_HEADERS_UNSUPPORTED = "media headers not supported yet"

_TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
_NAMED_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_MAX_BODY_LENGTH = 1024
_MAX_HEADER_LENGTH = 60
_MAX_FOOTER_LENGTH = 60
_MAX_BUTTONS = 10
_MAX_URL_BUTTONS = 2
_MAX_QUICK_REPLY_LENGTH = 25


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _component_type(component: dict) -> str:
    return str(component.get("type") or "").upper()


def _scan_placeholders(text: str | None) -> list[str]:
    """All {{token}} occurrences in order of appearance (with duplicates)."""
    return _PLACEHOLDER_RE.findall(text or "")


def _format_placeholders(text: str | None, parameter_format: str) -> list[str]:
    """Unique placeholder keys for one text field, filtered to the given
    parameter format. Positional keys are numerically sorted."""
    is_named = parameter_format == "named"
    tokens: list[str] = []
    for token in _scan_placeholders(text):
        if token.isdigit() == is_named:
            continue
        if token not in tokens:
            tokens.append(token)
    if not is_named:
        tokens.sort(key=int)
    return tokens


def _first_component(components: list[dict], component_type: str) -> dict | None:
    for component in components or []:
        if isinstance(component, dict) and _component_type(component) == component_type:
            return component
    return None


def _header_format(header: dict) -> str:
    return str(header.get("format") or "TEXT").upper()


def _normalize_parameter_format(parameter_format: str | None) -> str:
    return str(parameter_format or "positional").lower()


def _sequential_error(label: str, tokens: list[str]) -> str | None:
    """Positional tokens must be exactly {{1}}..{{n}} with no gaps."""
    numbers = sorted({int(token) for token in tokens})
    if numbers != list(range(1, len(numbers) + 1)):
        found = ", ".join("{{%d}}" % n for n in numbers)
        return (
            f"{label} positional parameters must be sequential "
            "{{1}}..{{n}}; found " + found
        )
    return None


def _placeholder_format_errors(
    label: str, text: str | None, parameter_format: str
) -> list[str]:
    """Errors for placeholders that do not match the parameter format."""
    errors = []
    for token in dict.fromkeys(_scan_placeholders(text)):
        if parameter_format == "positional" and not token.isdigit():
            errors.append(
                f"{label} contains non-numeric placeholder "
                "'{{%s}}' in a positional template" % token
            )
        elif parameter_format == "named":
            if token.isdigit():
                errors.append(
                    f"{label} contains numeric placeholder "
                    "'{{%s}}' in a named template" % token
                )
            elif not _NAMED_PARAM_RE.match(token):
                errors.append(
                    f"{label} parameter name '%s' is invalid: must match "
                    "^[a-z][a-z0-9_]*$" % token
                )
    return errors


def _named_example_names(entries: Any) -> set[str]:
    if not isinstance(entries, list):
        return set()
    return {
        entry.get("param_name")
        for entry in entries
        if isinstance(entry, dict) and entry.get("param_name")
    }


def validate_template_definition(
    name: str,
    language: str,
    category: str,
    components: list[dict],
    parameter_format: str,
) -> list[str]:
    """Validate a template definition against Meta's creation rules.

    Returns a list of human-readable errors; empty means valid.
    """
    errors: list[str] = []

    if not isinstance(name, str) or not _TEMPLATE_NAME_RE.match(name or ""):
        errors.append(
            f"invalid template name '{name}': "
            "must match ^[a-z0-9_]{1,512}$ (lowercase letters, digits, underscores)"
        )
    if not (language or "").strip():
        errors.append("language is required")
    if str(category or "").upper() not in VALID_CATEGORIES:
        errors.append(
            f"invalid category '{category}': must be one of "
            + ", ".join(sorted(VALID_CATEGORIES))
        )

    fmt = _normalize_parameter_format(parameter_format)
    if fmt not in VALID_PARAMETER_FORMATS:
        errors.append("parameter_format must be 'positional' or 'named'")
        fmt = "positional"

    if not isinstance(components, list) or not components:
        errors.append("components must be a non-empty list")
        return errors

    seen_types: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            errors.append("every component must be an object")
            continue
        ctype = _component_type(component)
        if ctype not in {"HEADER", "BODY", "FOOTER", "BUTTONS"}:
            errors.append(f"unsupported component type '{component.get('type')}'")
            continue
        if ctype in seen_types:
            errors.append(f"duplicate {ctype} component")
        seen_types.add(ctype)

    body = _first_component(components, "BODY")
    if body is None:
        errors.append("a BODY component is required")
    else:
        errors.extend(_validate_body(body, fmt))

    header = _first_component(components, "HEADER")
    if header is not None:
        errors.extend(_validate_header(header, fmt))

    footer = _first_component(components, "FOOTER")
    if footer is not None:
        errors.extend(_validate_footer(footer))

    buttons = _first_component(components, "BUTTONS")
    if buttons is not None:
        errors.extend(_validate_buttons(buttons, fmt))

    return errors


def _validate_body(body: dict, fmt: str) -> list[str]:
    errors: list[str] = []
    text = body.get("text") or ""
    if not text:
        errors.append("BODY text is required")
        return errors
    if len(text) > _MAX_BODY_LENGTH:
        errors.append(
            f"BODY text exceeds {_MAX_BODY_LENGTH} characters ({len(text)})"
        )
    errors.extend(_placeholder_format_errors("BODY", text, fmt))
    tokens = _format_placeholders(text, fmt)
    if not tokens:
        return errors
    if fmt == "positional":
        sequential = _sequential_error("BODY", tokens)
        if sequential:
            errors.append(sequential)
    example = body.get("example") if isinstance(body.get("example"), dict) else {}
    if fmt == "positional":
        rows = example.get("body_text")
        row = (
            rows[0]
            if isinstance(rows, list) and rows and isinstance(rows[0], list)
            else None
        )
        if row is None or len(row) < len(tokens):
            errors.append(
                "BODY component with variables must include example values "
                f"for every variable ({len(tokens)} required)"
            )
    else:
        names = _named_example_names(example.get("body_text_named_params"))
        if any(token not in names for token in tokens):
            errors.append(
                "BODY component with variables must include example values "
                "for every named parameter"
            )
    return errors


def _validate_header(header: dict, fmt: str) -> list[str]:
    if _header_format(header) != "TEXT":
        # IMAGE / VIDEO / DOCUMENT / LOCATION headers need media handles at
        # both creation and send time, which the channel does not do yet.
        return [MEDIA_HEADERS_UNSUPPORTED]

    errors: list[str] = []
    text = header.get("text") or ""
    if not text:
        errors.append("HEADER text is required for TEXT headers")
        return errors
    if len(text) > _MAX_HEADER_LENGTH:
        errors.append(
            f"HEADER text exceeds {_MAX_HEADER_LENGTH} characters ({len(text)})"
        )
    errors.extend(_placeholder_format_errors("HEADER", text, fmt))
    tokens = _format_placeholders(text, fmt)
    if len(tokens) > 1:
        errors.append("HEADER text supports at most 1 variable")
    if not tokens:
        return errors
    if fmt == "positional" and tokens != ["1"]:
        errors.append("HEADER variable must be {{1}}")
    example = header.get("example") if isinstance(header.get("example"), dict) else {}
    if fmt == "positional":
        header_text = example.get("header_text")
        if not (isinstance(header_text, list) and header_text):
            errors.append(
                "HEADER component with a variable must include example values"
            )
    else:
        names = _named_example_names(example.get("header_text_named_params"))
        if any(token not in names for token in tokens):
            errors.append(
                "HEADER component with a variable must include example values"
            )
    return errors


def _validate_footer(footer: dict) -> list[str]:
    errors: list[str] = []
    text = footer.get("text") or ""
    if len(text) > _MAX_FOOTER_LENGTH:
        errors.append(
            f"FOOTER text exceeds {_MAX_FOOTER_LENGTH} characters ({len(text)})"
        )
    if _scan_placeholders(text):
        errors.append("FOOTER text must not contain variables")
    return errors


def _validate_buttons(component: dict, fmt: str) -> list[str]:
    errors: list[str] = []
    buttons = component.get("buttons")
    if not isinstance(buttons, list) or not buttons:
        errors.append("BUTTONS component must contain a non-empty buttons list")
        return errors
    if len(buttons) > _MAX_BUTTONS:
        errors.append(f"at most {_MAX_BUTTONS} buttons allowed (got {len(buttons)})")

    url_count = 0
    for button in buttons:
        if not isinstance(button, dict):
            errors.append("every button must be an object")
            continue
        btype = str(button.get("type") or "").upper()
        if btype == "QUICK_REPLY":
            btext = button.get("text") or ""
            if len(btext) > _MAX_QUICK_REPLY_LENGTH:
                errors.append(
                    f"quick reply button text exceeds {_MAX_QUICK_REPLY_LENGTH} "
                    f"characters: '{btext}'"
                )
        elif btype == "URL":
            url_count += 1
            url = button.get("url") or ""
            errors.extend(_placeholder_format_errors("URL button", url, fmt))
            tokens = _format_placeholders(url, fmt)
            if tokens:
                if len(tokens) > 1 or not url.rstrip().endswith("}}"):
                    errors.append(
                        "URL button variable must be a single placeholder "
                        "at the end of the URL"
                    )
                elif fmt == "positional" and tokens != ["1"]:
                    errors.append("URL button variable must be {{1}}")
                if not button.get("example"):
                    errors.append(
                        "URL button with a variable must include an example value"
                    )
    if url_count > _MAX_URL_BUTTONS:
        errors.append(
            f"at most {_MAX_URL_BUTTONS} URL buttons allowed (got {url_count})"
        )
    return errors


def extract_template_placeholders(
    components: list[dict], parameter_format: str
) -> list[str]:
    """Distinct placeholder keys a sender must provide values for.

    Positional templates yield the numbers as strings (["1", "2"]),
    named templates the parameter names, covering the TEXT header
    variable, body variables and the URL button suffix variable.
    """
    fmt = _normalize_parameter_format(parameter_format)
    ordered: list[str] = []

    def _add(text: str | None) -> None:
        for token in _format_placeholders(text, fmt):
            if token not in ordered:
                ordered.append(token)

    header = _first_component(components, "HEADER")
    if header is not None and _header_format(header) == "TEXT":
        _add(header.get("text"))
    body = _first_component(components, "BODY")
    if body is not None:
        _add(body.get("text"))
    buttons = _first_component(components, "BUTTONS")
    for button in (buttons.get("buttons") or []) if buttons else []:
        if isinstance(button, dict) and str(button.get("type") or "").upper() == "URL":
            _add(button.get("url"))

    if fmt != "named":
        ordered.sort(key=int)
    return ordered


def _text_parameter(token: str, fmt: str, values: dict[str, str]) -> dict:
    if token not in values:
        raise ValueError(f"missing value for template placeholder '{token}'")
    parameter: dict[str, Any] = {"type": "text", "text": str(values[token])}
    if fmt == "named":
        parameter["parameter_name"] = token
    return parameter


def build_send_components(
    components: list[dict], parameter_format: str, values: dict[str, str]
) -> list[dict]:
    """Build the send-payload ``components`` for a template message.

    ``values`` maps placeholder keys (see extract_template_placeholders)
    to their fill-in strings. Raises ValueError on a missing value or a
    media header. Templates without variables yield an empty list.
    """
    fmt = _normalize_parameter_format(parameter_format)
    values = {str(key): value for key, value in (values or {}).items()}
    send_components: list[dict] = []

    for component in components or []:
        if not isinstance(component, dict):
            continue
        ctype = _component_type(component)
        if ctype == "HEADER":
            if _header_format(component) != "TEXT":
                raise ValueError(MEDIA_HEADERS_UNSUPPORTED)
            tokens = _format_placeholders(component.get("text"), fmt)
            if tokens:
                send_components.append(
                    {
                        "type": "header",
                        "parameters": [
                            _text_parameter(token, fmt, values) for token in tokens
                        ],
                    }
                )
        elif ctype == "BODY":
            tokens = _format_placeholders(component.get("text"), fmt)
            if tokens:
                send_components.append(
                    {
                        "type": "body",
                        "parameters": [
                            _text_parameter(token, fmt, values) for token in tokens
                        ],
                    }
                )
        elif ctype == "BUTTONS":
            for index, button in enumerate(component.get("buttons") or []):
                if not isinstance(button, dict):
                    continue
                if str(button.get("type") or "").upper() != "URL":
                    # Quick-reply payloads are not parameterised at send time.
                    continue
                tokens = _format_placeholders(button.get("url"), fmt)
                if tokens:
                    send_components.append(
                        {
                            "type": "button",
                            "sub_type": "url",
                            "index": str(index),
                            "parameters": [_text_parameter(tokens[0], fmt, values)],
                        }
                    )
    return send_components


# ---------------------------------------------------------------------------
# Graph API operations
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_quality(quality: Any) -> str | None:
    """Meta reports quality as a plain string or {"score": ..., "date": ...}."""
    if isinstance(quality, dict):
        quality = quality.get("score")
    return str(quality).upper() if quality else None


async def submit_template(
    template_row: WhatsAppTemplateModel,
) -> WhatsAppTemplateModel:
    """Submit a mirror row to Meta for review and store the outcome.

    New templates are created at the WABA; rows that already carry a
    meta_template_id (edited REJECTED/PAUSED templates) are updated in
    place, which re-enters Meta review. WhatsAppApiError propagates so
    callers can surface the Graph message.
    """
    # Imported lazily so the pure helpers above stay importable without
    # the Graph client stack.
    from api.services.messaging.whatsapp.client import client_for_configuration

    config = await db_client.get_messaging_configuration(
        template_row.messaging_configuration_id,
        organization_id=template_row.organization_id,
    )
    if config is None:
        raise ValueError("Messaging configuration not found for template")

    client = client_for_configuration(config)
    fields: dict[str, Any] = {"rejection_reason": None, "last_synced_at": _now()}

    if template_row.meta_template_id:
        await client.update_template(
            template_row.meta_template_id,
            components=template_row.components,
            category=template_row.category,
        )
        fields["status"] = "PENDING"
    else:
        result = await client.create_template(
            name=template_row.name,
            language=template_row.language,
            category=template_row.category,
            components=template_row.components,
            parameter_format=template_row.parameter_format,
        )
        fields["status"] = str(result.get("status") or "PENDING").upper()
        if result.get("id"):
            fields["meta_template_id"] = str(result["id"])
        # Meta may recategorise at submission time.
        if result.get("category"):
            fields["category"] = str(result["category"]).upper()

    updated = await db_client.update_whatsapp_template(
        template_row.id,
        organization_id=template_row.organization_id,
        **fields,
    )
    logger.info(
        "Submitted WhatsApp template {} ({}/{}) -> status {}",
        template_row.id,
        template_row.name,
        template_row.language,
        fields["status"],
    )
    return updated or template_row


async def sync_templates(
    config: MessagingConfigurationModel, organization_id: int
) -> dict:
    """Reconcile the local mirror with Meta's template list for one WABA.

    Matched rows (by name, language) get status/category/quality/meta id
    refreshed; templates only known at Meta are imported as new mirror
    rows; local DRAFT rows unknown at Meta are left untouched. Returns
    counts: fetched / created / updated / unchanged.
    """
    from api.services.messaging.whatsapp.client import client_for_configuration

    client = client_for_configuration(config)
    local_rows = await db_client.list_whatsapp_templates(
        organization_id, messaging_configuration_id=config.id
    )
    by_key = {(row.name, row.language): row for row in local_rows}
    counts = {"fetched": 0, "created": 0, "updated": 0, "unchanged": 0}
    now = _now()
    after: str | None = None

    while True:
        page = await client.list_templates(after=after)
        data = page.get("data") or []
        counts["fetched"] += len(data)

        for remote in data:
            name = remote.get("name")
            language = remote.get("language")
            if not name or not language:
                continue
            status = str(remote.get("status") or "").upper() or None
            category = str(remote.get("category") or "").upper() or None
            quality = _normalize_quality(remote.get("quality_score"))
            meta_id = str(remote["id"]) if remote.get("id") else None

            row = by_key.get((name, language))
            if row is None:
                created = await db_client.create_whatsapp_template(
                    organization_id=organization_id,
                    messaging_configuration_id=config.id,
                    name=name,
                    language=language,
                    category=category or "UTILITY",
                    components=remote.get("components") or [],
                    parameter_format=str(
                        remote.get("parameter_format") or "positional"
                    ).lower(),
                )
                await db_client.update_whatsapp_template(
                    created.id,
                    organization_id=organization_id,
                    status=status or "PENDING",
                    meta_template_id=meta_id,
                    quality_score=quality,
                    last_synced_at=now,
                )
                by_key[(name, language)] = created
                counts["created"] += 1
                continue

            fields: dict[str, Any] = {"last_synced_at": now}
            if status and status != row.status:
                fields["status"] = status
            if category and category != row.category:
                fields["category"] = category
            if quality and quality != row.quality_score:
                fields["quality_score"] = quality
            if meta_id and meta_id != row.meta_template_id:
                fields["meta_template_id"] = meta_id
            await db_client.update_whatsapp_template(
                row.id, organization_id=organization_id, **fields
            )
            counts["updated" if len(fields) > 1 else "unchanged"] += 1

        paging = page.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not after:
            break

    logger.info(
        "Synced WhatsApp templates for configuration {}: {}", config.id, counts
    )
    return counts


async def delete_template_at_meta(
    template_row: WhatsAppTemplateModel,
) -> bool | None:
    """Best-effort deletion of a submitted template at Meta.

    Returns None when the row was never submitted (nothing to delete),
    else True/False for whether the Graph call succeeded. Failures are
    logged, never raised — the local mirror row is deleted regardless.
    """
    if not template_row.meta_template_id:
        return None

    from api.services.messaging.whatsapp.client import (
        WhatsAppApiError,
        client_for_configuration,
    )

    config = await db_client.get_messaging_configuration(
        template_row.messaging_configuration_id,
        organization_id=template_row.organization_id,
    )
    if config is None:
        logger.warning(
            "Cannot delete WhatsApp template {} at Meta: configuration {} missing",
            template_row.id,
            template_row.messaging_configuration_id,
        )
        return False
    try:
        client = client_for_configuration(config)
        await client.delete_template(
            name=template_row.name, hsm_id=template_row.meta_template_id
        )
        return True
    except WhatsAppApiError as e:
        logger.warning(
            "Best-effort Meta deletion of WhatsApp template {} ({}) failed: {}",
            template_row.id,
            template_row.name,
            e,
        )
        return False


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
async def handle_template_webhook(kind: str, value: dict) -> None:
    """Apply a Meta template webhook change to the local mirror.

    kind is one of "template_status" / "template_quality" /
    "template_category" (see webhook_parser.parse_webhook_payload); value
    is the raw change value dict. Unknown templates are logged and
    skipped — Meta also notifies about templates we do not mirror.
    """
    value = value or {}
    meta_template_id = value.get("message_template_id")
    name = value.get("message_template_name")
    language = value.get("message_template_language")

    row = await db_client.find_whatsapp_template_for_status_update(
        meta_template_id=str(meta_template_id) if meta_template_id else None,
        name=name,
        language=language,
    )
    if row is None:
        logger.warning(
            "Template webhook {} for unknown template (meta_id={}, name={}, language={})",
            kind,
            meta_template_id,
            name,
            language,
        )
        return

    fields: dict[str, Any] = {}
    if kind == "template_status":
        event = str(value.get("event") or "").upper()
        if not event:
            logger.warning("template_status webhook without event: {}", value)
            return
        fields["status"] = event
        reason = value.get("reason")
        rejection_info = value.get("rejection_info")
        if not reason and isinstance(rejection_info, dict):
            reason = rejection_info.get("reason") or rejection_info.get("description")
        elif not reason and isinstance(rejection_info, str):
            reason = rejection_info
        if reason and str(reason).upper() != "NONE":
            fields["rejection_reason"] = str(reason)
        elif event == "APPROVED":
            fields["rejection_reason"] = None
        if meta_template_id and not row.meta_template_id:
            fields["meta_template_id"] = str(meta_template_id)
    elif kind == "template_quality":
        quality = _normalize_quality(value.get("new_quality_score"))
        if quality:
            fields["quality_score"] = quality
    elif kind == "template_category":
        correct_category = value.get("correct_category")
        if correct_category:
            # Meta flags an upcoming recategorisation before applying it.
            fields["category_pending_change"] = str(correct_category).upper()
        elif value.get("new_category"):
            fields["category"] = str(value["new_category"]).upper()
            fields["category_pending_change"] = None
    else:
        logger.warning("Unhandled template webhook kind: {}", kind)
        return

    if not fields:
        return
    await db_client.update_whatsapp_template(
        row.id, organization_id=row.organization_id, **fields
    )
    logger.info(
        "Applied {} webhook to WhatsApp template {} ({}/{}): {}",
        kind,
        row.id,
        row.name,
        row.language,
        {k: v for k, v in fields.items()},
    )
