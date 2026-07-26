"""Starter catalogue of subscription plans shipped with the SaaS deployment.

``DEFAULT_PLANS`` is a seed, not a source of truth: once a plan exists in the
database the platform admin owns it, and ``ensure_default_plans`` never touches
it again.
"""

from loguru import logger

from api.db import db_client

DEFAULT_CURRENCY = "EUR"

# Feature flags carried by a plan's ``features`` JSONB map.
FEATURE_KEYS = (
    "voice",
    "whatsapp",
    "campaigns",
    "telephony",
    "api_access",
    "knowledge_base",
    "integrations",
)

DEFAULT_PLANS: list[dict] = [
    {
        "code": "trial",
        "name": "Essai",
        "description": "Découverte de la plateforme pendant 14 jours.",
        "price_amount": 0,
        "currency": DEFAULT_CURRENCY,
        "billing_interval": "monthly",
        "trial_days": 14,
        "max_voice_minutes": 60,
        "max_whatsapp_messages": 200,
        "max_workflows": 3,
        "max_campaigns_per_month": 1,
        "max_users": 3,
        "max_concurrent_calls": 2,
        "features": {
            "voice": True,
            "whatsapp": True,
            "campaigns": False,
            "telephony": False,
            "api_access": False,
            "knowledge_base": True,
            "integrations": False,
        },
        "sort_order": 0,
    },
    {
        "code": "starter",
        "name": "Starter",
        "description": "Pour les petites équipes qui démarrent la voix et WhatsApp.",
        "price_amount": 149,
        "currency": DEFAULT_CURRENCY,
        "billing_interval": "monthly",
        "trial_days": 0,
        "max_voice_minutes": 500,
        "max_whatsapp_messages": 2000,
        "max_workflows": 10,
        "max_campaigns_per_month": 5,
        "max_users": 10,
        "max_concurrent_calls": 5,
        "features": {
            "voice": True,
            "whatsapp": True,
            "campaigns": True,
            "telephony": True,
            "api_access": False,
            "knowledge_base": True,
            "integrations": False,
        },
        "sort_order": 1,
    },
    {
        "code": "business",
        "name": "Business",
        "description": "Pour les équipes qui industrialisent leurs campagnes.",
        "price_amount": 449,
        "currency": DEFAULT_CURRENCY,
        "billing_interval": "monthly",
        "trial_days": 0,
        "max_voice_minutes": 2000,
        "max_whatsapp_messages": 10000,
        "max_workflows": 50,
        "max_campaigns_per_month": 25,
        "max_users": 30,
        "max_concurrent_calls": 15,
        "features": {key: True for key in FEATURE_KEYS},
        "sort_order": 2,
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "description": "Volume illimité, accompagnement et intégrations sur mesure.",
        "price_amount": 1490,
        "currency": DEFAULT_CURRENCY,
        "billing_interval": "monthly",
        "trial_days": 0,
        "max_voice_minutes": None,
        "max_whatsapp_messages": None,
        "max_workflows": None,
        "max_campaigns_per_month": None,
        "max_users": None,
        "max_concurrent_calls": None,
        "features": {key: True for key in FEATURE_KEYS},
        "sort_order": 3,
    },
]


async def ensure_default_plans() -> list[str]:
    """Create the default plans that are missing, and only those.

    Idempotent: an existing code is left untouched, including its pricing,
    limits and active flag, so a plan the admin has edited (or deactivated) is
    never resurrected. Returns the codes actually created.
    """
    existing_plans = await db_client.list_subscription_plans(include_inactive=True)
    existing_codes = {plan.code for plan in existing_plans}

    created: list[str] = []
    for plan in DEFAULT_PLANS:
        if plan["code"] in existing_codes:
            continue
        fields = {**plan, "features": dict(plan["features"])}
        await db_client.create_subscription_plan(**fields)
        created.append(plan["code"])

    if created:
        logger.info(f"Seeded default subscription plans: {', '.join(created)}")
    return created
