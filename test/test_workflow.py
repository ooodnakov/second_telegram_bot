from __future__ import annotations

import asyncio
import types
from pathlib import Path


def test_get_contacts_persists_submission(tmp_path: Path, bot_modules) -> None:
    workflow = bot_modules.workflow
    storage = bot_modules.storage
    client = storage.InMemoryValkey()
    prefix = "test"
    user_id = 99
    session_key = "session-1"

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    photo_path = session_dir / "photo.jpg"
    photo_path.write_text("data", encoding="utf-8")

    initial_data = {
        "session_key": session_key,
        "session_dir": session_dir,
        "photos": [photo_path],
        "position": "Coat",
        "condition": "Used",
        "size": "M",
        "material": "Wool",
        "description": "Warm coat",
        "price": "1000",
    }

    store = storage.ApplicationStore(client, prefix=prefix)
    store.init_session(user_id, initial_data)

    class DummyMessage:
        def __init__(self, text: str) -> None:
            self.text = text
            self.replies: list[tuple[str, str | None]] = []

        async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
            self.replies.append((text, parse_mode))

    class DummyBot:
        async def send_message(
            self, chat_id: int, text: str, parse_mode: str | None = None
        ) -> None:
            pass

        async def send_media_group(self, chat_id: int, media: list[object]) -> None:
            pass

        async def send_photo(self, chat_id: int, photo: object) -> None:
            pass

    user = types.SimpleNamespace(
        id=user_id,
        username="tester",
        first_name="Test",
        last_name="User",
    )
    message = DummyMessage("@seller")
    chat = types.SimpleNamespace(id=user_id)
    update = types.SimpleNamespace(
        message=message,
        effective_user=user,
        effective_chat=chat,
    )

    media_storage = bot_modules.media_storage.LocalMediaStorage(tmp_path / "media")

    bot_data = {
        "valkey_client": client,
        "valkey_prefix": prefix,
        "moderator_chat_ids": [],
        "media_storage": media_storage,
    }

    context = types.SimpleNamespace(
        application=types.SimpleNamespace(bot_data=bot_data),
        bot=DummyBot(),
    )

    async def run_workflow() -> None:
        result = await workflow.get_contacts(update, context)
        assert result is workflow.ConversationHandler.END

    asyncio.run(run_workflow())

    assert len(message.replies) == 2
    summary_text, summary_parse_mode = message.replies[0]
    assert summary_parse_mode == "Markdown"
    assert summary_text.startswith(workflow.get_message("workflow.summary_header"))
    assert (
        workflow.get_message("workflow.summary_contacts", value="@seller")
        in summary_text
    )

    acknowledgement_text, acknowledgement_parse_mode = message.replies[1]
    assert acknowledgement_parse_mode == "Markdown"
    assert acknowledgement_text == workflow.get_message("workflow.submission_received")

    record = client.hgetall(f"{prefix}:{session_key}")
    assert record["contacts"] == "@seller"
    assert record["user_id"] == str(user_id)
    assert f"{prefix}:{session_key}" in client.smembers(
        f"{prefix}:user:{user_id}:applications"
    )
    assert f"{prefix}:{session_key}" in client.smembers(f"{prefix}:applications")
    assert client.hgetall(f"{prefix}:session:{user_id}") == {}


def test_get_contacts_without_session_sends_warning(tmp_path: Path, bot_modules) -> None:
    workflow = bot_modules.workflow
    storage = bot_modules.storage
    client = storage.InMemoryValkey()

    class DummyMessage:
        def __init__(self, text: str) -> None:
            self.text = text
            self.replies: list[str] = []

        async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
            self.replies.append(text)

    class DummyBot:
        async def send_message(
            self, chat_id: int, text: str, parse_mode: str | None = None
        ) -> None:
            pass

        async def send_media_group(self, chat_id: int, media: list[object]) -> None:
            pass

        async def send_photo(self, chat_id: int, photo: object) -> None:
            pass

    user = types.SimpleNamespace(id=5)
    message = DummyMessage("@seller")
    update = types.SimpleNamespace(
        message=message, effective_user=user, effective_chat=None
    )

    media_storage = bot_modules.media_storage.LocalMediaStorage(tmp_path / "media")

    bot_data = {
        "valkey_client": client,
        "valkey_prefix": "test",
        "moderator_chat_ids": [],
        "media_storage": media_storage,
    }

    context = types.SimpleNamespace(
        application=types.SimpleNamespace(bot_data=bot_data),
        bot=DummyBot(),
    )

    async def run_workflow() -> None:
        result = await workflow.get_contacts(update, context)
        assert result is workflow.ConversationHandler.END

    asyncio.run(run_workflow())

    assert message.replies == [workflow.get_message("general.session_missing")]
