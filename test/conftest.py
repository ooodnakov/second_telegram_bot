from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from pytest import MonkeyPatch


@pytest.fixture(scope="session")
def stub_external_modules() -> Iterator[None]:
    monkeypatch = MonkeyPatch()

    if "telegram" not in sys.modules:
        try:
            importlib.import_module("telegram")
        except ModuleNotFoundError:
            telegram_module = types.ModuleType("telegram")
            telegram_module.Bot = MagicMock(name="Bot")
            telegram_module.Update = MagicMock(name="Update")
            telegram_module.InlineKeyboardButton = MagicMock(
                name="InlineKeyboardButton"
            )
            telegram_module.InlineKeyboardMarkup = MagicMock(
                name="InlineKeyboardMarkup"
            )
            telegram_module.InputMediaPhoto = MagicMock(name="InputMediaPhoto")
            monkeypatch.setitem(sys.modules, "telegram", telegram_module)

            telegram_error_module = types.ModuleType("telegram.error")
            telegram_error_module.BadRequest = ValueError
            telegram_error_module.TelegramError = RuntimeError
            monkeypatch.setitem(sys.modules, "telegram.error", telegram_error_module)

            telegram_constants_module = types.ModuleType("telegram.constants")
            telegram_constants_module.ChatType = SimpleNamespace(
                PRIVATE="private", GROUP="group"
            )
            monkeypatch.setitem(
                sys.modules, "telegram.constants", telegram_constants_module
            )

    if "telegram.ext" not in sys.modules:
        try:
            importlib.import_module("telegram.ext")
        except ModuleNotFoundError:
            ext_module = types.ModuleType("telegram.ext")

            app_builder = MagicMock(name="ApplicationBuilder()")
            app_builder.token.return_value = app_builder
            app_builder.build.return_value = SimpleNamespace(
                bot_data={},
                add_handler=MagicMock(name="add_handler"),
                add_error_handler=MagicMock(name="add_error_handler"),
                run_polling=MagicMock(name="run_polling"),
            )

            conversation_handler = MagicMock(name="ConversationHandler")
            conversation_handler.END = object()

            filters_mock = MagicMock(name="filters")
            filters_mock.TEXT = MagicMock(name="filters.TEXT")
            filters_mock.COMMAND = MagicMock(name="filters.COMMAND")
            filters_mock.PHOTO = MagicMock(name="filters.PHOTO")
            filters_mock.Regex = MagicMock(name="filters.Regex")

            ext_module.ApplicationBuilder = MagicMock(
                name="ApplicationBuilder", return_value=app_builder
            )
            ext_module.CommandHandler = MagicMock(name="CommandHandler")
            ext_module.MessageHandler = MagicMock(name="MessageHandler")
            ext_module.CallbackQueryHandler = MagicMock(name="CallbackQueryHandler")
            ext_module.ConversationHandler = conversation_handler
            ext_module.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object())
            ext_module.filters = filters_mock
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
