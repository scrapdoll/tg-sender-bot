from __future__ import annotations

from datetime import datetime, timezone
from random import Random

from aiogram.methods import SendInvoice
from aiogram.types import LabeledPrice
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_spam_agent.models import Base
from tg_spam_agent.repositories import (
    BillingRepository,
    DeliveryRepository,
    InboundRepository,
    MessageRepository,
    PlanRepository,
    SubscriptionRepository,
    SystemRepository,
    TenantRepository,
)
from tg_spam_agent.services.datetime_utils import ensure_utc


async def _tenant(session, user_id: int = 100) -> int:
    await SystemRepository(session).ensure_defaults((1,), 60, 5)
    tenant = await TenantRepository(session).ensure_tenant_for_user(
        user_id,
        default_interval_minutes=60,
        default_jitter_minutes=5,
    )
    return tenant.id


async def test_message_repository_random_only_from_enabled() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_id = await _tenant(session)

        repo = MessageRepository(session, tenant_id)
        first = await repo.create_message("first", created_by=1)
        second = await repo.create_message("second", created_by=1)
        await repo.toggle_message(second.id)

        chosen = await repo.choose_random_active_message(Random(1))
        assert chosen is not None
        assert chosen.id == first.id


async def test_subscription_repository_upsert_and_retry() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_id = await _tenant(session)
        repo = SubscriptionRepository(session, tenant_id)
        target = await repo.upsert_target("@publictarget/77", "public_topic", 77)
        assert target.join_status == "pending"
        assert target.topic_id == 77

        updated = await repo.mark_join_result(
            target.id,
            chat_id=123,
            title="Target",
            entity_type="channel",
            is_joined=True,
            join_status="joined",
            last_error=None,
        )
        assert updated.is_joined is True

        retried = await repo.queue_retry(target.id)
        assert retried.join_status == "retry"


async def test_system_repository_updates_schedule_fields_independently() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_id = await _tenant(session)
        repo = SystemRepository(session, tenant_id)
        last_run = datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc)
        first_next_run = datetime(2026, 4, 25, 11, 0, tzinfo=timezone.utc)
        second_next_run = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)

        await repo.update_settings(
            last_broadcast_at=last_run,
            next_broadcast_at=first_next_run,
        )
        updated = await repo.update_settings(next_broadcast_at=second_next_run)

        assert ensure_utc(updated.last_broadcast_at) == last_run
        assert ensure_utc(updated.next_broadcast_at) == second_next_run

        updated = await repo.update_settings(
            allow_paid_messages=True,
            max_paid_message_stars=250,
        )
        assert updated.allow_paid_messages is True
        assert updated.max_paid_message_stars == 250


async def test_inbound_repository_detects_seen_sender() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_id = await _tenant(session)
        repo = InboundRepository(session, tenant_id)

        assert await repo.has_events_from_sender(1001) is False

        await repo.log_inbound_event(
            sender_id=1001,
            username="sender",
            full_name="Sender Name",
            message_preview="hello",
            message_type="text",
        )

        assert await repo.has_events_from_sender(1001) is True
        assert await repo.has_events_from_sender(2002) is False


async def test_inbound_repository_lists_unique_sender_summaries() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_id = await _tenant(session)
        repo = InboundRepository(session, tenant_id)
        await repo.log_inbound_event(
            sender_id=1001,
            username="first",
            full_name="First User",
            message_preview="old message",
            message_type="text",
        )
        await repo.log_inbound_event(
            sender_id=2002,
            username=None,
            full_name=None,
            message_preview="another message",
            message_type="text",
        )
        await repo.log_inbound_event(
            sender_id=1001,
            username="first",
            full_name="First User",
            message_preview="latest message",
            message_type="text",
        )

        summaries = await repo.list_sender_summaries()

        assert [summary.event.sender_id for summary in summaries] == [1001, 2002]
        assert summaries[0].message_count == 2
        assert summaries[0].event.message_preview == "latest message"
        assert summaries[1].message_count == 1


async def test_delivery_repository_detects_success_since_timestamp() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_id = await _tenant(session)
        message = await MessageRepository(session, tenant_id).create_message("hello", created_by=1)
        target = await SubscriptionRepository(session, tenant_id).upsert_target(
            "@publictarget",
            "public",
        )
        repo = DeliveryRepository(session, tenant_id)
        before_success = datetime.now(timezone.utc)
        assert await repo.has_success_since(before_success) is False

        await repo.log_delivery(
            target_id=target.id,
            message_template_id=message.id,
            success=True,
        )

        assert await repo.has_success_since(before_success) is True


async def test_tenant_scoped_repositories_do_not_leak_data() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        first_tenant = await _tenant(session, 100)
        second_tenant = await _tenant(session, 200)

        await MessageRepository(session, first_tenant).create_message("first", 100)
        await MessageRepository(session, second_tenant).create_message("second", 200)
        await SubscriptionRepository(session, first_tenant).upsert_target("@first", "public")
        await SubscriptionRepository(session, second_tenant).upsert_target("@second", "public")

        first_messages = await MessageRepository(session, first_tenant).list_messages()
        first_targets = await SubscriptionRepository(session, first_tenant).list_targets()

        assert [message.text for message in first_messages] == ["first"]
        assert [target.source for target in first_targets] == ["@first"]


async def test_plan_admin_update_and_billing_payload() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_id = await _tenant(session, 100)
        plan_repo = PlanRepository(session)
        plan = await plan_repo.update_active_plan(
            price_stars=750,
            max_targets=25,
            max_templates=7,
            min_interval_minutes=45,
        )

        payload = BillingRepository.build_payload(tenant_id, plan.id)
        assert BillingRepository.parse_payload(payload) == (tenant_id, plan.id)
        assert plan.price_stars == 750
        assert plan.max_targets == 25
        assert plan.max_templates == 7
        assert plan.min_interval_minutes == 45

        subscription = await BillingRepository(session).activate_subscription(
            tenant_id=tenant_id,
            plan_id=plan.id,
            user_id=100,
            payload=payload,
            currency="XTR",
            total_amount=750,
            telegram_payment_charge_id="tg-charge",
            provider_payment_charge_id="provider-charge",
        )

        assert subscription is not None
        assert subscription.status == "active"


def test_stars_subscription_invoice_payload_contains_required_fields() -> None:
    invoice = SendInvoice(
        chat_id=100,
        title="Pro subscription",
        description="100 targets, 10 templates",
        payload=BillingRepository.build_payload(1, 1),
        currency="XTR",
        prices=[LabeledPrice(label="Pro", amount=500)],
        provider_token="",
        subscription_period=2_592_000,
    )

    dumped = invoice.model_dump()

    assert dumped["currency"] == "XTR"
    assert dumped["provider_token"] == ""
    assert dumped["subscription_period"] == 2_592_000
    assert dumped["prices"][0]["amount"] == 500
