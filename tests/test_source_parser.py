from __future__ import annotations

import pytest

from tg_spam_agent.services.source_parser import parse_target_source


def test_parse_username_source() -> None:
    parsed = parse_target_source("@ExamplePublic")

    assert parsed.normalized == "@examplepublic"
    assert parsed.access_type == "public"
    assert parsed.lookup_value == "ExamplePublic"


def test_parse_public_link() -> None:
    parsed = parse_target_source("https://t.me/TestChannel")

    assert parsed.normalized == "@testchannel"
    assert parsed.access_type == "public"
    assert parsed.lookup_value == "TestChannel"


def test_parse_private_invite() -> None:
    parsed = parse_target_source("https://t.me/+AbCdEf123")

    assert parsed.normalized == "invite:AbCdEf123"
    assert parsed.access_type == "private_invite"
    assert parsed.lookup_value == "AbCdEf123"


def test_parse_invalid_source() -> None:
    with pytest.raises(ValueError):
        parse_target_source("https://example.com/nope")
