"""Bootstrap the platform admin of a SaaS install.

Promotes a user to superuser — creating the account first when it does not
exist yet — and seeds the default subscription plans. Idempotent: re-running it
on an already bootstrapped install only reports the current state.

Run from the repo root with the api environment available:

    python -m scripts.bootstrap_platform_admin --email admin@example.com --password 'a-strong-password'

Inside the SaaS docker stack (the file is mounted into the api container by
docker-compose.saas.yaml):

    docker compose -f docker-compose.saas.yaml exec api \
        python -m scripts.bootstrap_platform_admin --email admin@example.com --password 'a-strong-password'
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

from sqlalchemy import update  # noqa: E402

from api.db import db_client  # noqa: E402
from api.db.models import UserModel  # noqa: E402
from api.enums import OrganizationConfigurationKey  # noqa: E402
from api.services.auth.depends import (  # noqa: E402
    create_user_configuration_with_mps_key,
)
from api.services.configuration.ai_model_configuration import (  # noqa: E402
    convert_legacy_ai_model_configuration_to_v2,
)
from api.services.subscription.plans import ensure_default_plans  # noqa: E402
from api.utils.auth import hash_password  # noqa: E402


async def _create_user(email: str, password: str) -> UserModel:
    """Create a local-auth user with its own organization, mirroring auth signup."""
    user = await db_client.create_user_with_email(
        email=email, password_hash=hash_password(password)
    )

    org_provider_id = f"org_{user.provider_id}"
    organization, _ = await db_client.get_or_create_organization_by_provider_id(
        org_provider_id=org_provider_id, user_id=user.id
    )
    await db_client.add_user_to_organization(user.id, organization.id)
    await db_client.update_user_selected_organization(user.id, organization.id)

    # Same best-effort default model configuration signup creates; it needs the
    # managed model services API, so a failure here must not fail the bootstrap.
    try:
        mps_config = await create_user_configuration_with_mps_key(
            user.id, organization.id, user.provider_id
        )
        if mps_config:
            await db_client.update_user_configuration(user.id, mps_config)
            model_config_v2 = convert_legacy_ai_model_configuration_to_v2(mps_config)
            await db_client.upsert_configuration(
                organization.id,
                OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value,
                model_config_v2.model_dump(mode="json", exclude_none=True),
            )
    except Exception as exc:
        print(f"  warning: default model configuration not created ({exc})")

    print(f"  created user id={user.id} with organization id={organization.id}")
    return user


async def _promote_to_superuser(user_id: int) -> None:
    """Set users.is_superuser; there is no db_client helper for this flag."""
    async with db_client.async_session() as session:
        await session.execute(
            update(UserModel).where(UserModel.id == user_id).values(is_superuser=True)
        )
        await session.commit()


async def bootstrap(email: str, password: str | None) -> int:
    if password is not None and not password.strip():
        print("--password must not be empty.", file=sys.stderr)
        return 2

    user = await db_client.get_user_by_email(email)
    if user is None:
        if not password:
            print(
                f"No user found for {email}. Pass --password to create the account.",
                file=sys.stderr,
            )
            return 1
        print(f"Creating user {email}")
        user = await _create_user(email, password)
    else:
        print(f"User {email} already exists (id={user.id}); password left unchanged")

    if user.is_superuser:
        print("  already a platform admin")
    else:
        await _promote_to_superuser(user.id)
        print("  promoted to platform admin (is_superuser=true)")

    await ensure_default_plans()
    plans = await db_client.list_subscription_plans(include_inactive=True)
    print(f"Default subscription plans ensured ({len(plans)} plan(s) in catalogue)")
    for plan in plans:
        print(f"  {plan.code}: {plan.name} — {plan.price_amount} {plan.currency}")

    print("Done. Sign in with this account to reach the platform admin dashboard.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote a user to platform admin and seed the default subscription plans."
    )
    parser.add_argument("--email", required=True, help="Email of the platform admin")
    parser.add_argument(
        "--password",
        default=None,
        help="Password used only when the account has to be created",
    )
    args = parser.parse_args()

    raise SystemExit(asyncio.run(bootstrap(args.email.strip(), args.password)))


if __name__ == "__main__":
    main()
