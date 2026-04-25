from __future__ import annotations

import pytest

from tg_spam_agent.services.source_parser import parse_target_source, split_target_sources


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


def test_parse_public_forum_topic_link() -> None:
    parsed = parse_target_source("https://t.me/TestForum/123")

    assert parsed.normalized == "@testforum/123"
    assert parsed.access_type == "public_topic"
    assert parsed.lookup_value == "TestForum"
    assert parsed.topic_id == 123


def test_parse_at_username_forum_topic_source() -> None:
    parsed = parse_target_source("@sbtgifts/33229")

    assert parsed.normalized == "@sbtgifts/33229"
    assert parsed.access_type == "public_topic"
    assert parsed.lookup_value == "sbtgifts"
    assert parsed.topic_id == 33229


def test_parse_bare_username_forum_topic_source() -> None:
    parsed = parse_target_source("sbtgifts/33229")

    assert parsed.normalized == "@sbtgifts/33229"
    assert parsed.access_type == "public_topic"
    assert parsed.lookup_value == "sbtgifts"
    assert parsed.topic_id == 33229


def test_parse_private_forum_topic_link() -> None:
    parsed = parse_target_source("https://t.me/c/1234567890/42")

    assert parsed.normalized == "c:1234567890/42"
    assert parsed.access_type == "private_topic"
    assert parsed.lookup_value == "-1001234567890"
    assert parsed.topic_id == 42


def test_parse_private_invite() -> None:
    parsed = parse_target_source("https://t.me/+AbCdEf123")

    assert parsed.normalized == "invite:AbCdEf123"
    assert parsed.access_type == "private_invite"
    assert parsed.lookup_value == "AbCdEf123"


def test_parse_invalid_source() -> None:
    with pytest.raises(ValueError):
        parse_target_source("https://example.com/nope")


def test_split_target_sources_by_space_comma_and_newline() -> None:
    raw = "@one, @two\nhttps://t.me/three/4   https://t.me/+invite"

    assert split_target_sources(raw) == [
        "@one",
        "@two",
        "https://t.me/three/4",
        "https://t.me/+invite",
    ]
