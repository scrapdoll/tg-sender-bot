from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from random import Random

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_spam_agent.models import (
    BroadcastSettings,
    DeliveryLog,
    InboundEvent,
    ManagerUserPreference,
    MessageTemplate,
    Owner,
    SubscriptionTarget,
    WhitelistUser,
    utcnow,
)


_UNSET = object()


class SystemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_defaults(
        self,
        owner_ids: tuple[int, ...],
        default_interval_minutes: int,
        default_jitter_minutes: int,
    ) -> BroadcastSettings:
        settings = await self.get_settings()
        if settings is None:
            settings = BroadcastSettings(
                id=1,
                base_interval_minutes=default_interval_minutes,
                jitter_minutes=default_jitter_minutes,
                is_active=True,
            )
            self.session.add(settings)

        for owner_id in owner_ids:
            owner = await self.session.get(Owner, owner_id)
            if owner is None:
                self.session.add(Owner(user_id=owner_id))

        await self.session.commit()
        return settings

    async def get_settings(self) -> BroadcastSettings | None:
        return await self.session.get(BroadcastSettings, 1)

    async def update_settings(
        self,
        *,
        base_interval_minutes: int | None = None,
        jitter_minutes: int | None = None,
        is_active: bool | None = None,
        allow_paid_messages: bool | None = None,
        max_paid_message_stars: int | None = None,
        next_broadcast_at: datetime | None | object = _UNSET,
        last_broadcast_at: datetime | None | object = _UNSET,
    ) -> BroadcastSettings:
        settings = await self.session.get(BroadcastSettings, 1)
        if settings is None:
            settings = BroadcastSettings(id=1)
            self.session.add(settings)

        if base_interval_minutes is not None:
            settings.base_interval_minutes = base_interval_minutes
        if jitter_minutes is not None:
            settings.jitter_minutes = jitter_minutes
        if is_active is not None:
            settings.is_active = is_active
        if allow_paid_messages is not None:
            settings.allow_paid_messages = allow_paid_messages
        if max_paid_message_stars is not None:
            settings.max_paid_message_stars = max(0, max_paid_message_stars)
        if next_broadcast_at is not _UNSET:
            settings.next_broadcast_at = next_broadcast_at
        if last_broadcast_at is not _UNSET:
            settings.last_broadcast_at = last_broadcast_at

        await self.session.commit()
        return settings

    async def list_owner_ids(self) -> list[int]:
        result = await self.session.scalars(select(Owner.user_id).order_by(Owner.user_id))
        return list(result)

    async def get_status_counts(self) -> dict[str, int]:
        total_targets = await self.session.scalar(
            select(func.count()).select_from(SubscriptionTarget)
        )
        joined_targets = await self.session.scalar(
            select(func.count()).select_from(SubscriptionTarget).where(
                SubscriptionTarget.is_joined.is_(True),
                SubscriptionTarget.is_enabled.is_(True),
            )
        )
        pending_targets = await self.session.scalar(
            select(func.count()).select_from(SubscriptionTarget).where(
                SubscriptionTarget.join_status.in_(("pending", "retry", "approval_pending"))
            )
        )
        active_messages = await self.session.scalar(
            select(func.count()).select_from(MessageTemplate).where(
                MessageTemplate.is_enabled.is_(True)
            )
        )
        whitelist_users = await self.session.scalar(
            select(func.count()).select_from(WhitelistUser)
        )
        return {
            "total_targets": total_targets or 0,
            "joined_targets": joined_targets or 0,
            "pending_targets": pending_targets or 0,
            "active_messages": active_messages or 0,
            "whitelist_users": whitelist_users or 0,
        }


class AccessRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def is_owner(self, user_id: int) -> bool:
        return await self.session.get(Owner, user_id) is not None

    async def is_allowed_manager_user(self, user_id: int) -> bool:
        if await self.is_owner(user_id):
            return True
        return await self.session.get(WhitelistUser, user_id) is not None

    async def list_whitelist(self) -> list[WhitelistUser]:
        result = await self.session.scalars(
            select(WhitelistUser).order_by(WhitelistUser.user_id)
        )
        return list(result)

    async def add_whitelist_user(self, user_id: int) -> WhitelistUser:
        entry = await self.session.get(WhitelistUser, user_id)
        if entry is None:
            entry = WhitelistUser(user_id=user_id)
            self.session.add(entry)
            await self.session.commit()
        return entry

    async def remove_whitelist_user(self, user_id: int) -> bool:
        entry = await self.session.get(WhitelistUser, user_id)
        if entry is None:
            return False
        await self.session.delete(entry)
        await self.session.commit()
        return True


class ManagerPreferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_language(self, user_id: int) -> str:
        preference = await self.session.get(ManagerUserPreference, user_id)
        if preference is None:
            return "ru"
        return preference.language_code or "ru"

    async def set_language(self, user_id: int, language_code: str) -> ManagerUserPreference:
        normalized = "en" if language_code == "en" else "ru"
        preference = await self.session.get(ManagerUserPreference, user_id)
        if preference is None:
            preference = ManagerUserPreference(
                user_id=user_id,
                language_code=normalized,
            )
            self.session.add(preference)
        else:
            preference.language_code = normalized
        await self.session.commit()
        return preference


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_message(self, text: str, created_by: int) -> MessageTemplate:
        message = MessageTemplate(text=text, created_by=created_by)
        self.session.add(message)
        await self.session.commit()
        return message

    async def list_messages(self) -> list[MessageTemplate]:
        result = await self.session.scalars(
            select(MessageTemplate).order_by(MessageTemplate.created_at.desc())
        )
        return list(result)

    async def get_message(self, message_id: int) -> MessageTemplate | None:
        return await self.session.get(MessageTemplate, message_id)

    async def toggle_message(self, message_id: int) -> MessageTemplate | None:
        message = await self.session.get(MessageTemplate, message_id)
        if message is None:
            return None
        message.is_enabled = not message.is_enabled
        await self.session.commit()
        return message

    async def delete_message(self, message_id: int) -> bool:
        message = await self.session.get(MessageTemplate, message_id)
        if message is None:
            return False
        await self.session.delete(message)
        await self.session.commit()
        return True

    async def list_active_messages(self) -> list[MessageTemplate]:
        result = await self.session.scalars(
            select(MessageTemplate)
            .where(MessageTemplate.is_enabled.is_(True))
            .order_by(MessageTemplate.id)
        )
        return list(result)

    async def choose_random_active_message(
        self, rng: Random | None = None
    ) -> MessageTemplate | None:
        messages = await self.list_active_messages()
        if not messages:
            return None
        chooser = rng or Random()
        return chooser.choice(messages)


class SubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_target(
        self,
        source: str,
        access_type: str,
        topic_id: int | None = None,
    ) -> SubscriptionTarget:
        result = await self.session.scalar(
            select(SubscriptionTarget).where(SubscriptionTarget.source == source)
        )
        if result is None:
            result = SubscriptionTarget(
                source=source,
                access_type=access_type,
                topic_id=topic_id,
                join_status="pending",
                is_enabled=True,
                is_joined=False,
            )
            self.session.add(result)
        else:
            result.access_type = access_type
            result.topic_id = topic_id
            result.join_status = "pending"
            result.last_error = None
        await self.session.commit()
        return result

    async def list_targets(self) -> list[SubscriptionTarget]:
        result = await self.session.scalars(
            select(SubscriptionTarget).order_by(SubscriptionTarget.created_at.desc())
        )
        return list(result)

    async def get_target(self, target_id: int) -> SubscriptionTarget | None:
        return await self.session.get(SubscriptionTarget, target_id)

    async def list_pending_targets(self) -> list[SubscriptionTarget]:
        result = await self.session.scalars(
            select(SubscriptionTarget)
            .where(
                SubscriptionTarget.is_enabled.is_(True),
                SubscriptionTarget.is_joined.is_(False),
                SubscriptionTarget.join_status.in_(("pending", "retry")),
            )
            .order_by(SubscriptionTarget.id)
        )
        return list(result)

    async def list_enabled_joined_targets(self) -> list[SubscriptionTarget]:
        result = await self.session.scalars(
            select(SubscriptionTarget)
            .where(
                SubscriptionTarget.is_enabled.is_(True),
                SubscriptionTarget.is_joined.is_(True),
            )
            .order_by(SubscriptionTarget.id)
        )
        return list(result)

    async def mark_join_result(
        self,
        target_id: int,
        *,
        chat_id: int | None,
        title: str | None,
        entity_type: str,
        is_joined: bool,
        join_status: str,
        last_error: str | None,
    ) -> SubscriptionTarget | None:
        target = await self.session.get(SubscriptionTarget, target_id)
        if target is None:
            return None
        target.chat_id = chat_id
        target.title = title
        target.entity_type = entity_type
        target.is_joined = is_joined
        target.join_status = join_status
        target.last_error = last_error
        target.last_checked_at = utcnow()
        await self.session.commit()
        return target

    async def queue_retry(self, target_id: int) -> SubscriptionTarget | None:
        target = await self.session.get(SubscriptionTarget, target_id)
        if target is None:
            return None
        target.join_status = "retry"
        target.last_error = None
        target.last_checked_at = utcnow()
        await self.session.commit()
        return target

    async def toggle_enabled(self, target_id: int) -> SubscriptionTarget | None:
        target = await self.session.get(SubscriptionTarget, target_id)
        if target is None:
            return None
        target.is_enabled = not target.is_enabled
        await self.session.commit()
        return target

    async def delete_target(self, target_id: int) -> bool:
        target = await self.session.get(SubscriptionTarget, target_id)
        if target is None:
            return False
        await self.session.delete(target)
        await self.session.commit()
        return True

    async def disable_target_with_error(
        self, target_id: int, error: str
    ) -> SubscriptionTarget | None:
        target = await self.session.get(SubscriptionTarget, target_id)
        if target is None:
            return None
        target.is_enabled = False
        target.last_error = error
        target.last_checked_at = utcnow()
        await self.session.commit()
        return target


class DeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log_delivery(
        self,
        *,
        target_id: int,
        message_template_id: int | None,
        success: bool,
        error: str | None = None,
    ) -> DeliveryLog:
        delivery = DeliveryLog(
            target_id=target_id,
            message_template_id=message_template_id,
            success=success,
            error=error,
        )
        self.session.add(delivery)
        await self.session.commit()
        return delivery

    async def list_recent_failures(self, limit: int = 5) -> list[DeliveryLog]:
        result = await self.session.scalars(
            select(DeliveryLog)
            .where(
                or_(DeliveryLog.success.is_(False), DeliveryLog.error.is_not(None))
            )
            .order_by(DeliveryLog.attempted_at.desc())
            .limit(limit)
        )
        return list(result)

    async def has_success_since(self, since: datetime) -> bool:
        result = await self.session.scalar(
            select(DeliveryLog.id)
            .where(
                DeliveryLog.success.is_(True),
                DeliveryLog.attempted_at >= since,
            )
            .limit(1)
        )
        return result is not None


class InboundRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def has_events_from_sender(self, sender_id: int) -> bool:
        existing_id = await self.session.scalar(
            select(InboundEvent.id).where(InboundEvent.sender_id == sender_id).limit(1)
        )
        return existing_id is not None

    async def list_sender_summaries(self, limit: int = 50) -> list[InboundSenderSummary]:
        latest_by_sender = (
            select(
                InboundEvent.sender_id.label("sender_id"),
                func.max(InboundEvent.id).label("latest_id"),
                func.count(InboundEvent.id).label("message_count"),
            )
            .group_by(InboundEvent.sender_id)
            .subquery()
        )
        result = await self.session.execute(
            select(InboundEvent, latest_by_sender.c.message_count)
            .join(latest_by_sender, InboundEvent.id == latest_by_sender.c.latest_id)
            .order_by(InboundEvent.received_at.desc())
            .limit(limit)
        )
        return [
            InboundSenderSummary(event=event, message_count=message_count)
            for event, message_count in result.all()
        ]

    async def log_inbound_event(
        self,
        *,
        sender_id: int,
        username: str | None,
        full_name: str | None,
        message_preview: str | None,
        message_type: str,
    ) -> InboundEvent:
        event = InboundEvent(
            sender_id=sender_id,
            username=username,
            full_name=full_name,
            message_preview=message_preview,
            message_type=message_type,
        )
        self.session.add(event)
        await self.session.commit()
        return event


@dataclass(slots=True)
class InboundSenderSummary:
    event: InboundEvent
    message_count: int


@dataclass(slots=True)
class StatusSnapshot:
    settings: BroadcastSettings
    owner_ids: list[int]
    counts: dict[str, int]
    recent_failures: list[DeliveryLog]


class StatusRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_snapshot(self) -> StatusSnapshot:
        system_repo = SystemRepository(self.session)
        delivery_repo = DeliveryRepository(self.session)
        settings = await system_repo.get_settings()
        if settings is None:
            settings = BroadcastSettings(id=1)
        return StatusSnapshot(
            settings=settings,
            owner_ids=await system_repo.list_owner_ids(),
            counts=await system_repo.get_status_counts(),
            recent_failures=await delivery_repo.list_recent_failures(),
        )
