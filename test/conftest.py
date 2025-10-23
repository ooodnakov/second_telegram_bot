from __future__ import annotations

import enum
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from pytest import MonkeyPatch


@pytest.fixture(scope="session")
def stub_external_modules() -> Iterator[None]:
    monkeypatch = MonkeyPatch()

    if "telegram" not in sys.modules:
        telegram_module = types.ModuleType("telegram")
        telegram_module.Bot = type("Bot", (), {})
        telegram_module.Update = type("Update", (), {})

        class _InlineKeyboardButton:
            def __init__(
                self, text: str | None = None, callback_data: str | None = None
            ):
                self.text = text
                self.callback_data = callback_data

        class _InlineKeyboardMarkup:
            def __init__(self, inline_keyboard: list[list[object]] | None = None):
                self.inline_keyboard = inline_keyboard or []

        class _InputMediaPhoto:
            def __init__(
                self,
                media: object | None = None,
                *,
                caption: str | None = None,
                parse_mode: str | None = None,
                **kwargs,
            ):
                self.media = media
                self.caption = caption
                self.parse_mode = parse_mode
                self.extra = kwargs

        telegram_module.InlineKeyboardButton = _InlineKeyboardButton
        telegram_module.InlineKeyboardMarkup = _InlineKeyboardMarkup
        telegram_module.InputMediaPhoto = _InputMediaPhoto
        monkeypatch.setitem(sys.modules, "telegram", telegram_module)

        telegram_error_module = types.ModuleType("telegram.error")
        telegram_error_module.BadRequest = type("BadRequest", (Exception,), {})
        telegram_error_module.TelegramError = type("TelegramError", (Exception,), {})
        monkeypatch.setitem(sys.modules, "telegram.error", telegram_error_module)

        telegram_constants_module = types.ModuleType("telegram.constants")
        telegram_constants_module.ChatType = enum.Enum(
            "ChatType", {"PRIVATE": "private", "GROUP": "group"}
        )
        monkeypatch.setitem(
            sys.modules, "telegram.constants", telegram_constants_module
        )

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

    if "minio" not in sys.modules:
        import io

        minio_module = types.ModuleType("minio")
        minio_error_module = types.ModuleType("minio.error")
        minio_error_module.S3Error = type("S3Error", (Exception,), {})

        class _DummyObject:
            def __init__(self, name: str) -> None:
                self.object_name = name

        class _DummyMinio:
            def __init__(self, *args, **kwargs) -> None:
                self._storage: dict[str, dict[str, bytes]] = {}

            def bucket_exists(self, bucket: str) -> bool:
                return bucket in self._storage

            def make_bucket(self, bucket: str) -> None:
                self._storage.setdefault(bucket, {})

            def fput_object(
                self, bucket: str, object_name: str, file_path: str
            ) -> None:
                self._storage.setdefault(bucket, {})
                data = Path(file_path).read_bytes()
                self._storage[bucket][object_name] = data

            def fget_object(
                self, bucket: str, object_name: str, file_path: str
            ) -> None:
                data = self._storage.get(bucket, {}).get(object_name)
                if data is None:
                    raise minio_error_module.S3Error("missing")
                target = Path(file_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

            def list_objects(
                self, bucket: str, prefix: str = "", recursive: bool = False
            ):
                for name in sorted(self._storage.get(bucket, {})):
                    if name.startswith(prefix):
                        yield _DummyObject(name)

            def get_object(self, bucket: str, object_name: str):
                data = self._storage.get(bucket, {}).get(object_name)
                if data is None:
                    raise minio_error_module.S3Error("missing")
                return io.BytesIO(data)

        minio_module.Minio = _DummyMinio
        monkeypatch.setitem(sys.modules, "minio", minio_module)
        monkeypatch.setitem(sys.modules, "minio.error", minio_error_module)

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
    media_storage_module = importlib.reload(
        importlib.import_module("bot.media_storage")
    )
    config_module = importlib.reload(importlib.import_module("bot.config"))
    workflow_module = importlib.reload(importlib.import_module("bot.workflow"))
    admin_module = importlib.reload(importlib.import_module("bot.admin"))
    admin_commands_module = importlib.reload(
        importlib.import_module("bot.admin_commands")
    )
    editing_module = importlib.reload(importlib.import_module("bot.editing"))
    constants_module = importlib.reload(importlib.import_module("bot.constants"))
    commands_module = importlib.reload(importlib.import_module("bot.commands"))
    return SimpleNamespace(
        logging=logging_module,
        config=config_module,
        storage=storage_module,
        media_storage=media_storage_module,
        workflow=workflow_module,
        admin=admin_module,
        admin_commands=admin_commands_module,
        editing=editing_module,
        constants=constants_module,
        commands=commands_module,
    )
