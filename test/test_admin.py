from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _make_context(
    client: object,
    *,
    prefix: str = "test",
    super_admins: list[int] | None = None,
) -> SimpleNamespace:
    bot_data = {"valkey_client": client, "valkey_prefix": prefix}
    if super_admins is not None:
        bot_data["super_admin_ids"] = super_admins
    return SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))


def test_add_and_remove_admin_roundtrip(bot_modules) -> None:
    client = bot_modules.storage.InMemoryValkey()
    context = _make_context(client, super_admins=[999])

    assert bot_modules.admin.add_admin(context, 123)
    assert 123 in bot_modules.admin.get_admins(context)

    removed = bot_modules.admin.remove_admin(context, 123)
    assert removed is True
    assert 123 not in bot_modules.admin.get_admins(context)


def test_recipients_for_audience_filters_recent(bot_modules) -> None:
    client = bot_modules.storage.InMemoryValkey()
    prefix = "testbot"
    context = _make_context(client, prefix=prefix)

    now = datetime.now(timezone.utc)
    recent = now - timedelta(days=2)
    stale = now - timedelta(days=45)

    recent_key = f"{prefix}:submission:recent"
    stale_key = f"{prefix}:submission:stale"

    client.hset(
        recent_key,
        mapping={"user_id": "1", "created_at": recent.isoformat()},
    )
    client.hset(
        stale_key,
        mapping={"user_id": "2", "created_at": stale.isoformat()},
    )
    client.sadd(f"{prefix}:applications", recent_key)
    client.sadd(f"{prefix}:applications", stale_key)
    client.sadd(f"{prefix}:users", "1")
    client.sadd(f"{prefix}:users", "2")

    all_recipients = bot_modules.admin.recipients_for_audience(context, "all")
    assert all_recipients == {1, 2}

    recent_recipients = bot_modules.admin.recipients_for_audience(context, "recent")
    assert recent_recipients == {1}


def test_mark_application_revoked_success(bot_modules) -> None:
    client = bot_modules.storage.InMemoryValkey()
    prefix = "testbot"
    context = _make_context(client, prefix=prefix)
    session_key = "session123"
    key = f"{prefix}:{session_key}"

    client.hset(
        key,
        mapping={
            "session_key": session_key,
            "user_id": "42",
            "position": "Test",
        },
    )
    client.sadd(f"{prefix}:applications", key)

    result = bot_modules.admin.mark_application_revoked(context, session_key, 42)
    assert result is True

    record = client.hgetall(key)
    assert record.get("revoked_by") == "42"
    assert record.get("revoked_at")


def test_mark_application_revoked_requires_owner(bot_modules) -> None:
    client = bot_modules.storage.InMemoryValkey()
    prefix = "testbot"
    context = _make_context(client, prefix=prefix)
    session_key = "session123"
    key = f"{prefix}:{session_key}"

    client.hset(
        key,
        mapping={
            "session_key": session_key,
            "user_id": "7",
        },
    )
    client.sadd(f"{prefix}:applications", key)

    result = bot_modules.admin.mark_application_revoked(context, session_key, 42)
    assert result is False

    record = client.hgetall(key)
    assert "revoked_at" not in record


def test_mark_application_reviewed_sets_fields(bot_modules) -> None:
    client = bot_modules.storage.InMemoryValkey()
    prefix = "testbot"
    context = _make_context(client, prefix=prefix)
    session_key = "session-review"
    key = f"{prefix}:{session_key}"

    client.hset(
        key,
        mapping={"session_key": session_key, "user_id": "7"},
    )

    timestamp = bot_modules.admin.mark_application_reviewed(context, session_key, 555)
    assert timestamp is not None

    record = client.hgetall(key)
    assert record.get("reviewed_by") == "555"
    assert record.get("reviewed_at") == timestamp


def test_clear_application_review_resets_fields(bot_modules) -> None:
    client = bot_modules.storage.InMemoryValkey()
    prefix = "testbot"
    context = _make_context(client, prefix=prefix)
    session_key = "session-review"
    key = f"{prefix}:{session_key}"

    client.hset(
        key,
        mapping={"session_key": session_key, "user_id": "7"},
    )

    timestamp = bot_modules.admin.mark_application_reviewed(context, session_key, 777)
    assert timestamp is not None

    success = bot_modules.admin.clear_application_review(context, session_key)
    assert success is True

    record = client.hgetall(key)
    assert record.get("reviewed_at") == ""
    assert record.get("reviewed_by") == ""


def test_show_admin_roster_lists_assignments(bot_modules) -> None:
    admin_commands = bot_modules.admin_commands
    storage = bot_modules.storage

    client = storage.InMemoryValkey()
    prefix = "testbot"
    bot_data = {
        "valkey_client": client,
        "valkey_prefix": prefix,
        "super_admin_ids": [101, 202],
    }
    context = SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))

    client.sadd(f"{prefix}:admins", "303")
    client.sadd(f"{prefix}:admins", "404")
    client.sadd(f"{prefix}:admins", "101")  # ensure duplicates are ignored

    class DummyMessage:
        def __init__(self) -> None:
            self.replies: list[tuple[str, str | None]] = []

        async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
            self.replies.append((text, parse_mode))

    message = DummyMessage()
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=101))

    async def invoke() -> None:
        await admin_commands.show_admin_roster(update, context)

    asyncio.run(invoke())

    assert len(message.replies) == 1
    text, parse_mode = message.replies[0]
    assert parse_mode == "Markdown"
    assert "101" in text and "202" in text
    assert "303" in text and "404" in text
    admins_label = admin_commands.get_message("admin.roster_admins")
    admin_section = text.split(admins_label, 1)[-1]
    assert "101" not in admin_section


def test_show_admin_roster_requires_super_admin(bot_modules) -> None:
    admin_commands = bot_modules.admin_commands
    storage = bot_modules.storage

    client = storage.InMemoryValkey()
    prefix = "testbot"
    bot_data = {
        "valkey_client": client,
        "valkey_prefix": prefix,
        "super_admin_ids": [1],
    }
    context = SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))

    class DummyMessage:
        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
            self.replies.append(text)

    message = DummyMessage()
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=999))

    async def invoke() -> None:
        await admin_commands.show_admin_roster(update, context)

    asyncio.run(invoke())

    assert message.replies == [admin_commands.get_message("admin.super_admin_required")]
