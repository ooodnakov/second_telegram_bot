from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace


class DummyMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
        self.replies.append(text)


class RecordingBot:
    def __init__(self) -> None:
        self.sent_photos: list[SimpleNamespace] = []
        self.edited_media: list[SimpleNamespace] = []
        self._next_message_id = 100

    async def send_photo(
        self,
        *,
        chat_id: int,
        photo,
        caption: str,
        parse_mode: str,
        reply_markup,
    ) -> SimpleNamespace:
        message_id = self._next_message_id
        self._next_message_id += 1
        self.sent_photos.append(
            SimpleNamespace(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        )
        return SimpleNamespace(message_id=message_id)

    async def edit_message_media(
        self,
        *,
        chat_id: int,
        message_id: int,
        media,
        reply_markup,
    ) -> None:
        self.edited_media.append(
            SimpleNamespace(
                chat_id=chat_id,
                message_id=message_id,
                media=media,
                reply_markup=reply_markup,
            )
        )


class DummyQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=42)
        self.answers: list[tuple[str | None, bool | None]] = []

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        self.answers.append((text, show_alert))


@contextmanager
def _patched_telegram_types(admin_commands) -> None:
    original_button = admin_commands.InlineKeyboardButton
    original_markup = admin_commands.InlineKeyboardMarkup
    original_media = admin_commands.InputMediaPhoto

    class _Button:
        def __init__(self, text: str, callback_data: str) -> None:
            self.text = text
            self.callback_data = callback_data

    class _Markup:
        def __init__(self, inline_keyboard) -> None:
            self.inline_keyboard = inline_keyboard

    class _Media:
        def __init__(self, *, media, caption: str, parse_mode: str) -> None:
            self.media = media
            self.caption = caption
            self.parse_mode = parse_mode

    admin_commands.InlineKeyboardButton = _Button  # type: ignore[assignment]
    admin_commands.InlineKeyboardMarkup = _Markup  # type: ignore[assignment]
    admin_commands.InputMediaPhoto = _Media  # type: ignore[assignment]
    try:
        yield
    finally:
        admin_commands.InlineKeyboardButton = original_button  # type: ignore[assignment]
        admin_commands.InlineKeyboardMarkup = original_markup  # type: ignore[assignment]
        admin_commands.InputMediaPhoto = original_media  # type: ignore[assignment]


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

    with _patched_telegram_types(admin_commands):
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

    with _patched_telegram_types(admin_commands):
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


def _build_submission_with_photos(tmp_path, session_key: str) -> dict[str, str]:
    photo_one = tmp_path / "one.jpg"
    photo_two = tmp_path / "two.jpg"
    photo_one.write_bytes(b"photo-one")
    photo_two.write_bytes(b"photo-two")
    return {
        "session_key": session_key,
        "user_id": "123",
        "created_at": "2024-01-01T00:00:00+00:00",
        "position": "Jacket",
        "condition": "Used",
        "size": "M",
        "material": "Cotton",
        "description": "Warm jacket",
        "price": "1000",
        "contacts": "@seller",
        "photos": f"{photo_one},{photo_two}",
    }


def test_admin_photo_navigation_advances_photo(bot_modules, tmp_path) -> None:
    admin_commands = bot_modules.admin_commands
    session_key = "session-advance"
    submission = _build_submission_with_photos(tmp_path, session_key)
    state = admin_commands._build_view_state([submission])
    state["chat_id"] = 555
    bot = RecordingBot()
    context = _build_context(bot_modules, bot)
    context.user_data[admin_commands.ADMIN_VIEW_STATE_KEY] = state

    async def invoke() -> None:
        await admin_commands._render_admin_application(context, state)

        assert state["message_id"] is not None
        assert len(bot.sent_photos) == 1
        assert state["photo_indexes"][session_key] == 0

        query = DummyQuery(f"admin_app_photo_next:{session_key}")
        update = SimpleNamespace(callback_query=query)

        await admin_commands.navigate_application_photo_next(update, context)

        assert state["photo_indexes"][session_key] == 1
        assert query.answers
        assert len(bot.edited_media) == 1

        edit_call = bot.edited_media[0]
        assert edit_call.media.media.name.endswith("two.jpg")
        assert getattr(edit_call.media.media, "closed", False)

        expected_counter = admin_commands.get_message(
            "admin.photo_counter", current=2, total=2
        )
        assert f"<b>Фото:</b> {expected_counter}" in edit_call.media.caption

        markup = edit_call.reply_markup
        inline_keyboard = getattr(markup, "inline_keyboard", None)
        assert inline_keyboard is not None
        photo_buttons = [
            button
            for row in inline_keyboard
            for button in row
            if getattr(button, "callback_data", "").startswith("admin_app_photo_")
        ]
        assert {button.callback_data for button in photo_buttons} == {
            f"admin_app_photo_prev:{session_key}",
            f"admin_app_photo_next:{session_key}",
        }

    asyncio.run(invoke())


def test_admin_photo_navigation_wraps_previous(bot_modules, tmp_path) -> None:
    admin_commands = bot_modules.admin_commands
    session_key = "session-wrap"
    submission = _build_submission_with_photos(tmp_path, session_key)
    state = admin_commands._build_view_state([submission])
    state["chat_id"] = 777
    bot = RecordingBot()
    context = _build_context(bot_modules, bot)
    context.user_data[admin_commands.ADMIN_VIEW_STATE_KEY] = state

    async def invoke() -> None:
        await admin_commands._render_admin_application(context, state)

        assert state["photo_indexes"][session_key] == 0

        query = DummyQuery(f"admin_app_photo_prev:{session_key}")
        update = SimpleNamespace(callback_query=query)

        await admin_commands.navigate_application_photo_prev(update, context)

        assert state["photo_indexes"][session_key] == 1
        assert query.answers
        assert len(bot.edited_media) == 1
        edit_call = bot.edited_media[0]
        assert edit_call.media.media.name.endswith("two.jpg")
        expected_counter = admin_commands.get_message(
            "admin.photo_counter", current=2, total=2
        )
        assert f"<b>Фото:</b> {expected_counter}" in edit_call.media.caption

    asyncio.run(invoke())
