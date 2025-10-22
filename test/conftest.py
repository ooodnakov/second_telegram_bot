from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace
from typing import Iterator

import pytest
from pytest import MonkeyPatch


@pytest.fixture(scope="session")
def stub_external_modules() -> Iterator[None]:
    monkeypatch = MonkeyPatch()

    if "telegram" not in sys.modules:
        telegram_module = types.ModuleType("telegram")
        for name in (
            "Bot",
            "Update",
            "InlineKeyboardButton",
            "InlineKeyboardMarkup",
            "InputMediaPhoto",
        ):
            setattr(telegram_module, name, type(name, (), {}))
        monkeypatch.setitem(sys.modules, "telegram", telegram_module)

        telegram_error_module = types.ModuleType("telegram.error")
        telegram_error_module.BadRequest = type("BadRequest", (Exception,), {})
        telegram_error_module.TelegramError = type("TelegramError", (Exception,), {})
        monkeypatch.setitem(sys.modules, "telegram.error", telegram_error_module)

    if "telegram.ext" not in sys.modules:
        ext_module = types.ModuleType("telegram.ext")

        class _ApplicationBuilder:
            def token(self, _token: str) -> "_ApplicationBuilder":
                return self

            def build(self) -> SimpleNamespace:
                return SimpleNamespace(
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
        ext_module.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object())
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
        base = type("ValkeyError", (Exception,), {})
        valkey_exceptions.ValkeyError = base
        valkey_exceptions.ConnectionError = type("ConnectionError", (base,), {})
        valkey_exceptions.TimeoutError = type("TimeoutError", (base,), {})
        valkey_exceptions.ResponseError = type("ResponseError", (base,), {})
        monkeypatch.setitem(sys.modules, "valkey.exceptions", valkey_exceptions)

    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.fixture(scope="module")
def bot_modules(stub_external_modules: None) -> SimpleNamespace:
    logging_module = importlib.reload(importlib.import_module("bot.logging"))
    storage_module = importlib.reload(importlib.import_module("bot.storage"))
    config_module = importlib.reload(importlib.import_module("bot.config"))
    workflow_module = importlib.reload(importlib.import_module("bot.workflow"))
    admin_module = importlib.reload(importlib.import_module("bot.admin"))
    admin_commands_module = importlib.reload(
        importlib.import_module("bot.admin_commands")
    )
    constants_module = importlib.reload(importlib.import_module("bot.constants"))
    return SimpleNamespace(
        logging=logging_module,
        config=config_module,
        storage=storage_module,
        workflow=workflow_module,
        admin=admin_module,
        admin_commands=admin_commands_module,
        constants=constants_module,
    )
