"""Canonical Google OAuth scope registry.

Three different questions get confused constantly; this module keeps them apart:

1. **What does a tool family need?** :data:`FAMILY_SCOPES` — a fixed, server-side
   answer so the browser never has to know which scope a Calendar tool implies.
2. **What does *this deployment* request?** :func:`requested_scopes` — read from
   ``GOOGLE_OAUTH_SCOPES``, which defaults to the two non-sensitive scopes.
3. **What did *this organization* actually grant?**
   :func:`granted_scopes_for_organization` — whatever Google echoed back on the
   consent that org went through, which can be narrower than what was asked for
   (the user can decline individual scopes) and older than the deployment's
   current list.

Why (1) and (2) are deliberately different
------------------------------------------
Volira requests ``drive.file`` + ``spreadsheets`` today because both are
NON-SENSITIVE: no Google verification, no recurring paid security assessment.
The families below name the scopes a feature *would* need, and most of them are
not free to ask for:

- ``drive.readonly``, ``gmail.readonly`` — RESTRICTED. Verification *plus* a
  yearly third-party assessment (CASA), paid, recurring, and applied to the
  whole OAuth client — including every organization already connected.
- ``calendar.readonly``, ``calendar.events``, ``documents``, ``gmail.send`` —
  SENSITIVE. Verification (~10 days), no annual assessment.

So a family appearing here does **not** mean this deployment can use it. The
discovery layer must compare the family's scopes against what was *granted*
(:func:`missing_family_scopes`) and tell the operator which scope is missing,
rather than showing an unexplained empty list. Widening ``GOOGLE_OAUTH_SCOPES``
is an operator decision with a compliance bill attached, never a code change.

Broad scopes cover narrow ones
------------------------------
A grant of ``spreadsheets`` satisfies a requirement for
``spreadsheets.readonly``; ``drive`` satisfies ``drive.readonly`` *and*
``drive.file``. Google does not return the implied scopes, so a naive set
membership test reports a missing scope for an account that has strictly more
access than required. :data:`SCOPE_IMPLICATIONS` states every implication we
rely on explicitly — it is a whitelist, not a prefix rule, because
``drive.readonly`` does **not** cover ``drive.file`` (drive.file grants *write*
on the files the user picked, which a read-only grant cannot).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from api.constants import GOOGLE_OAUTH_SCOPES

from .oauth import get_connection

_BASE = "https://www.googleapis.com/auth"

# Sheets
SCOPE_SPREADSHEETS = f"{_BASE}/spreadsheets"
SCOPE_SPREADSHEETS_READONLY = f"{_BASE}/spreadsheets.readonly"

# Drive
SCOPE_DRIVE = f"{_BASE}/drive"
SCOPE_DRIVE_READONLY = f"{_BASE}/drive.readonly"
SCOPE_DRIVE_FILE = f"{_BASE}/drive.file"
SCOPE_DRIVE_METADATA_READONLY = f"{_BASE}/drive.metadata.readonly"

# Calendar
SCOPE_CALENDAR = f"{_BASE}/calendar"
SCOPE_CALENDAR_READONLY = f"{_BASE}/calendar.readonly"
SCOPE_CALENDAR_EVENTS = f"{_BASE}/calendar.events"
SCOPE_CALENDAR_EVENTS_READONLY = f"{_BASE}/calendar.events.readonly"

# Gmail
SCOPE_GMAIL_READONLY = f"{_BASE}/gmail.readonly"
SCOPE_GMAIL_SEND = f"{_BASE}/gmail.send"
SCOPE_GMAIL_COMPOSE = f"{_BASE}/gmail.compose"
SCOPE_GMAIL_MODIFY = f"{_BASE}/gmail.modify"
# The legacy "everything in the mailbox" scope. Trailing slash is part of it.
SCOPE_GMAIL_FULL = "https://mail.google.com/"

# Docs
SCOPE_DOCUMENTS = f"{_BASE}/documents"
SCOPE_DOCUMENTS_READONLY = f"{_BASE}/documents.readonly"

# Identity
SCOPE_OPENID = "openid"
SCOPE_USERINFO_EMAIL = f"{_BASE}/userinfo.email"
SCOPE_USERINFO_PROFILE = f"{_BASE}/userinfo.profile"

# Google accepts (and echoes) the short forms of the two identity scopes.
# Normalizing them means a connection that came back with "email" is not
# reported as missing userinfo.email.
SCOPE_ALIASES: Mapping[str, str] = {
    "email": SCOPE_USERINFO_EMAIL,
    "profile": SCOPE_USERINFO_PROFILE,
}


class ScopeSensitivity(str, Enum):
    """Google's classification, which decides the review a deployment owes.

    - ``NON_SENSITIVE``: nothing to do.
    - ``SENSITIVE``: app verification before external users can consent.
    - ``RESTRICTED``: verification *and* a paid annual security assessment.
    - ``UNKNOWN``: a scope an operator configured that this registry does not
      classify — surfaced rather than silently called safe.
    """

    NON_SENSITIVE = "non_sensitive"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


# Source of truth is Google's published scope classification; this table is the
# copy the UI warns from. Keep it in sync when adding a scope to a family.
SCOPE_SENSITIVITY: Mapping[str, ScopeSensitivity] = {
    SCOPE_SPREADSHEETS: ScopeSensitivity.NON_SENSITIVE,
    SCOPE_SPREADSHEETS_READONLY: ScopeSensitivity.NON_SENSITIVE,
    SCOPE_DRIVE_FILE: ScopeSensitivity.NON_SENSITIVE,
    SCOPE_OPENID: ScopeSensitivity.NON_SENSITIVE,
    SCOPE_USERINFO_EMAIL: ScopeSensitivity.NON_SENSITIVE,
    SCOPE_USERINFO_PROFILE: ScopeSensitivity.NON_SENSITIVE,
    SCOPE_CALENDAR: ScopeSensitivity.SENSITIVE,
    SCOPE_CALENDAR_READONLY: ScopeSensitivity.SENSITIVE,
    SCOPE_CALENDAR_EVENTS: ScopeSensitivity.SENSITIVE,
    SCOPE_CALENDAR_EVENTS_READONLY: ScopeSensitivity.SENSITIVE,
    SCOPE_DOCUMENTS: ScopeSensitivity.SENSITIVE,
    SCOPE_DOCUMENTS_READONLY: ScopeSensitivity.SENSITIVE,
    # gmail.send is Google's one non-restricted Gmail scope: it can only send,
    # never read, so it costs verification but no annual assessment. Every Gmail
    # scope that can *read* a mailbox is restricted.
    SCOPE_GMAIL_SEND: ScopeSensitivity.SENSITIVE,
    SCOPE_DRIVE: ScopeSensitivity.RESTRICTED,
    SCOPE_DRIVE_READONLY: ScopeSensitivity.RESTRICTED,
    SCOPE_DRIVE_METADATA_READONLY: ScopeSensitivity.RESTRICTED,
    SCOPE_GMAIL_READONLY: ScopeSensitivity.RESTRICTED,
    SCOPE_GMAIL_COMPOSE: ScopeSensitivity.RESTRICTED,
    SCOPE_GMAIL_MODIFY: ScopeSensitivity.RESTRICTED,
    SCOPE_GMAIL_FULL: ScopeSensitivity.RESTRICTED,
}

# Ordered worst-first so a set of scopes can be summarised by its heaviest
# obligation (see :func:`highest_sensitivity`).
_SENSITIVITY_RANK: Mapping[ScopeSensitivity, int] = {
    ScopeSensitivity.RESTRICTED: 3,
    ScopeSensitivity.SENSITIVE: 2,
    ScopeSensitivity.UNKNOWN: 1,
    ScopeSensitivity.NON_SENSITIVE: 0,
}

# "Granting X also grants Y". Written out one implication at a time on purpose:
# these are capability statements, not string prefixes.
#
# Deliberately absent:
#   drive.readonly -> drive.file   — drive.file allows *writing* the picked file
#   drive.file     -> drive.readonly — drive.file sees only what the user picked
#   gmail.send     -> anything     — send cannot read
SCOPE_IMPLICATIONS: Mapping[str, tuple[str, ...]] = {
    SCOPE_SPREADSHEETS: (SCOPE_SPREADSHEETS_READONLY,),
    SCOPE_DRIVE: (
        SCOPE_DRIVE_READONLY,
        SCOPE_DRIVE_FILE,
        SCOPE_DRIVE_METADATA_READONLY,
    ),
    SCOPE_DRIVE_READONLY: (SCOPE_DRIVE_METADATA_READONLY,),
    SCOPE_CALENDAR: (
        SCOPE_CALENDAR_READONLY,
        SCOPE_CALENDAR_EVENTS,
        SCOPE_CALENDAR_EVENTS_READONLY,
    ),
    SCOPE_CALENDAR_READONLY: (SCOPE_CALENDAR_EVENTS_READONLY,),
    SCOPE_CALENDAR_EVENTS: (SCOPE_CALENDAR_EVENTS_READONLY,),
    SCOPE_DOCUMENTS: (SCOPE_DOCUMENTS_READONLY,),
    SCOPE_GMAIL_FULL: (
        SCOPE_GMAIL_READONLY,
        SCOPE_GMAIL_MODIFY,
        SCOPE_GMAIL_COMPOSE,
        SCOPE_GMAIL_SEND,
    ),
    SCOPE_GMAIL_MODIFY: (
        SCOPE_GMAIL_READONLY,
        SCOPE_GMAIL_COMPOSE,
        SCOPE_GMAIL_SEND,
    ),
    SCOPE_GMAIL_COMPOSE: (SCOPE_GMAIL_SEND,),
}


FAMILY_SHEETS = "sheets"
FAMILY_CALENDAR = "calendar"
FAMILY_DRIVE = "drive"
FAMILY_GMAIL = "gmail"
FAMILY_DOCS = "docs"
FAMILY_ALL = "all"

# What each tool family needs to work. Sheets stays on drive.file (the picked
# file only): listing a whole Drive is what would drag the deployment into
# restricted-scope territory, and the Picker already gives the owner a way to
# choose a spreadsheet without it.
FAMILY_SCOPES: Mapping[str, tuple[str, ...]] = {
    FAMILY_SHEETS: (SCOPE_SPREADSHEETS, SCOPE_DRIVE_FILE),
    FAMILY_CALENDAR: (SCOPE_CALENDAR_READONLY, SCOPE_CALENDAR_EVENTS),
    FAMILY_DRIVE: (SCOPE_DRIVE_READONLY,),
    FAMILY_GMAIL: (SCOPE_GMAIL_READONLY, SCOPE_GMAIL_SEND),
    FAMILY_DOCS: (SCOPE_DOCUMENTS, SCOPE_DRIVE_READONLY),
}

FAMILIES: tuple[str, ...] = tuple(FAMILY_SCOPES)


@dataclass(frozen=True)
class ScopeDescriptor:
    """One scope, with everything the UI needs to explain it.

    ``requested`` is deployment-level (is it in ``GOOGLE_OAUTH_SCOPES``?),
    ``granted`` is organization-level (did this org's consent cover it?). Both
    are computed through :func:`has_scope`, so a broader scope counts.
    """

    scope: str
    sensitivity: ScopeSensitivity
    requested: bool
    granted: bool


def normalize_scope(scope: str) -> str:
    """Trim a scope and resolve Google's short identity aliases."""
    cleaned = str(scope or "").strip()
    return SCOPE_ALIASES.get(cleaned, cleaned)


def normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """Normalize a scope list, dropping blanks and duplicates, keeping order."""
    normalized = dict.fromkeys(
        normalized_scope
        for normalized_scope in (normalize_scope(scope) for scope in scopes or ())
        if normalized_scope
    )
    return tuple(normalized)


def expand_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Close a scope set over :data:`SCOPE_IMPLICATIONS`.

    The closure is transitive (``gmail.modify`` reaches ``gmail.send`` through
    ``gmail.compose``), so an implication chain never has to be flattened by
    hand in the table above.
    """
    resolved: set[str] = set()
    pending = list(normalize_scopes(scopes))
    while pending:
        scope = pending.pop()
        if scope in resolved:
            continue
        resolved.add(scope)
        pending.extend(SCOPE_IMPLICATIONS.get(scope, ()))
    return frozenset(resolved)


def has_scope(granted: Iterable[str], required: str) -> bool:
    """True when ``granted`` covers ``required``, directly or by implication."""
    needle = normalize_scope(required)
    if not needle:
        return False
    return needle in expand_scopes(granted)


def missing_scopes(granted: Iterable[str], required: Iterable[str]) -> tuple[str, ...]:
    """The subset of ``required`` that ``granted`` does not cover, in order."""
    covered = expand_scopes(granted)
    return tuple(
        scope for scope in normalize_scopes(required) if scope not in covered
    )


def family_scopes(family: str) -> tuple[str, ...]:
    """Scopes one tool family needs. ``"all"`` returns the union of every family.

    Raises ``ValueError`` for an unknown family rather than returning an empty
    tuple, which a caller would read as "nothing required" and let through.
    """
    key = str(family or "").strip().lower()
    if key == FAMILY_ALL:
        union: dict[str, None] = {}
        for scopes in FAMILY_SCOPES.values():
            union.update(dict.fromkeys(scopes))
        return tuple(union)
    if key not in FAMILY_SCOPES:
        raise ValueError(f"Unknown Google tool family: {family!r}")
    return FAMILY_SCOPES[key]


def scope_catalog() -> dict[str, list[str]]:
    """The whole registry, shaped for the required-scopes endpoint.

    ``{"sheets": [...], ..., "all": [...]}`` — this is what lets the browser stop
    guessing which scopes a tool needs.
    """
    catalog = {family: list(scopes) for family, scopes in FAMILY_SCOPES.items()}
    catalog[FAMILY_ALL] = list(family_scopes(FAMILY_ALL))
    return catalog


def requested_scopes() -> tuple[str, ...]:
    """Scopes this deployment asks Google for, from ``GOOGLE_OAUTH_SCOPES``."""
    return normalize_scopes(GOOGLE_OAUTH_SCOPES)


def is_requested(scope: str) -> bool:
    """True when this deployment's consent screen covers ``scope``."""
    return has_scope(requested_scopes(), scope)


def missing_requested_scopes(required: Iterable[str]) -> tuple[str, ...]:
    """Which of ``required`` this deployment never even asks for.

    Distinct from a per-organization gap: no amount of re-consenting fixes it,
    because the authorization URL does not carry the scope. The operator has to
    widen ``GOOGLE_OAUTH_SCOPES`` (and own the resulting Google review).
    """
    return missing_scopes(requested_scopes(), required)


def missing_family_scopes(granted: Iterable[str], family: str) -> tuple[str, ...]:
    """Scopes ``family`` needs that this grant does not cover."""
    return missing_scopes(granted, family_scopes(family))


def supports_family(granted: Iterable[str], family: str) -> bool:
    """True when a grant covers everything ``family`` needs."""
    return not missing_family_scopes(granted, family)


def sensitivity_of(scope: str) -> ScopeSensitivity:
    """Google's classification for one scope, ``UNKNOWN`` when unclassified."""
    return SCOPE_SENSITIVITY.get(normalize_scope(scope), ScopeSensitivity.UNKNOWN)


def highest_sensitivity(scopes: Iterable[str]) -> ScopeSensitivity:
    """The heaviest obligation a scope set carries.

    What an operator actually needs to know before widening
    ``GOOGLE_OAUTH_SCOPES``: one restricted scope makes the whole client
    restricted.
    """
    worst = ScopeSensitivity.NON_SENSITIVE
    for scope in normalize_scopes(scopes):
        current = sensitivity_of(scope)
        if _SENSITIVITY_RANK[current] > _SENSITIVITY_RANK[worst]:
            worst = current
    return worst


def deployment_sensitivity() -> ScopeSensitivity:
    """The classification this deployment's own scope list falls under."""
    return highest_sensitivity(requested_scopes())


def scope_descriptors(
    granted: Iterable[str] = (), *, scopes: Iterable[str] | None = None
) -> tuple[ScopeDescriptor, ...]:
    """Describe scopes for the UI: what they cost, what is asked, what is held.

    Defaults to every scope any family needs plus whatever this deployment
    requests — so a scope an operator added by hand still shows up (classified
    ``UNKNOWN`` if this registry does not know it) instead of vanishing.
    """
    if scopes is None:
        catalogued = dict.fromkeys(family_scopes(FAMILY_ALL))
        catalogued.update(dict.fromkeys(requested_scopes()))
        selected: Iterable[str] = catalogued
    else:
        selected = scopes

    granted_scopes_set = expand_scopes(granted)
    requested_set = expand_scopes(requested_scopes())
    return tuple(
        ScopeDescriptor(
            scope=scope,
            sensitivity=sensitivity_of(scope),
            requested=scope in requested_set,
            granted=scope in granted_scopes_set,
        )
        for scope in normalize_scopes(selected)
    )


async def granted_scopes_for_organization(organization_id: int) -> tuple[str, ...]:
    """Scopes Google actually granted this organization.

    Empty when the organization has no connection *or* when its grant was
    revoked: a revoked connection grants nothing, and reporting its historical
    scopes would make every discovery section look available right up until the
    call to Google fails.
    """
    connection = await get_connection(organization_id)
    if connection is None or connection.is_revoked:
        return ()
    return normalize_scopes(connection.scopes)
