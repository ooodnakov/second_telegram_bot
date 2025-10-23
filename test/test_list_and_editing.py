import asyncio
from pathlib import Path
from types import SimpleNamespace


class DummyBot:
    def __init__(self, file_dir: Path) -> None:
        self.file_dir = file_dir
        self.edited_messages: list[tuple[int, int, str]] = []
        self.sent_photos: list[tuple[int, Path]] = []
        self.file_requests: list[str] = []

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup=None
    ) -> None:
        self.edited_messages.append((chat_id, message_id, text))

    async def send_photo(self, chat_id: int, photo) -> None:  # pragma: no cover - IO
        path = Path(getattr(photo, "name", "photo"))
        self.sent_photos.append((chat_id, path))

    async def send_media_group(
        self, chat_id: int, media: list
    ) -> None:  # pragma: no cover
        self.sent_photos.append((chat_id, Path("group")))

    async def get_file(self, file_id: str):  # pragma: no cover - network
        self.file_requests.append(file_id)

        class _TelegramFile:
            def __init__(self, directory: Path, identifier: str) -> None:
                self.directory = directory
                self.identifier = identifier
                self.file_path = str(directory / f"{identifier}.jpg")

            async def download_to_drive(self, custom_path: str) -> None:
                Path(custom_path).write_bytes(b"data")

        return _TelegramFile(self.file_dir, file_id)


class DummyMessage:
    def __init__(self, chat_id: int, message_id: int = 1) -> None:
        self.chat_id = chat_id
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id)
        self.replies: list[tuple[str, object | None]] = []

    async def reply_text(self, text: str, reply_markup=None) -> None:
        self.replies.append((text, reply_markup))


class DummyCallbackQuery:
    def __init__(self, data: str, user_id: int, message: DummyMessage) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[tuple[str, object | None]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))


class DummyUserMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []
        self.photo: list[SimpleNamespace] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def _build_context(bot_modules, bot, storage) -> tuple[object, SimpleNamespace]:
    client = bot_modules.storage.InMemoryValkey()
    bot_data = {
        "valkey_client": client,
        "valkey_prefix": "testbot",
        "moderator_chat_ids": [],
        "media_storage": storage,
    }
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=bot,
        user_data={},
    )
    return client, context


def _make_local_storage(bot_modules, tmp_path):
    return bot_modules.media_storage.LocalMediaStorage(tmp_path / "media")


def _make_minio_storage(bot_modules, tmp_path):
    client = bot_modules.media_storage.Minio(
        "minio", access_key=None, secret_key=None, secure=False
    )
    storage = bot_modules.media_storage.MinioMediaStorage(
        client,
        bucket="test-bucket",
        cache_dir=tmp_path / "cache",
    )
    return client, storage


def _create_submission(
    client,
    prefix: str,
    session_key: str,
    user_id: int,
    storage,
    *,
    filename: str = "photo.jpg",
    position: str = "Coat",
    payload: bytes = b"photo",
) -> str:
    session = storage.get_session(session_key)
    target_path = storage.allocate_path(session, filename)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(payload)
    handle = storage.finalize_upload(session, target_path)
    record = {
        "session_key": session_key,
        "user_id": str(user_id),
        "position": position,
        "condition": "Used",
        "description": "Warm",
        "created_at": "2023-01-01T00:00:00",
        "photos": handle,
        "session_dir": str(session.directory or ""),
    }
    key = f"{prefix}:{session_key}"
    client.hset(key, mapping=record)
    client.sadd(f"{prefix}:applications", key)
    return handle


def test_list_applications_renders_grid(tmp_path, bot_modules):
    async def run() -> None:
        commands = bot_modules.commands
        bot = DummyBot(tmp_path)
        storage = _make_local_storage(bot_modules, tmp_path)
        client, context = _build_context(bot_modules, bot, storage)

        _create_submission(client, "testbot", "s1", 100, storage)

        message = DummyMessage(chat_id=100)
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=100),
        )

        result = await commands.list_applications(update, context)
        assert result is commands.ConversationHandler.END

        assert message.replies
        text_value, markup = message.replies[0]
        assert text_value == commands.get_message("list.instructions")
        assert markup is not None
        assert markup.inline_keyboard
        assert markup.inline_keyboard[0][0].callback_data.startswith("list:view:")

    asyncio.run(run())


def test_list_applications_renders_grid_minio(tmp_path, bot_modules):
    async def run() -> None:
        commands = bot_modules.commands
        bot = DummyBot(tmp_path)
        _minio_client, storage = _make_minio_storage(bot_modules, tmp_path)
        client, context = _build_context(bot_modules, bot, storage)

        _create_submission(client, "testbot", "s1", 100, storage)

        message = DummyMessage(chat_id=100)
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=100),
        )

        result = await commands.list_applications(update, context)
        assert result is commands.ConversationHandler.END

        assert message.replies
        text_value, markup = message.replies[0]
        assert text_value == commands.get_message("list.instructions")
        assert markup is not None
        assert markup.inline_keyboard
        assert markup.inline_keyboard[0][0].callback_data.startswith("list:view:")
        assert (tmp_path / "cache" / "s1" / "photo.jpg").exists()

    asyncio.run(run())


def test_paginate_list_updates_message(tmp_path, bot_modules):
    async def run() -> None:
        commands = bot_modules.commands
        bot = DummyBot(tmp_path)
        storage = bot_modules.media_storage.LocalMediaStorage(tmp_path / "media")
        client, context = _build_context(bot_modules, bot, storage)

        for idx in range(2):
            session = f"s{idx + 1}"
            _create_submission(
                client,
                "testbot",
                session,
                100,
                storage,
                position=f"Item {idx}",
            )

        message = DummyMessage(chat_id=100)
        await commands.list_applications(
            SimpleNamespace(message=message, effective_user=SimpleNamespace(id=100)),
            context,
        )

        callback_message = DummyMessage(chat_id=100, message_id=10)
        query = DummyCallbackQuery("list:page:0:100", 100, callback_message)
        await commands.paginate_list(SimpleNamespace(callback_query=query), context)

        assert query.edits
        text, markup = query.edits[0]
        assert text == commands.get_message("list.instructions")
        assert markup is not None

    asyncio.run(run())


def test_show_application_detail_sends_photos(tmp_path, bot_modules):
    async def run() -> None:
        commands = bot_modules.commands
        bot = DummyBot(tmp_path)
        storage = bot_modules.media_storage.LocalMediaStorage(tmp_path / "media")
        client, context = _build_context(bot_modules, bot, storage)

        _create_submission(client, "testbot", "session", 100, storage)

        list_message = DummyMessage(chat_id=100)
        await commands.list_applications(
            SimpleNamespace(
                message=list_message, effective_user=SimpleNamespace(id=100)
            ),
            context,
        )

        callback_message = DummyMessage(chat_id=100, message_id=55)
        query = DummyCallbackQuery("list:view:session:0:100", 100, callback_message)
        await commands.show_application_detail(
            SimpleNamespace(callback_query=query), context
        )

        assert query.edits
        detail_text, markup = query.edits[0]
        assert "Заявка:" in detail_text
        assert markup is not None
        assert any(
            button.callback_data.startswith("edit:")
            for row in markup.inline_keyboard
            for button in row
        )
        assert bot.sent_photos

    asyncio.run(run())


async def _prepare_detail_view(tmp_path, bot_modules):
    commands = bot_modules.commands
    bot = DummyBot(tmp_path)
    storage = bot_modules.media_storage.LocalMediaStorage(tmp_path / "media")
    client, context = _build_context(bot_modules, bot, storage)

    _create_submission(client, "testbot", "session", 100, storage)

    await commands.list_applications(
        SimpleNamespace(
            message=DummyMessage(chat_id=100),
            effective_user=SimpleNamespace(id=100),
        ),
        context,
    )
    detail_query = DummyCallbackQuery(
        "list:view:session:0:100", 100, DummyMessage(100, 5)
    )
    await commands.show_application_detail(
        SimpleNamespace(callback_query=detail_query), context
    )
    return commands, bot_modules.editing, bot_modules.constants, bot, client, context


def test_edit_position_updates_record(tmp_path, bot_modules):
    async def run() -> None:
        (
            commands,
            editing,
            constants,
            bot,
            client,
            context,
        ) = await _prepare_detail_view(tmp_path, bot_modules)

        start_query = DummyCallbackQuery(
            "edit:position:session", 100, DummyMessage(100)
        )
        state = await editing.start_edit_position(
            SimpleNamespace(callback_query=start_query), context
        )
        assert state == constants.EDIT_POSITION
        assert start_query.message.replies

        user_message = DummyUserMessage("Новая позиция")
        result = await editing.receive_position(
            SimpleNamespace(
                message=user_message, effective_user=SimpleNamespace(id=100)
            ),
            context,
        )
        assert result is commands.ConversationHandler.END
        assert user_message.replies == [commands.get_message("edit.position_saved")]

        record = client.hgetall("testbot:session")
        assert record["position"] == "Новая позиция"
        assert bot.edited_messages

    asyncio.run(run())


def test_edit_description_updates_record(tmp_path, bot_modules):
    async def run() -> None:
        (
            commands,
            editing,
            constants,
            bot,
            client,
            context,
        ) = await _prepare_detail_view(tmp_path, bot_modules)

        start_query = DummyCallbackQuery(
            "edit:description:session", 100, DummyMessage(100)
        )
        state = await editing.start_edit_description(
            SimpleNamespace(callback_query=start_query), context
        )
        assert state == constants.EDIT_DESCRIPTION

        user_message = DummyUserMessage("Новое описание")
        result = await editing.receive_description(
            SimpleNamespace(
                message=user_message, effective_user=SimpleNamespace(id=100)
            ),
            context,
        )
        assert result is commands.ConversationHandler.END
        assert user_message.replies == [commands.get_message("edit.description_saved")]

        record = client.hgetall("testbot:session")
        assert record["description"] == "Новое описание"
        assert bot.edited_messages

    asyncio.run(run())


def test_edit_condition_updates_record(tmp_path, bot_modules):
    async def run() -> None:
        (
            commands,
            editing,
            constants,
            bot,
            client,
            context,
        ) = await _prepare_detail_view(tmp_path, bot_modules)

        start_query = DummyCallbackQuery(
            "edit:condition:session", 100, DummyMessage(100)
        )
        state = await editing.start_edit_condition(
            SimpleNamespace(callback_query=start_query), context
        )
        assert state == constants.EDIT_CONDITION

        choice_query = DummyCallbackQuery(
            "edit_condition:set:session:new",
            100,
            DummyMessage(100, 6),
        )
        result = await editing.receive_condition_choice(
            SimpleNamespace(callback_query=choice_query), context
        )
        assert result is commands.ConversationHandler.END
        assert choice_query.answers[-1] == (None, False)

        record = client.hgetall("testbot:session")
        assert record["condition"] == commands.get_message("workflow.condition_new")
        assert bot.edited_messages

    asyncio.run(run())


def test_start_edit_photos_prompts_with_skip_keyword(tmp_path, bot_modules):
    async def run() -> None:
        (
            commands,
            editing,
            constants,
            _bot,
            _client,
            context,
        ) = await _prepare_detail_view(tmp_path, bot_modules)

        start_message = DummyMessage(100)
        start_query = DummyCallbackQuery("edit:photos:session", 100, start_message)
        state = await editing.start_edit_photos(
            SimpleNamespace(callback_query=start_query), context
        )

        assert state == constants.EDIT_PHOTOS
        assert start_message.replies
        text, markup = start_message.replies[0]
        assert markup is None
        assert text == commands.get_message(
            "edit.photos_prompt", keyword=constants.SKIP_KEYWORD
        )

    asyncio.run(run())


def test_edit_photos_replaces_media(tmp_path, bot_modules):
    async def run() -> None:
        (
            commands,
            editing,
            constants,
            bot,
            client,
            context,
        ) = await _prepare_detail_view(tmp_path, bot_modules)

        start_query = DummyCallbackQuery("edit:photos:session", 100, DummyMessage(100))
        state = await editing.start_edit_photos(
            SimpleNamespace(callback_query=start_query), context
        )
        assert state == constants.EDIT_PHOTOS

        photo_message = DummyUserMessage()
        photo_message.photo = [SimpleNamespace(file_id="file-1")]
        result = await editing.receive_photo_upload(
            SimpleNamespace(
                message=photo_message, effective_user=SimpleNamespace(id=100)
            ),
            context,
        )
        assert result == constants.EDIT_PHOTOS
        assert bot.file_requests == ["file-1"]

        skip_message = DummyUserMessage(bot_modules.constants.SKIP_KEYWORD)
        result = await editing.finalize_photo_upload(
            SimpleNamespace(
                message=skip_message, effective_user=SimpleNamespace(id=100)
            ),
            context,
        )
        assert result is commands.ConversationHandler.END
        assert skip_message.replies == [commands.get_message("edit.photos_saved")]

        record = client.hgetall("testbot:session")
        assert "update_01" in record["photos"]
        assert bot.edited_messages
        assert bot.sent_photos

    asyncio.run(run())
