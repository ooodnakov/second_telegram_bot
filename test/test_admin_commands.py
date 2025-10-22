from __future__ import annotations

import asyncio
from types import SimpleNamespace


class DummyMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
        self.replies.append(text)


def _build_context(bot_modules, bot) -> SimpleNamespace:
    storage = bot_modules.storage
    client = storage.InMemoryValkey()
    bot_data = {
        "valkey_client": client,
        "valkey_prefix": "testbot",
        "super_admin_ids": [1],
    }
    return SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=bot,
        user_data={},
    )


def test_receive_admin_id_accepts_username(bot_modules) -> None:
    admin_commands = bot_modules.admin_commands
    admin_module = bot_modules.admin

    class ResolvingBot:
        async def get_chat(self, identifier: str) -> SimpleNamespace:
            assert identifier == "@new_admin"
            return SimpleNamespace(id=777, type=admin_commands.ChatType.PRIVATE)

    context = _build_context(bot_modules, ResolvingBot())
    message = DummyMessage("@new_admin")
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))

    async def invoke() -> None:
        result = await admin_commands.receive_admin_id(update, context)
        assert result is admin_commands.ConversationHandler.END

    asyncio.run(invoke())

    assert message.replies == [
        admin_commands.get_message("admin.add_success", user_id=777)
    ]
    assert 777 in admin_module.get_admins(context)


def test_receive_admin_id_reports_unknown_username(bot_modules) -> None:
    admin_commands = bot_modules.admin_commands
    admin_module = bot_modules.admin

    class FailingBot:
        async def get_chat(self, identifier: str) -> SimpleNamespace:
            raise admin_commands.BadRequest("Chat not found")

    context = _build_context(bot_modules, FailingBot())
    message = DummyMessage("@missing_user")
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))

    async def invoke() -> None:
        result = await admin_commands.receive_admin_id(update, context)
        assert result == admin_commands.ADMIN_ADD_ADMIN_WAIT_ID

    asyncio.run(invoke())

    assert message.replies == [
        admin_commands.get_message(
            "admin.user_lookup_failed", identifier="@missing_user"
        )
    ]
    assert admin_module.get_admins(context) == set()


def test_receive_admin_id_reports_lookup_failure(bot_modules) -> None:
    admin_commands = bot_modules.admin_commands
    admin_module = bot_modules.admin

    class ErrorBot:
        async def get_chat(self, identifier: str) -> SimpleNamespace:
            raise admin_commands.TelegramError("Gateway Timeout")

    context = _build_context(bot_modules, ErrorBot())
    message = DummyMessage("@flaky_user")
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))

    async def invoke() -> None:
        result = await admin_commands.receive_admin_id(update, context)
        assert result == admin_commands.ADMIN_ADD_ADMIN_WAIT_ID

    asyncio.run(invoke())

    assert message.replies == [admin_commands.get_message("admin.user_lookup_error")]
    assert admin_module.get_admins(context) == set()
