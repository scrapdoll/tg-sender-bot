from __future__ import annotations

import argparse
import asyncio

from tg_spam_agent.config import Settings
from tg_spam_agent.db import create_session_factory, init_database
from tg_spam_agent.logging_utils import configure_logging
from tg_spam_agent.manager.app import run_manager_bot
from tg_spam_agent.sender.app import run_sender_userbot
from tg_spam_agent.sender.auth import init_userbot_session


async def _bootstrap_settings() -> tuple[Settings, object]:
    settings = Settings.load()
    settings.ensure_runtime_dirs()
    session_factory = create_session_factory(settings)
    await init_database(session_factory, settings)
    return settings, session_factory


async def _run_manager() -> None:
    settings, session_factory = await _bootstrap_settings()
    await run_manager_bot(settings, session_factory)


async def _run_sender() -> None:
    settings, session_factory = await _bootstrap_settings()
    await run_sender_userbot(settings, session_factory)


async def _run_init_session() -> None:
    settings = Settings.load()
    settings.ensure_runtime_dirs()
    await init_userbot_session(settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram spam agent toolkit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run-manager", help="Run the manager bot")
    subparsers.add_parser("run-sender", help="Run the Telethon sender userbot")
    subparsers.add_parser(
        "init-userbot-session",
        help="Deprecated: connect userbot sessions through the manager bot Account menu",
    )
    return parser


def main() -> None:
    settings = Settings.load()
    configure_logging(settings.log_level)
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run-manager":
        asyncio.run(_run_manager())
    elif args.command == "run-sender":
        asyncio.run(_run_sender())
    elif args.command == "init-userbot-session":
        asyncio.run(_run_init_session())
