"""Configuration helpers for the Telegram bot."""

from __future__ import annotations

import os
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from bot.logging import logger
from bot.storage import InMemoryValkey
from valkey import Valkey
from valkey.exceptions import (
    ConnectionError as ValkeyConnectionError,
)
from valkey.exceptions import (
    ResponseError as ValkeyResponseError,
)
from valkey.exceptions import (
    TimeoutError as ValkeyTimeoutError,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.ini"
CONFIG_SECTION = "telegram"
VALKEY_CONFIG_SECTION = "valkey"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load bot configuration from an ini file."""

    config_source = path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    config_path = Path(config_source).expanduser()
    logger.debug("Loading configuration from {}", config_path)

    parser = ConfigParser()
    if not parser.read(config_path, encoding="utf-8"):
        logger.error("Config file not found or unreadable: {}", config_path)
        raise RuntimeError(f"Config file not found or unreadable: {config_path}")

    if not parser.has_section(CONFIG_SECTION):
        logger.error(
            "Section '{}' missing in config file {}", CONFIG_SECTION, config_path
        )
        raise RuntimeError(f"Section '{CONFIG_SECTION}' missing in config file.")

    token = parser.get(CONFIG_SECTION, "token", fallback="").strip()
    if not token:
        logger.error("Telegram bot token is missing in the config file {}", config_path)
        raise RuntimeError("Telegram bot token is missing in the config file.")

    moderators: list[int] = []
    raw_moderators = parser.get(
        CONFIG_SECTION, "moderator_chat_ids", fallback=""
    ).strip()
    if raw_moderators:
        for raw_id in raw_moderators.replace("\n", ",").split(","):
            candidate = raw_id.strip()
            if not candidate:
                continue
            try:
                moderators.append(int(candidate))
            except ValueError as exc:
                logger.error("Invalid moderator chat id encountered: {}", candidate)
                raise RuntimeError(f"Invalid moderator chat id: {candidate!r}") from exc

    super_admins: list[int] = []
    raw_super_admins = parser.get(
        CONFIG_SECTION, "super_admin_ids", fallback=""
    ).strip()
    if raw_super_admins:
        for raw_id in raw_super_admins.replace("\n", ",").split(","):
            candidate = raw_id.strip()
            if not candidate:
                continue
            try:
                super_admins.append(int(candidate))
            except ValueError as exc:
                logger.error(
                    "Invalid super admin id encountered in config: {}",
                    candidate,
                )
                raise RuntimeError(f"Invalid super admin id: {candidate!r}") from exc

    super_admins = sorted(set(super_admins))

    if not parser.has_section(VALKEY_CONFIG_SECTION):
        logger.error(
            "Section '{}' missing in config file {}", VALKEY_CONFIG_SECTION, config_path
        )
        raise RuntimeError(f"Section '{VALKEY_CONFIG_SECTION}' missing in config file.")

    host = parser.get(VALKEY_CONFIG_SECTION, "valkey_host", fallback="").strip()
    if not host:
        logger.error("Valkey host is missing in the config file {}", config_path)
        raise RuntimeError("Valkey host is missing in the config file.")

    try:
        port = parser.getint(VALKEY_CONFIG_SECTION, "valkey_port")
    except ValueError as exc:
        logger.error("Valkey port must be an integer in {}", config_path)
        raise RuntimeError("Valkey port must be an integer.") from exc

    password = parser.get(VALKEY_CONFIG_SECTION, "valkey_pass", fallback="").strip()
    prefix = (
        parser.get(
            VALKEY_CONFIG_SECTION, "valkey_prefix", fallback="second_hand"
        ).strip()
        or "second_hand"
    )

    logger.info(
        "Configuration loaded for {} moderators, {} super admins and Valkey host {}:{} with prefix '{}'",
        len(moderators),
        len(super_admins),
        host,
        port,
        prefix,
    )

    return {
        "token": token,
        "moderator_chat_ids": moderators,
        "super_admin_ids": super_admins,
        "valkey": {
            "host": host,
            "port": port,
            "password": password or None,
            "prefix": prefix,
        },
    }


def create_valkey_client(settings: dict[str, Any]) -> Valkey | InMemoryValkey:
    """Create a Valkey client using loaded configuration settings."""

    valkey_settings = settings["valkey"]
    client = Valkey(
        host=valkey_settings["host"],
        port=valkey_settings["port"],
        password=valkey_settings["password"],
    )

    try:
        client.ping()
        logger.info(
            "Connected to Valkey at {}:{}",
            valkey_settings["host"],
            valkey_settings["port"],
        )
        return client
    except ValkeyConnectionError as exc:
        logger.warning(
            "Valkey connection unavailable ({}). Falling back to in-memory store",
            exc,
        )
        return InMemoryValkey()
    except (ValkeyTimeoutError, ValkeyResponseError) as exc:
        logger.warning(
            "Valkey ping failed with {}. Falling back to in-memory store",
            type(exc).__name__,
        )
        logger.opt(exception=exc).debug("Valkey ping failure details")
        return InMemoryValkey()
    except Exception:  # noqa: BLE001
        logger.exception("Valkey connection failed. Falling back to in-memory store")
        return InMemoryValkey()


__all__ = [
    "CONFIG_SECTION",
    "DEFAULT_CONFIG_PATH",
    "VALKEY_CONFIG_SECTION",
    "create_valkey_client",
    "load_config",
]
