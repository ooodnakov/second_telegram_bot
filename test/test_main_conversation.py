from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace


def test_new_command_allows_reentry(monkeypatch, stub_external_modules) -> None:
    if "telegram.request" not in sys.modules:
        request_module = types.ModuleType("telegram.request")

        class DummyHTTPXRequest:
            def __init__(self, *args, **kwargs) -> None:
                self.args = args
                self.kwargs = kwargs

        request_module.HTTPXRequest = DummyHTTPXRequest
        monkeypatch.setitem(sys.modules, "telegram.request", request_module)

    main = importlib.reload(importlib.import_module("bot.main"))

    created_handlers: list[object] = []

    class RecordingConversationHandler:
        END = object()

        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs
            created_handlers.append(self)

    class RecordingCommandHandler:
        def __init__(self, command, callback, **kwargs) -> None:
            self.command = command
            self.callback = callback
            self.kwargs = kwargs

    class DummyHandler:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class DummyBuilder:
        def token(self, _token: str) -> "DummyBuilder":
            return self

        def request(self, _request: object) -> "DummyBuilder":
            return self

        def build(self) -> SimpleNamespace:
            return SimpleNamespace(
                bot_data={},
                add_handler=lambda *args, **kwargs: None,
                add_error_handler=lambda *args, **kwargs: None,
                run_polling=lambda: None,
            )

    monkeypatch.setattr(main, "ConversationHandler", RecordingConversationHandler)
    monkeypatch.setattr(main, "CommandHandler", RecordingCommandHandler)
    monkeypatch.setattr(main, "MessageHandler", DummyHandler)
    monkeypatch.setattr(main, "CallbackQueryHandler", DummyHandler)
    monkeypatch.setattr(main, "ApplicationBuilder", DummyBuilder)
    monkeypatch.setattr(main, "create_valkey_client", lambda _config: object())
    monkeypatch.setattr(main, "create_media_storage", lambda _config=None: object())
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: {
            "token": "test-token",
            "valkey": {"prefix": "test-prefix"},
            "moderator_chat_ids": [],
            "super_admin_ids": [],
        },
    )

    main.main()

    new_handlers = [
        handler
        for handler in created_handlers
        if any(
            getattr(entry_point, "command", None) == "new"
            for entry_point in handler.kwargs.get("entry_points", [])
        )
    ]

    assert new_handlers
    assert new_handlers[0].kwargs.get("allow_reentry") is True
