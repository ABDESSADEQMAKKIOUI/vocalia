"""DB operations for the messaging channel (WhatsApp Cloud API).

Covers org channel configurations, business numbers (addresses),
conversation-window <-> workflow-run mapping, webhook idempotency,
the local mirror of Meta message templates, and the suppression list.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select
from sqlalchemy.orm import joinedload

from api.db.base_client import BaseDBClient
from api.db.models import (
    MessagingAddressModel,
    MessagingConfigurationModel,
    MessagingSuppressionModel,
    WhatsAppConversationModel,
    WhatsAppProcessedMessageModel,
    WhatsAppTemplateModel,
)


class MessagingClient(BaseDBClient):
    # ------------------------------------------------------------------
    # Configurations
    # ------------------------------------------------------------------
    async def create_messaging_configuration(
        self,
        *,
        organization_id: int,
        name: str,
        provider: str,
        credentials: dict,
    ) -> MessagingConfigurationModel:
        async with self.async_session() as session:
            config = MessagingConfigurationModel(
                organization_id=organization_id,
                name=name,
                provider=provider,
                credentials=credentials,
            )
            session.add(config)
            await session.commit()
            await session.refresh(config)
        return config

    async def list_messaging_configurations(
        self, organization_id: int, *, active_only: bool = False
    ) -> list[MessagingConfigurationModel]:
        async with self.async_session() as session:
            query = (
                select(MessagingConfigurationModel)
                .options(joinedload(MessagingConfigurationModel.addresses))
                .where(MessagingConfigurationModel.organization_id == organization_id)
                .order_by(MessagingConfigurationModel.id)
            )
            if active_only:
                query = query.where(MessagingConfigurationModel.is_active.is_(True))
            result = await session.execute(query)
            return list(result.scalars().unique().all())

    async def get_messaging_configuration(
        self, configuration_id: int, *, organization_id: int | None = None
    ) -> MessagingConfigurationModel | None:
        async with self.async_session() as session:
            query = (
                select(MessagingConfigurationModel)
                .options(joinedload(MessagingConfigurationModel.addresses))
                .where(MessagingConfigurationModel.id == configuration_id)
            )
            if organization_id is not None:
                query = query.where(
                    MessagingConfigurationModel.organization_id == organization_id
                )
            result = await session.execute(query)
            return result.scalars().unique().first()

    async def update_messaging_configuration(
        self,
        configuration_id: int,
        *,
        organization_id: int,
        name: str | None = None,
        credentials: dict | None = None,
        is_active: bool | None = None,
    ) -> MessagingConfigurationModel | None:
        async with self.async_session() as session:
            result = await session.execute(
                select(MessagingConfigurationModel)
                .where(MessagingConfigurationModel.id == configuration_id)
                .where(
                    MessagingConfigurationModel.organization_id == organization_id
                )
                .with_for_update()
            )
            config = result.scalars().first()
            if not config:
                return None
            if name is not None:
                config.name = name
            if credentials is not None:
                config.credentials = credentials
            if is_active is not None:
                config.is_active = is_active
            await session.commit()
            await session.refresh(config)
        return config

    async def delete_messaging_configuration(
        self, configuration_id: int, *, organization_id: int
    ) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                delete(MessagingConfigurationModel)
                .where(MessagingConfigurationModel.id == configuration_id)
                .where(
                    MessagingConfigurationModel.organization_id == organization_id
                )
            )
            await session.commit()
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # Addresses (business phone numbers)
    # ------------------------------------------------------------------
    async def create_messaging_address(
        self,
        *,
        organization_id: int,
        messaging_configuration_id: int,
        address: str,
        external_id: str,
        account_id: str | None = None,
        inbound_workflow_id: int | None = None,
        extra_metadata: dict | None = None,
    ) -> MessagingAddressModel:
        async with self.async_session() as session:
            row = MessagingAddressModel(
                organization_id=organization_id,
                messaging_configuration_id=messaging_configuration_id,
                address=address,
                external_id=external_id,
                account_id=account_id,
                inbound_workflow_id=inbound_workflow_id,
                extra_metadata=extra_metadata or {},
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def get_messaging_address_by_external_id(
        self, external_id: str
    ) -> MessagingAddressModel | None:
        """Inbound routing: Meta phone_number_id -> address (+config eager-loaded)."""
        async with self.async_session() as session:
            result = await session.execute(
                select(MessagingAddressModel)
                .options(joinedload(MessagingAddressModel.configuration))
                .where(MessagingAddressModel.external_id == external_id)
                .where(MessagingAddressModel.is_active.is_(True))
            )
            return result.scalars().first()

    async def update_messaging_address(
        self,
        address_id: int,
        *,
        organization_id: int,
        inbound_workflow_id: int | None = ...,
        is_active: bool | None = None,
        extra_metadata: dict | None = None,
    ) -> MessagingAddressModel | None:
        async with self.async_session() as session:
            result = await session.execute(
                select(MessagingAddressModel)
                .where(MessagingAddressModel.id == address_id)
                .where(MessagingAddressModel.organization_id == organization_id)
                .with_for_update()
            )
            row = result.scalars().first()
            if not row:
                return None
            if inbound_workflow_id is not ...:
                row.inbound_workflow_id = inbound_workflow_id
            if is_active is not None:
                row.is_active = is_active
            if extra_metadata is not None:
                row.extra_metadata = extra_metadata
            await session.commit()
            await session.refresh(row)
        return row

    async def delete_messaging_address(
        self, address_id: int, *, organization_id: int
    ) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                delete(MessagingAddressModel)
                .where(MessagingAddressModel.id == address_id)
                .where(MessagingAddressModel.organization_id == organization_id)
            )
            await session.commit()
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------
    async def get_open_whatsapp_conversation(
        self, *, phone_number_id: str, wa_id: str
    ) -> WhatsAppConversationModel | None:
        async with self.async_session() as session:
            result = await session.execute(
                select(WhatsAppConversationModel)
                .where(WhatsAppConversationModel.phone_number_id == phone_number_id)
                .where(WhatsAppConversationModel.wa_id == wa_id)
                .where(WhatsAppConversationModel.state == "open")
            )
            return result.scalars().first()

    async def get_whatsapp_conversation(
        self, conversation_id: int, *, organization_id: int | None = None
    ) -> WhatsAppConversationModel | None:
        async with self.async_session() as session:
            query = (
                select(WhatsAppConversationModel)
                .options(joinedload(WhatsAppConversationModel.messaging_address))
                .where(WhatsAppConversationModel.id == conversation_id)
            )
            if organization_id is not None:
                query = query.where(
                    WhatsAppConversationModel.organization_id == organization_id
                )
            result = await session.execute(query)
            return result.scalars().first()

    async def list_whatsapp_conversations(
        self,
        organization_id: int,
        *,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WhatsAppConversationModel]:
        """Inbox listing, most recently active first (address eager-loaded)."""
        async with self.async_session() as session:
            query = (
                select(WhatsAppConversationModel)
                .options(joinedload(WhatsAppConversationModel.messaging_address))
                .where(WhatsAppConversationModel.organization_id == organization_id)
                .order_by(WhatsAppConversationModel.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            if state and state != "all":
                query = query.where(WhatsAppConversationModel.state == state)
            result = await session.execute(query)
            return list(result.scalars().unique().all())

    async def set_whatsapp_conversation_agent_paused(
        self, conversation_id: int, *, organization_id: int, paused: bool
    ) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                update(WhatsAppConversationModel)
                .where(WhatsAppConversationModel.id == conversation_id)
                .where(
                    WhatsAppConversationModel.organization_id == organization_id
                )
                .values(agent_paused=paused, updated_at=datetime.now(UTC))
            )
            await session.commit()
            return result.rowcount > 0

    async def get_whatsapp_conversation_by_run(
        self, workflow_run_id: int
    ) -> WhatsAppConversationModel | None:
        async with self.async_session() as session:
            result = await session.execute(
                select(WhatsAppConversationModel).where(
                    WhatsAppConversationModel.workflow_run_id == workflow_run_id
                )
            )
            return result.scalars().first()

    async def create_whatsapp_conversation(
        self,
        *,
        organization_id: int,
        messaging_address_id: int,
        phone_number_id: str,
        wa_id: str,
        workflow_run_id: int,
        profile_name: str | None = None,
        campaign_id: int | None = None,
        service_window_expires_at: datetime | None = None,
        last_inbound_at: datetime | None = None,
    ) -> WhatsAppConversationModel:
        """Create an open conversation. Raises IntegrityError if one is
        already open for (phone_number_id, wa_id) — callers should retry
        with get_open_whatsapp_conversation on conflict."""
        async with self.async_session() as session:
            row = WhatsAppConversationModel(
                organization_id=organization_id,
                messaging_address_id=messaging_address_id,
                phone_number_id=phone_number_id,
                wa_id=wa_id,
                workflow_run_id=workflow_run_id,
                profile_name=profile_name,
                campaign_id=campaign_id,
                state="open",
                service_window_expires_at=service_window_expires_at,
                last_inbound_at=last_inbound_at,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
        return row

    async def touch_whatsapp_conversation(
        self,
        conversation_id: int,
        *,
        profile_name: str | None = None,
        last_inbound_at: datetime | None = None,
        last_outbound_at: datetime | None = None,
        service_window_expires_at: datetime | None = None,
    ) -> None:
        async with self.async_session() as session:
            values: dict = {"updated_at": datetime.now(UTC)}
            if profile_name is not None:
                values["profile_name"] = profile_name
            if last_inbound_at is not None:
                values["last_inbound_at"] = last_inbound_at
            if last_outbound_at is not None:
                values["last_outbound_at"] = last_outbound_at
            if service_window_expires_at is not None:
                values["service_window_expires_at"] = service_window_expires_at
            await session.execute(
                update(WhatsAppConversationModel)
                .where(WhatsAppConversationModel.id == conversation_id)
                .values(**values)
            )
            await session.commit()

    async def close_whatsapp_conversation(self, conversation_id: int) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                update(WhatsAppConversationModel)
                .where(WhatsAppConversationModel.id == conversation_id)
                .where(WhatsAppConversationModel.state == "open")
                .values(state="closed", updated_at=datetime.now(UTC))
            )
            await session.commit()
            return result.rowcount > 0

    async def list_stale_whatsapp_conversations(
        self, *, inactive_since: datetime, limit: int = 100
    ) -> list[WhatsAppConversationModel]:
        """Open conversations with no activity since the cutoff — sweeper input."""
        async with self.async_session() as session:
            result = await session.execute(
                select(WhatsAppConversationModel)
                .where(WhatsAppConversationModel.state == "open")
                .where(
                    or_(
                        WhatsAppConversationModel.last_inbound_at.is_(None),
                        WhatsAppConversationModel.last_inbound_at < inactive_since,
                    )
                )
                .where(WhatsAppConversationModel.updated_at < inactive_since)
                .limit(limit)
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Webhook idempotency
    # ------------------------------------------------------------------
    async def try_record_wamid(
        self, wamid: str, *, conversation_id: int | None = None
    ) -> bool:
        """Record a processed inbound wamid. Returns False if already seen."""
        async with self.async_session() as session:
            session.add(
                WhatsAppProcessedMessageModel(
                    wamid=wamid, conversation_id=conversation_id
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def purge_processed_wamids(self, *, older_than: datetime) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                delete(WhatsAppProcessedMessageModel).where(
                    WhatsAppProcessedMessageModel.received_at < older_than
                )
            )
            await session.commit()
            return result.rowcount

    # ------------------------------------------------------------------
    # Templates (local mirror)
    # ------------------------------------------------------------------
    async def create_whatsapp_template(
        self,
        *,
        organization_id: int,
        messaging_configuration_id: int,
        name: str,
        language: str,
        category: str,
        components: list,
        parameter_format: str = "positional",
        created_by: int | None = None,
    ) -> WhatsAppTemplateModel:
        async with self.async_session() as session:
            row = WhatsAppTemplateModel(
                organization_id=organization_id,
                messaging_configuration_id=messaging_configuration_id,
                name=name,
                language=language,
                category=category,
                components=components,
                parameter_format=parameter_format,
                created_by=created_by,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def list_whatsapp_templates(
        self,
        organization_id: int,
        *,
        messaging_configuration_id: int | None = None,
        status: str | None = None,
    ) -> list[WhatsAppTemplateModel]:
        async with self.async_session() as session:
            query = (
                select(WhatsAppTemplateModel)
                .where(WhatsAppTemplateModel.organization_id == organization_id)
                .order_by(WhatsAppTemplateModel.updated_at.desc())
            )
            if messaging_configuration_id is not None:
                query = query.where(
                    WhatsAppTemplateModel.messaging_configuration_id
                    == messaging_configuration_id
                )
            if status is not None:
                query = query.where(WhatsAppTemplateModel.status == status)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def get_whatsapp_template(
        self, template_id: int, *, organization_id: int | None = None
    ) -> WhatsAppTemplateModel | None:
        async with self.async_session() as session:
            query = select(WhatsAppTemplateModel).where(
                WhatsAppTemplateModel.id == template_id
            )
            if organization_id is not None:
                query = query.where(
                    WhatsAppTemplateModel.organization_id == organization_id
                )
            result = await session.execute(query)
            return result.scalars().first()

    async def update_whatsapp_template(
        self,
        template_id: int,
        *,
        organization_id: int | None = None,
        **fields,
    ) -> WhatsAppTemplateModel | None:
        """Update mirror fields (components, status, meta_template_id,
        quality_score, rejection_reason, category, category_pending_change,
        last_synced_at, parameter_format)."""
        allowed = {
            "name",
            "language",
            "category",
            "parameter_format",
            "components",
            "status",
            "meta_template_id",
            "quality_score",
            "rejection_reason",
            "category_pending_change",
            "last_synced_at",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unsupported template fields: {bad}")
        async with self.async_session() as session:
            query = (
                select(WhatsAppTemplateModel)
                .where(WhatsAppTemplateModel.id == template_id)
                .with_for_update()
            )
            if organization_id is not None:
                query = query.where(
                    WhatsAppTemplateModel.organization_id == organization_id
                )
            result = await session.execute(query)
            row = result.scalars().first()
            if not row:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
        return row

    async def find_whatsapp_template_for_status_update(
        self,
        *,
        meta_template_id: str | None,
        name: str | None,
        language: str | None,
        waba_configuration_ids: list[int] | None = None,
    ) -> WhatsAppTemplateModel | None:
        """Locate the mirror row a template webhook refers to — by Meta id
        first, then by (name, language) within the given configurations."""
        async with self.async_session() as session:
            if meta_template_id:
                result = await session.execute(
                    select(WhatsAppTemplateModel).where(
                        WhatsAppTemplateModel.meta_template_id == str(meta_template_id)
                    )
                )
                row = result.scalars().first()
                if row:
                    return row
            if name and language:
                query = (
                    select(WhatsAppTemplateModel)
                    .where(WhatsAppTemplateModel.name == name)
                    .where(WhatsAppTemplateModel.language == language)
                )
                if waba_configuration_ids:
                    query = query.where(
                        WhatsAppTemplateModel.messaging_configuration_id.in_(
                            waba_configuration_ids
                        )
                    )
                result = await session.execute(query)
                return result.scalars().first()
        return None

    async def delete_whatsapp_template(
        self, template_id: int, *, organization_id: int
    ) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                delete(WhatsAppTemplateModel)
                .where(WhatsAppTemplateModel.id == template_id)
                .where(WhatsAppTemplateModel.organization_id == organization_id)
            )
            await session.commit()
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # Suppressions
    # ------------------------------------------------------------------
    async def upsert_messaging_suppression(
        self,
        *,
        organization_id: int,
        address: str,
        scope: str,
        reason: str,
        expires_at: datetime | None = None,
    ) -> MessagingSuppressionModel:
        async with self.async_session() as session:
            result = await session.execute(
                select(MessagingSuppressionModel)
                .where(MessagingSuppressionModel.organization_id == organization_id)
                .where(MessagingSuppressionModel.address == address)
                .where(MessagingSuppressionModel.scope == scope)
                .with_for_update()
            )
            row = result.scalars().first()
            if row:
                row.reason = reason
                row.expires_at = expires_at
                row.updated_at = datetime.now(UTC)
            else:
                row = MessagingSuppressionModel(
                    organization_id=organization_id,
                    address=address,
                    scope=scope,
                    reason=reason,
                    expires_at=expires_at,
                )
                session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def is_messaging_suppressed(
        self,
        *,
        organization_id: int,
        address: str,
        marketing: bool,
    ) -> bool:
        """True when the address must not receive this send. Marketing sends
        are blocked by both scopes; non-marketing only by scope='all'."""
        now = datetime.now(UTC)
        scopes = ["all", "marketing"] if marketing else ["all"]
        async with self.async_session() as session:
            result = await session.execute(
                select(MessagingSuppressionModel.id)
                .where(MessagingSuppressionModel.organization_id == organization_id)
                .where(MessagingSuppressionModel.address == address)
                .where(MessagingSuppressionModel.scope.in_(scopes))
                .where(
                    or_(
                        MessagingSuppressionModel.expires_at.is_(None),
                        MessagingSuppressionModel.expires_at > now,
                    )
                )
                .limit(1)
            )
            return result.scalars().first() is not None

    async def list_messaging_suppressions(
        self, organization_id: int, *, limit: int = 200, offset: int = 0
    ) -> list[MessagingSuppressionModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(MessagingSuppressionModel)
                .where(MessagingSuppressionModel.organization_id == organization_id)
                .order_by(MessagingSuppressionModel.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all())

    async def delete_messaging_suppression(
        self, suppression_id: int, *, organization_id: int
    ) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                delete(MessagingSuppressionModel)
                .where(MessagingSuppressionModel.id == suppression_id)
                .where(MessagingSuppressionModel.organization_id == organization_id)
            )
            await session.commit()
            return result.rowcount > 0

    async def purge_expired_messaging_suppressions(self) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                delete(MessagingSuppressionModel).where(
                    MessagingSuppressionModel.expires_at.isnot(None),
                    MessagingSuppressionModel.expires_at < datetime.now(UTC),
                )
            )
            await session.commit()
            return result.rowcount
