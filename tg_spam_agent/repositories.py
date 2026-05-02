from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from random import Random

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_spam_agent.models import (
    BroadcastSettings,
    DeliveryLog,
    InboundEvent,
    ManagerUserPreference,
    MessageTemplate,
    PaymentEvent,
    PlatformAdmin,
    SubscriptionPlan,
    SubscriptionTarget,
    TelegramSession,
    Tenant,
    TenantMember,
    TenantSubscription,
    utcnow,
)
from tg_spam_agent.services.datetime_utils import ensure_utc


DEFAULT_PLAN_NAME = "Pro"
DEFAULT_PLAN_PRICE_STARS = 500
DEFAULT_PLAN_PERIOD_SECONDS = 2_592_000
DEFAULT_PLAN_MAX_TARGETS = 100
DEFAULT_PLAN_MAX_TEMPLATES = 10
DEFAULT_PLAN_MIN_INTERVAL_MINUTES = 30

_UNSET = object()


def _require_tenant_id(tenant_id: int | None) -> int:
    if tenant_id is None:
        raise ValueError("tenant_id is required for tenant-scoped repository operations")
    return tenant_id


@dataclass(slots=True)
class TenantContext:
    tenant_id: int
    user_id: int
    role: str
    is_platform_admin: bool
    subscription_status: str
    subscription_active: bool

    @property
    def can_manage(self) -> bool:
        return self.role in {"owner", "admin"} or self.is_platform_admin

    @property
    def can_mutate(self) -> bool:
        return self.can_manage and self.subscription_active


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def sync_platform_admins(self, platform_admin_ids: tuple[int, ...]) -> None:
        for user_id in platform_admin_ids:
            if await self.session.get(PlatformAdmin, user_id) is None:
                self.session.add(PlatformAdmin(user_id=user_id))
        await self.session.commit()

    async def ensure_default_plan(self) -> SubscriptionPlan:
        plan = await self.session.scalar(
            select(SubscriptionPlan)
            .where(SubscriptionPlan.is_active.is_(True))
            .order_by(SubscriptionPlan.id)
            .limit(1)
        )
        if plan is None:
            plan = SubscriptionPlan(
                name=DEFAULT_PLAN_NAME,
                price_stars=DEFAULT_PLAN_PRICE_STARS,
                period_seconds=DEFAULT_PLAN_PERIOD_SECONDS,
                max_targets=DEFAULT_PLAN_MAX_TARGETS,
                max_templates=DEFAULT_PLAN_MAX_TEMPLATES,
                min_interval_minutes=DEFAULT_PLAN_MIN_INTERVAL_MINUTES,
                is_active=True,
            )
            self.session.add(plan)
            await self.session.commit()
        return plan

    async def ensure_tenant_for_user(
        self,
        user_id: int,
        *,
        default_interval_minutes: int,
        default_jitter_minutes: int,
    ) -> Tenant:
        member = await self.session.scalar(
            select(TenantMember)
            .where(TenantMember.user_id == user_id)
            .order_by(TenantMember.created_at)
            .limit(1)
        )
        if member is not None:
            tenant = await self.session.get(Tenant, member.tenant_id)
            if tenant is not None:
                return tenant

        tenant = Tenant(owner_user_id=user_id, status="active")
        self.session.add(tenant)
        await self.session.flush()
        self.session.add(TenantMember(tenant_id=tenant.id, user_id=user_id, role="owner"))
        self.session.add(
            BroadcastSettings(
                tenant_id=tenant.id,
                base_interval_minutes=default_interval_minutes,
                jitter_minutes=default_jitter_minutes,
            )
        )
        self.session.add(TenantSubscription(tenant_id=tenant.id, status="inactive"))
        self.session.add(TelegramSession(tenant_id=tenant.id, status="missing"))
        await self.session.commit()
        return tenant

    async def get_context(
        self,
        user_id: int,
        *,
        default_interval_minutes: int,
        default_jitter_minutes: int,
    ) -> TenantContext:
        tenant = await self.ensure_tenant_for_user(
            user_id,
            default_interval_minutes=default_interval_minutes,
            default_jitter_minutes=default_jitter_minutes,
        )
        member = await self.session.get(TenantMember, (tenant.id, user_id))
        is_platform_admin = await self.session.get(PlatformAdmin, user_id) is not None
        subscription = await self.session.get(TenantSubscription, tenant.id)
        status = subscription.status if subscription is not None else "inactive"
        active = self._subscription_is_active(subscription)
        return TenantContext(
            tenant_id=tenant.id,
            user_id=user_id,
            role=member.role if member is not None else "viewer",
            is_platform_admin=is_platform_admin,
            subscription_status=status,
            subscription_active=active,
        )

    async def list_active_sender_tenants(self) -> list[int]:
        now = datetime.now(timezone.utc)
        result = await self.session.scalars(
            select(Tenant.id)
            .join(TenantSubscription, TenantSubscription.tenant_id == Tenant.id)
            .join(TelegramSession, TelegramSession.tenant_id == Tenant.id)
            .where(
                Tenant.status == "active",
                TenantSubscription.status == "active",
                TenantSubscription.current_period_end.is_not(None),
                TenantSubscription.current_period_end > now,
                TelegramSession.status == "connected",
                TelegramSession.encrypted_string_session.is_not(None),
            )
            .order_by(Tenant.id)
        )
        return list(result)

    @staticmethod
    def _subscription_is_active(subscription: TenantSubscription | None) -> bool:
        if subscription is None or subscription.status != "active":
            return False
        period_end = ensure_utc(subscription.current_period_end)
        return period_end is not None and period_end > datetime.now(timezone.utc)


class PlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active_plan(self) -> SubscriptionPlan | None:
        result = await self.session.scalars(
            select(SubscriptionPlan)
            .where(SubscriptionPlan.is_active.is_(True))
            .order_by(SubscriptionPlan.id)
            .limit(1)
        )
        return result.first()

    async def get_plan(self, plan_id: int) -> SubscriptionPlan | None:
        return await self.session.get(SubscriptionPlan, plan_id)

    async def update_active_plan(
        self,
        *,
        price_stars: int | None = None,
        max_targets: int | None = None,
        max_templates: int | None = None,
        min_interval_minutes: int | None = None,
        is_active: bool | None = None,
    ) -> SubscriptionPlan:
        plan = await TenantRepository(self.session).ensure_default_plan()
        if price_stars is not None:
            plan.price_stars = max(1, min(price_stars, 10_000))
        if max_targets is not None:
            plan.max_targets = max(1, max_targets)
        if max_templates is not None:
            plan.max_templates = max(1, max_templates)
        if min_interval_minutes is not None:
            plan.min_interval_minutes = max(1, min_interval_minutes)
        if is_active is not None:
            plan.is_active = is_active
        await self.session.commit()
        return plan


class BillingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def build_payload(tenant_id: int, plan_id: int) -> str:
        return f"tenant:{tenant_id}:plan:{plan_id}"

    @staticmethod
    def parse_payload(payload: str) -> tuple[int, int] | None:
        parts = payload.split(":")
        if len(parts) != 4 or parts[0] != "tenant" or parts[2] != "plan":
            return None
        try:
            return int(parts[1]), int(parts[3])
        except ValueError:
            return None

    async def get_subscription(self, tenant_id: int) -> TenantSubscription | None:
        return await self.session.get(TenantSubscription, tenant_id)

    async def activate_subscription(
        self,
        *,
        tenant_id: int,
        plan_id: int,
        user_id: int,
        payload: str,
        currency: str,
        total_amount: int,
        telegram_payment_charge_id: str | None,
        provider_payment_charge_id: str | None,
    ) -> TenantSubscription | None:
        plan = await self.session.get(SubscriptionPlan, plan_id)
        if plan is None or currency != "XTR" or total_amount != plan.price_stars:
            return None
        subscription = await self.session.get(TenantSubscription, tenant_id)
        if subscription is None:
            subscription = TenantSubscription(tenant_id=tenant_id)
            self.session.add(subscription)
        subscription.plan_id = plan.id
        subscription.status = "active"
        subscription.current_period_end = datetime.now(timezone.utc) + timedelta(
            seconds=plan.period_seconds
        )
        subscription.telegram_payment_charge_id = telegram_payment_charge_id
        subscription.provider_payment_charge_id = provider_payment_charge_id
        self.session.add(
            PaymentEvent(
                tenant_id=tenant_id,
                plan_id=plan_id,
                user_id=user_id,
                event_type="successful_payment",
                payload=payload,
                currency=currency,
                total_amount=total_amount,
                telegram_payment_charge_id=telegram_payment_charge_id,
                provider_payment_charge_id=provider_payment_charge_id,
            )
        )
        await self.session.commit()
        return subscription

    async def record_refund(
        self,
        *,
        tenant_id: int,
        plan_id: int | None,
        user_id: int,
        payload: str,
        currency: str,
        total_amount: int,
        telegram_payment_charge_id: str | None,
        provider_payment_charge_id: str | None,
    ) -> None:
        subscription = await self.session.get(TenantSubscription, tenant_id)
        if subscription is not None:
            subscription.status = "canceled"
        self.session.add(
            PaymentEvent(
                tenant_id=tenant_id,
                plan_id=plan_id,
                user_id=user_id,
                event_type="refunded_payment",
                payload=payload,
                currency=currency,
                total_amount=total_amount,
                telegram_payment_charge_id=telegram_payment_charge_id,
                provider_payment_charge_id=provider_payment_charge_id,
            )
        )
        await self.session.commit()


class TelegramSessionRepository:
    def __init__(self, session: AsyncSession, tenant_id: int | None) -> None:
        self.session = session
        self.tenant_id = _require_tenant_id(tenant_id)

    async def get(self) -> TelegramSession:
        session = await self.session.get(TelegramSession, self.tenant_id)
        if session is None:
            session = TelegramSession(tenant_id=self.tenant_id, status="missing")
            self.session.add(session)
            await self.session.commit()
        return session

    async def save_connected(
        self,
        *,
        encrypted_string_session: str,
        telegram_user_id: int | None,
    ) -> TelegramSession:
        session = await self.get()
        session.encrypted_string_session = encrypted_string_session
        session.telegram_user_id = telegram_user_id
        session.status = "connected"
        session.last_error = None
        await self.session.commit()
        return session

    async def set_error(self, error: str) -> TelegramSession:
        session = await self.get()
        session.status = "error"
        session.last_error = error
        await self.session.commit()
        return session

    async def disconnect(self) -> TelegramSession:
        session = await self.get()
        session.encrypted_string_session = None
        session.telegram_user_id = None
        session.status = "missing"
        session.last_error = None
        await self.session.commit()
        return session


class SystemRepository:
    def __init__(self, session: AsyncSession, tenant_id: int | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def ensure_defaults(
        self,
        platform_admin_ids: tuple[int, ...],
        default_interval_minutes: int,
        default_jitter_minutes: int,
    ) -> SubscriptionPlan:
        tenant_repo = TenantRepository(self.session)
        await tenant_repo.sync_platform_admins(platform_admin_ids)
        return await tenant_repo.ensure_default_plan()

    async def get_settings(self) -> BroadcastSettings | None:
        tenant_id = _require_tenant_id(self.tenant_id)
        return await self.session.scalar(
            select(BroadcastSettings).where(BroadcastSettings.tenant_id == tenant_id)
        )

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
        tenant_id = _require_tenant_id(self.tenant_id)
        settings = await self.get_settings()
        if settings is None:
            settings = BroadcastSettings(tenant_id=tenant_id)
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
        tenant_id = _require_tenant_id(self.tenant_id)
        result = await self.session.scalars(
            select(TenantMember.user_id)
            .where(
                TenantMember.tenant_id == tenant_id,
                TenantMember.role.in_(("owner", "admin")),
            )
            .order_by(TenantMember.user_id)
        )
        return list(result)

    async def list_platform_admin_ids(self) -> list[int]:
        result = await self.session.scalars(
            select(PlatformAdmin.user_id).order_by(PlatformAdmin.user_id)
        )
        return list(result)

    async def get_status_counts(self) -> dict[str, int]:
        tenant_id = _require_tenant_id(self.tenant_id)
        total_targets = await self.session.scalar(
            select(func.count()).select_from(SubscriptionTarget).where(
                SubscriptionTarget.tenant_id == tenant_id
            )
        )
        joined_targets = await self.session.scalar(
            select(func.count()).select_from(SubscriptionTarget).where(
                SubscriptionTarget.tenant_id == tenant_id,
                SubscriptionTarget.is_joined.is_(True),
                SubscriptionTarget.is_enabled.is_(True),
            )
        )
        pending_targets = await self.session.scalar(
            select(func.count()).select_from(SubscriptionTarget).where(
                SubscriptionTarget.tenant_id == tenant_id,
                SubscriptionTarget.join_status.in_(("pending", "retry", "approval_pending")),
            )
        )
        active_messages = await self.session.scalar(
            select(func.count()).select_from(MessageTemplate).where(
                MessageTemplate.tenant_id == tenant_id,
                MessageTemplate.is_enabled.is_(True),
            )
        )
        members = await self.session.scalar(
            select(func.count()).select_from(TenantMember).where(
                TenantMember.tenant_id == tenant_id
            )
        )
        return {
            "total_targets": total_targets or 0,
            "joined_targets": joined_targets or 0,
            "pending_targets": pending_targets or 0,
            "active_messages": active_messages or 0,
            "whitelist_users": members or 0,
        }


class AccessRepository:
    def __init__(self, session: AsyncSession, tenant_id: int | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def is_platform_admin(self, user_id: int) -> bool:
        return await self.session.get(PlatformAdmin, user_id) is not None

    async def is_owner(self, user_id: int) -> bool:
        tenant_id = _require_tenant_id(self.tenant_id)
        member = await self.session.get(TenantMember, (tenant_id, user_id))
        return member is not None and member.role == "owner"

    async def is_allowed_manager_user(self, user_id: int) -> bool:
        if await self.is_platform_admin(user_id):
            return True
        tenant_id = _require_tenant_id(self.tenant_id)
        return await self.session.get(TenantMember, (tenant_id, user_id)) is not None

    async def list_whitelist(self) -> list[TenantMember]:
        tenant_id = _require_tenant_id(self.tenant_id)
        result = await self.session.scalars(
            select(TenantMember)
            .where(TenantMember.tenant_id == tenant_id)
            .order_by(TenantMember.user_id)
        )
        return list(result)

    async def add_whitelist_user(self, user_id: int) -> TenantMember:
        tenant_id = _require_tenant_id(self.tenant_id)
        entry = await self.session.get(TenantMember, (tenant_id, user_id))
        if entry is None:
            entry = TenantMember(tenant_id=tenant_id, user_id=user_id, role="admin")
            self.session.add(entry)
            await self.session.commit()
        return entry

    async def remove_whitelist_user(self, user_id: int) -> bool:
        tenant_id = _require_tenant_id(self.tenant_id)
        entry = await self.session.get(TenantMember, (tenant_id, user_id))
        if entry is None or entry.role == "owner":
            return False
        await self.session.delete(entry)
        await self.session.commit()
        return True


class ManagerPreferenceRepository:
    def __init__(self, session: AsyncSession, tenant_id: int | None) -> None:
        self.session = session
        self.tenant_id = _require_tenant_id(tenant_id)

    async def get_language(self, user_id: int) -> str:
        preference = await self.session.get(
            ManagerUserPreference, (self.tenant_id, user_id)
        )
        if preference is None:
            return "ru"
        return preference.language_code or "ru"

    async def set_language(self, user_id: int, language_code: str) -> ManagerUserPreference:
        normalized = "en" if language_code == "en" else "ru"
        preference = await self.session.get(
            ManagerUserPreference, (self.tenant_id, user_id)
        )
        if preference is None:
            preference = ManagerUserPreference(
                tenant_id=self.tenant_id,
                user_id=user_id,
                language_code=normalized,
            )
            self.session.add(preference)
        else:
            preference.language_code = normalized
        await self.session.commit()
        return preference


class MessageRepository:
    def __init__(self, session: AsyncSession, tenant_id: int | None) -> None:
        self.session = session
        self.tenant_id = _require_tenant_id(tenant_id)

    async def create_message(self, text: str, created_by: int) -> MessageTemplate:
        message = MessageTemplate(
            tenant_id=self.tenant_id,
            text=text,
            created_by=created_by,
        )
        self.session.add(message)
        await self.session.commit()
        return message

    async def count_messages(self) -> int:
        return (
            await self.session.scalar(
                select(func.count()).select_from(MessageTemplate).where(
                    MessageTemplate.tenant_id == self.tenant_id
                )
            )
            or 0
        )

    async def list_messages(self) -> list[MessageTemplate]:
        result = await self.session.scalars(
            select(MessageTemplate)
            .where(MessageTemplate.tenant_id == self.tenant_id)
            .order_by(MessageTemplate.created_at.desc())
        )
        return list(result)

    async def get_message(self, message_id: int) -> MessageTemplate | None:
        return await self.session.scalar(
            select(MessageTemplate).where(
                MessageTemplate.tenant_id == self.tenant_id,
                MessageTemplate.id == message_id,
            )
        )

    async def toggle_message(self, message_id: int) -> MessageTemplate | None:
        message = await self.get_message(message_id)
        if message is None:
            return None
        message.is_enabled = not message.is_enabled
        await self.session.commit()
        return message

    async def delete_message(self, message_id: int) -> bool:
        message = await self.get_message(message_id)
        if message is None:
            return False
        await self.session.delete(message)
        await self.session.commit()
        return True

    async def list_active_messages(self) -> list[MessageTemplate]:
        result = await self.session.scalars(
            select(MessageTemplate)
            .where(
                MessageTemplate.tenant_id == self.tenant_id,
                MessageTemplate.is_enabled.is_(True),
            )
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
    def __init__(self, session: AsyncSession, tenant_id: int | None) -> None:
        self.session = session
        self.tenant_id = _require_tenant_id(tenant_id)

    async def upsert_target(
        self,
        source: str,
        access_type: str,
        topic_id: int | None = None,
    ) -> SubscriptionTarget:
        result = await self.session.scalar(
            select(SubscriptionTarget).where(
                SubscriptionTarget.tenant_id == self.tenant_id,
                SubscriptionTarget.source == source,
            )
        )
        if result is None:
            result = SubscriptionTarget(
                tenant_id=self.tenant_id,
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

    async def count_targets(self) -> int:
        return (
            await self.session.scalar(
                select(func.count()).select_from(SubscriptionTarget).where(
                    SubscriptionTarget.tenant_id == self.tenant_id
                )
            )
            or 0
        )

    async def list_targets(self) -> list[SubscriptionTarget]:
        result = await self.session.scalars(
            select(SubscriptionTarget)
            .where(SubscriptionTarget.tenant_id == self.tenant_id)
            .order_by(SubscriptionTarget.created_at.desc())
        )
        return list(result)

    async def get_target(self, target_id: int) -> SubscriptionTarget | None:
        return await self.session.scalar(
            select(SubscriptionTarget).where(
                SubscriptionTarget.tenant_id == self.tenant_id,
                SubscriptionTarget.id == target_id,
            )
        )

    async def list_pending_targets(self) -> list[SubscriptionTarget]:
        result = await self.session.scalars(
            select(SubscriptionTarget)
            .where(
                SubscriptionTarget.tenant_id == self.tenant_id,
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
                SubscriptionTarget.tenant_id == self.tenant_id,
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
        target = await self.get_target(target_id)
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
        target = await self.get_target(target_id)
        if target is None:
            return None
        target.join_status = "retry"
        target.last_error = None
        target.last_checked_at = utcnow()
        await self.session.commit()
        return target

    async def toggle_enabled(self, target_id: int) -> SubscriptionTarget | None:
        target = await self.get_target(target_id)
        if target is None:
            return None
        target.is_enabled = not target.is_enabled
        await self.session.commit()
        return target

    async def delete_target(self, target_id: int) -> bool:
        target = await self.get_target(target_id)
        if target is None:
            return False
        await self.session.delete(target)
        await self.session.commit()
        return True

    async def disable_target_with_error(
        self, target_id: int, error: str
    ) -> SubscriptionTarget | None:
        target = await self.get_target(target_id)
        if target is None:
            return None
        target.is_enabled = False
        target.last_error = error
        target.last_checked_at = utcnow()
        await self.session.commit()
        return target


class DeliveryRepository:
    def __init__(self, session: AsyncSession, tenant_id: int | None) -> None:
        self.session = session
        self.tenant_id = _require_tenant_id(tenant_id)

    async def log_delivery(
        self,
        *,
        target_id: int,
        message_template_id: int | None,
        success: bool,
        error: str | None = None,
    ) -> DeliveryLog:
        delivery = DeliveryLog(
            tenant_id=self.tenant_id,
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
                DeliveryLog.tenant_id == self.tenant_id,
                or_(DeliveryLog.success.is_(False), DeliveryLog.error.is_not(None)),
            )
            .order_by(DeliveryLog.attempted_at.desc())
            .limit(limit)
        )
        return list(result)

    async def has_success_since(self, since: datetime) -> bool:
        result = await self.session.scalar(
            select(DeliveryLog.id)
            .where(
                DeliveryLog.tenant_id == self.tenant_id,
                DeliveryLog.success.is_(True),
                DeliveryLog.attempted_at >= since,
            )
            .limit(1)
        )
        return result is not None


class InboundRepository:
    def __init__(self, session: AsyncSession, tenant_id: int | None) -> None:
        self.session = session
        self.tenant_id = _require_tenant_id(tenant_id)

    async def has_events_from_sender(self, sender_id: int) -> bool:
        existing_id = await self.session.scalar(
            select(InboundEvent.id)
            .where(
                InboundEvent.tenant_id == self.tenant_id,
                InboundEvent.sender_id == sender_id,
            )
            .limit(1)
        )
        return existing_id is not None

    async def list_sender_summaries(self, limit: int = 50) -> list[InboundSenderSummary]:
        latest_by_sender = (
            select(
                InboundEvent.sender_id.label("sender_id"),
                func.max(InboundEvent.id).label("latest_id"),
                func.count(InboundEvent.id).label("message_count"),
            )
            .where(InboundEvent.tenant_id == self.tenant_id)
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
            tenant_id=self.tenant_id,
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
    subscription: TenantSubscription | None
    plan: SubscriptionPlan | None
    telegram_session: TelegramSession


class StatusRepository:
    def __init__(self, session: AsyncSession, tenant_id: int | None) -> None:
        self.session = session
        self.tenant_id = _require_tenant_id(tenant_id)

    async def get_snapshot(self) -> StatusSnapshot:
        system_repo = SystemRepository(self.session, self.tenant_id)
        delivery_repo = DeliveryRepository(self.session, self.tenant_id)
        settings = await system_repo.get_settings()
        if settings is None:
            settings = BroadcastSettings(tenant_id=self.tenant_id)
        subscription = await BillingRepository(self.session).get_subscription(self.tenant_id)
        plan = None
        if subscription is not None and subscription.plan_id is not None:
            plan = await PlanRepository(self.session).get_plan(subscription.plan_id)
        if plan is None:
            plan = await PlanRepository(self.session).get_active_plan()
        return StatusSnapshot(
            settings=settings,
            owner_ids=await system_repo.list_owner_ids(),
            counts=await system_repo.get_status_counts(),
            recent_failures=await delivery_repo.list_recent_failures(),
            subscription=subscription,
            plan=plan,
            telegram_session=await TelegramSessionRepository(
                self.session, self.tenant_id
            ).get(),
        )
