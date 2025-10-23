from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from .utils import capture_logs, extract_messages


def test_application_store_emits_logging(tmp_path: Path, bot_modules) -> None:
    store = bot_modules.storage.ApplicationStore(
        bot_modules.storage.InMemoryValkey(), prefix="test"
    )
    photo_path = tmp_path / "example.jpg"
    photo_path.write_text("content", encoding="utf-8")

    initial_data = {
        "session_key": "session-1",
        "session_dir": tmp_path,
        "photos": [],
    }

    with capture_logs(bot_modules.logging.logger, level="DEBUG") as events:
        store.init_session(42, initial_data)
        store.set_fields(42, position="Chair")
        photos = store.append_photo(42, photo_path)
        assert str(photo_path) in photos
        session = store.get(42)
        assert session["position"] == "Chair"
        assert session["photos"][0] == str(photo_path)
        store.clear(42)

    messages = extract_messages(events)
    assert any("Initialized session" in message for message in messages)
    assert any("Updated session" in message for message in messages)
    assert any("Appended photo" in message for message in messages)
    assert any("Loaded session" in message for message in messages)
    assert any("Cleared session" in message for message in messages)

    assert store.get(42) == {}


def test_get_application_store_requires_client(bot_modules) -> None:
    context = SimpleNamespace(application=SimpleNamespace(bot_data={}))

    with pytest.raises(RuntimeError):
        bot_modules.storage.get_application_store(context)


def test_get_application_store_uses_configured_prefix(bot_modules) -> None:
    client = bot_modules.storage.InMemoryValkey()
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"valkey_client": client, "valkey_prefix": "custom"}
        )
    )

    store = bot_modules.storage.get_application_store(context)
    store.set_fields(7, position="Tester")

    stored = client.hgetall("custom:session:7")
    assert stored["position"] == "Tester"
