from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_spam_agent.models import Base
from tg_spam_agent.repositories import (
    MessageRepository,
    SubscriptionRepository,
    SystemRepository,
)
from tg_spam_agent.sender.app import _run_single_broadcast


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str, dict[str, object]]] = []

    async def get_entity(self, candidate):
        return candidate

    async def get_dialogs(self):
        return []

    async def send_message(self, entity, text, **kwargs) -> None:
        self.sent.append((entity, text, kwargs))


class FakeDebugNotifier:
    async def notify(self, *args, **kwargs) -> None:
        return None


async def test_broadcast_without_ready_inputs_does_not_advance_schedule() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await SystemRepository(session).ensure_defaults((1,), 60, 0)

    await _run_single_broadcast(FakeClient(), session_factory, FakeDebugNotifier())

    async with session_factory() as session:
        settings = await SystemRepository(session).get_settings()

    assert settings.last_broadcast_at is None
    assert settings.next_broadcast_at is None


async def test_broadcast_with_ready_inputs_sends_and_advances_schedule() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await SystemRepository(session).ensure_defaults((1,), 60, 0)
        message = await MessageRepository(session).create_message("hello", created_by=1)
        target = await SubscriptionRepository(session).upsert_target(
            "@publictarget",
            "public",
        )
        await SubscriptionRepository(session).mark_join_result(
            target.id,
            chat_id=123,
            title="Public Target",
            entity_type="channel",
            is_joined=True,
            join_status="joined",
            last_error=None,
        )

    client = FakeClient()
    await _run_single_broadcast(client, session_factory, FakeDebugNotifier())

    async with session_factory() as session:
        settings = await SystemRepository(session).get_settings()

    assert client.sent == [("publictarget", message.text, {})]
    assert settings.last_broadcast_at is not None
    assert settings.next_broadcast_at is not None
