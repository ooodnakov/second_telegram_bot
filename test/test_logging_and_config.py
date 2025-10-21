from __future__ import annotations

import importlib
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from pytest import MonkeyPatch


@pytest.fixture(autouse=True, scope="module")
def stub_external_modules() -> Iterator[None]:
    monkeypatch = MonkeyPatch()
    if "telegram" not in sys.modules:
        telegram_module = types.ModuleType("telegram")
        for name in (
            "Update",
            "InlineKeyboardButton",
            "InlineKeyboardMarkup",
            "InputMediaPhoto",
        ):
            setattr(telegram_module, name, type(name, (), {}))
        monkeypatch.setitem(sys.modules, "telegram", telegram_module)

        telegram_error_module = types.ModuleType("telegram.error")
        telegram_error_module.BadRequest = type("BadRequest", (Exception,), {})
        monkeypatch.setitem(sys.modules, "telegram.error", telegram_error_module)

    if "telegram.ext" not in sys.modules:
        ext_module = types.ModuleType("telegram.ext")

        class _ApplicationBuilder:
            def token(self, _token: str) -> "_ApplicationBuilder":
                return self

            def build(self) -> types.SimpleNamespace:
                return types.SimpleNamespace(
                    bot_data={},
                    add_handler=lambda *args, **kwargs: None,
                    add_error_handler=lambda *args, **kwargs: None,
                    run_polling=lambda: None,
                )

        class _ConversationHandler:
            END = object()

            def __init__(self, *args, **kwargs) -> None:  # noqa: D401 - dummy init
                """Placeholder initializer."""

        class _DummyHandler:
            def __init__(self, *args, **kwargs) -> None:  # noqa: D401 - dummy init
                """Placeholder initializer."""

        class _Filters:
            def __init__(self) -> None:
                self.TEXT = self
                self.COMMAND = self
                self.PHOTO = self

            def __and__(self, _other: object) -> "_Filters":
                return self

            def __or__(self, _other: object) -> "_Filters":
                return self

            def __invert__(self) -> "_Filters":
                return self

            def Regex(self, _pattern: str) -> "_Filters":
                return self

        ext_module.ApplicationBuilder = _ApplicationBuilder
        ext_module.CommandHandler = _DummyHandler
        ext_module.MessageHandler = _DummyHandler
        ext_module.CallbackQueryHandler = _DummyHandler
        ext_module.ConversationHandler = _ConversationHandler
        ext_module.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object())
        ext_module.filters = _Filters()
        monkeypatch.setitem(sys.modules, "telegram.ext", ext_module)

    if "valkey" not in sys.modules:
        valkey_module = types.ModuleType("valkey")

        class _DummyValkey:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def ping(self) -> None:
                return None

            def hset(self, *args, **kwargs) -> None:
                return None

            def hgetall(self, *args, **kwargs):  # noqa: ANN001 - compatible signature
                return {}

            def sadd(self, *args, **kwargs) -> None:
                return None

            def smembers(self, *args, **kwargs):  # noqa: ANN001
                return set()

            def delete(self, *args, **kwargs) -> None:
                return None

        valkey_module.Valkey = _DummyValkey
        monkeypatch.setitem(sys.modules, "valkey", valkey_module)

    if "valkey.exceptions" not in sys.modules:
        valkey_exceptions = types.ModuleType("valkey.exceptions")
        valkey_exceptions.ConnectionError = type("ConnectionError", (Exception,), {})
        monkeypatch.setitem(sys.modules, "valkey.exceptions", valkey_exceptions)

    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.fixture(scope="module")
def bot_main(stub_external_modules: None) -> types.ModuleType:
    module = importlib.import_module("bot.main")
    return importlib.reload(module)


@contextmanager
def capture_logs(logger: Any, level: str = "DEBUG") -> Iterator[list[object]]:
    events: list[object] = []

    def sink(message: object) -> None:
        events.append(message)

    handler_id = logger.add(sink, level=level)
    try:
        yield events
    finally:
        logger.remove(handler_id)


def extract_messages(events: list[object]) -> list[str]:
    messages: list[str] = []
    for event in events:
        try:
            # loguru Message objects expose the formatted message via record["message"].
            record = event.record  # type: ignore[attr-defined]
            messages.append(str(record["message"]))
        except AttributeError:
            messages.append(str(event))
    return messages


def test_load_config_logs_and_parses(
    tmp_path: Path, bot_main: types.ModuleType
) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        """
[telegram]
token = 123456:ABC
moderator_chat_ids = 123,456

[valkey]
valkey_host = localhost
valkey_port = 6379
valkey_pass = secret
redis_prefix = demo_prefix
""".strip()
    )

    with capture_logs(bot_main.logger, level="INFO") as events:
        config = bot_main.load_config(config_path)

    assert config["token"] == "123456:ABC"
    assert config["moderator_chat_ids"] == [123, 456]
    assert config["valkey"]["host"] == "localhost"
    assert config["valkey"]["port"] == 6379
    assert config["valkey"]["password"] == "secret"
    assert config["valkey"]["prefix"] == "demo_prefix"

    messages = extract_messages(events)
    assert any(
        "Configuration loaded for 2 moderators" in message for message in messages
    )
    assert any("Valkey host localhost:6379" in message for message in messages)


def test_application_store_emits_logging(
    tmp_path: Path, bot_main: types.ModuleType
) -> None:
    store = bot_main.ApplicationStore(bot_main.InMemoryValkey(), prefix="test")
    photo_path = tmp_path / "example.jpg"
    photo_path.write_text("content", encoding="utf-8")

    initial_data = {
        "session_key": "session-1",
        "session_dir": tmp_path,
        "photos": [],
    }

    with capture_logs(bot_main.logger, level="DEBUG") as events:
        store.init_session(42, initial_data)
        store.set_fields(42, position="Chair")
        photos = store.append_photo(42, photo_path)
        assert photo_path in photos
        session = store.get(42)
        assert session["position"] == "Chair"
        assert session["photos"][0] == photo_path
        store.clear(42)

    messages = extract_messages(events)
    assert any("Initialized session" in message for message in messages)
    assert any("Updated session" in message for message in messages)
    assert any("Appended photo" in message for message in messages)
    assert any("Loaded session" in message for message in messages)
    assert any("Cleared session" in message for message in messages)

    # After clearing, ensure the session data is removed.
    assert store.get(42) == {}
