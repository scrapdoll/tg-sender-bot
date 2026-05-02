from __future__ import annotations

from tg_spam_agent.config import Settings


def test_sender_debug_errors_are_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SENDER_DEBUG_ERRORS_TO_CHAT", raising=False)
    monkeypatch.delenv("SENDER_DEBUG_ERROR_COOLDOWN_SECONDS", raising=False)
    monkeypatch.setenv("PLATFORM_ADMIN_IDS", "100,200")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", "secret")

    settings = Settings.load()

    assert settings.sender_debug_errors_to_chat is False
    assert settings.sender_debug_error_cooldown_seconds == 300
    assert settings.platform_admin_ids == (100, 200)
    assert settings.session_encryption_key == "secret"


def test_sender_debug_errors_can_be_enabled(monkeypatch) -> None:
    monkeypatch.setenv("SENDER_DEBUG_ERRORS_TO_CHAT", "true")
    monkeypatch.setenv("SENDER_DEBUG_ERROR_COOLDOWN_SECONDS", "10")

    settings = Settings.load()

    assert settings.sender_debug_errors_to_chat is True
    assert settings.sender_debug_error_cooldown_seconds == 10
