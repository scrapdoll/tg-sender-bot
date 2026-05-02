from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_spam_agent.models import Base
from tg_spam_agent.repositories import (
    MessageRepository,
    SubscriptionRepository,
    SystemRepository,
    TelegramSessionRepository,
    TenantRepository,
)
from tg_spam_agent.sender.app import _run_single_broadcast
from tg_spam_agent.sender.app import _extract_required_paid_stars
from telethon.errors import RPCError


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str, int | None]] = []

    async def get_entity(self, candidate):
        return candidate

    async def get_input_entity(self, entity):
        return entity

    async def get_dialogs(self):
        return []

    async def __call__(self, request):
        self.sent.append((request.peer, request.message, request.allow_paid_stars))


class FakeDebugNotifier:
    async def notify(self, *args, **kwargs) -> None:
        return None


async def _tenant(session, user_id: int = 100) -> int:
    await SystemRepository(session).ensure_defaults((1,), 60, 0)
    tenant = await TenantRepository(session).ensure_tenant_for_user(
        user_id,
        default_interval_minutes=60,
        default_jitter_minutes=0,
    )
    return tenant.id


async def test_broadcast_without_ready_inputs_does_not_advance_schedule() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_id = await _tenant(session)

    await _run_single_broadcast(FakeClient(), session_factory, FakeDebugNotifier(), tenant_id)

    async with session_factory() as session:
        settings = await SystemRepository(session, tenant_id).get_settings()

    assert settings.last_broadcast_at is None
    assert settings.next_broadcast_at is None


async def test_broadcast_with_ready_inputs_sends_and_advances_schedule() -> None:
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
        await SubscriptionRepository(session, tenant_id).mark_join_result(
            target.id,
            chat_id=123,
            title="Public Target",
            entity_type="channel",
            is_joined=True,
            join_status="joined",
            last_error=None,
        )

    client = FakeClient()
    await _run_single_broadcast(client, session_factory, FakeDebugNotifier(), tenant_id)

    async with session_factory() as session:
        settings = await SystemRepository(session, tenant_id).get_settings()

    assert client.sent == [("publictarget", message.text, None)]
    assert settings.last_broadcast_at is not None
    assert settings.next_broadcast_at is not None


def test_extract_required_paid_stars_from_rpc_error() -> None:
    error = RPCError(None, "ALLOW_PAYMENT_REQUIRED_42", 400)

    assert _extract_required_paid_stars(error) == 42


async def test_sender_tenant_selection_requires_active_subscription_and_session() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_id = await _tenant(session, 100)
        assert await TenantRepository(session).list_active_sender_tenants() == []

        await TelegramSessionRepository(session, tenant_id).save_connected(
            encrypted_string_session="encrypted",
            telegram_user_id=100,
        )
        assert await TenantRepository(session).list_active_sender_tenants() == []
