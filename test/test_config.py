from __future__ import annotations

from pathlib import Path

from .utils import capture_logs, extract_messages


def test_load_config_logs_and_parses(tmp_path: Path, bot_modules) -> None:
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

    with capture_logs(bot_modules.logging.logger, level="INFO") as events:
        config = bot_modules.config.load_config(config_path)

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


def test_create_valkey_client_success(monkeypatch, bot_modules) -> None:
    created: dict[str, object] = {}

    class DummyValkey:
        def __init__(self, host: str, port: int, password: str | None) -> None:
            created["args"] = (host, port, password)

        def ping(self) -> None:
            created["pinged"] = True

    monkeypatch.setattr(bot_modules.config, "Valkey", DummyValkey)

    settings = {"valkey": {"host": "demo", "port": 6380, "password": "pass"}}

    client = bot_modules.config.create_valkey_client(settings)

    assert isinstance(client, DummyValkey)
    assert created["args"] == ("demo", 6380, "pass")
    assert created.get("pinged") is True


def test_create_valkey_client_falls_back_on_connection_error(
    monkeypatch, bot_modules
) -> None:
    class FailingValkey:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def ping(self) -> None:
            raise bot_modules.config.ValkeyConnectionError("boom")

    monkeypatch.setattr(bot_modules.config, "Valkey", FailingValkey)

    settings = {"valkey": {"host": "demo", "port": 6380, "password": "pass"}}

    client = bot_modules.config.create_valkey_client(settings)

    assert isinstance(client, bot_modules.storage.InMemoryValkey)
