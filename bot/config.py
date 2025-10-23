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
STORAGE_CONFIG_SECTION = "storage"
DEFAULT_MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"
DEFAULT_MEDIA_CACHE = DEFAULT_MEDIA_ROOT / "cache"


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

    storage_settings = _load_storage_settings(parser, config_path)
    backend = storage_settings.get("backend", "local")

    logger.info(
        "Configuration loaded for {} moderators, {} super admins and Valkey host {}:{} with prefix '{}'",  # noqa: E501
        len(moderators),
        len(super_admins),
        host,
        port,
        prefix,
    )
    logger.info("Media storage backend configured: {}", backend)

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
        "storage": storage_settings,
    }


def _load_storage_settings(parser: ConfigParser, config_path: Path) -> dict[str, Any]:
    if not parser.has_section(STORAGE_CONFIG_SECTION):
        return {
            "backend": "local",
            "local_root": str(DEFAULT_MEDIA_ROOT),
            "cache_dir": str(DEFAULT_MEDIA_ROOT),
        }

    backend = (
        parser.get(STORAGE_CONFIG_SECTION, "backend", fallback="local")
        .strip()
        .lower()
        or "local"
    )
    cache_dir_raw = parser.get(STORAGE_CONFIG_SECTION, "cache_dir", fallback="").strip()
    cache_dir = Path(cache_dir_raw).expanduser() if cache_dir_raw else None

    if backend == "local":
        local_root_raw = parser.get(
            STORAGE_CONFIG_SECTION, "local_root", fallback=str(DEFAULT_MEDIA_ROOT)
        ).strip()
        local_root = Path(local_root_raw).expanduser() if local_root_raw else DEFAULT_MEDIA_ROOT
        return {
            "backend": "local",
            "local_root": str(local_root),
            "cache_dir": str(cache_dir or local_root),
        }

    if backend == "minio":
        endpoint = parser.get(STORAGE_CONFIG_SECTION, "minio_endpoint", fallback="").strip()
        if not endpoint:
            raise RuntimeError(
                f"MinIO endpoint must be configured in section '{STORAGE_CONFIG_SECTION}'"
            )
        bucket = parser.get(STORAGE_CONFIG_SECTION, "minio_bucket", fallback="").strip()
        if not bucket:
            raise RuntimeError(
                f"MinIO bucket must be configured in section '{STORAGE_CONFIG_SECTION}'"
            )
        access_key = parser.get(
            STORAGE_CONFIG_SECTION, "minio_access_key", fallback=""
        ).strip()
        secret_key = parser.get(
            STORAGE_CONFIG_SECTION, "minio_secret_key", fallback=""
        ).strip()
        secure = parser.getboolean(STORAGE_CONFIG_SECTION, "minio_secure", fallback=True)
        prefix = parser.get(STORAGE_CONFIG_SECTION, "minio_prefix", fallback="").strip()
        cache_path = cache_dir or DEFAULT_MEDIA_CACHE
        return {
            "backend": "minio",
            "cache_dir": str(cache_path),
            "minio": {
                "endpoint": endpoint,
                "bucket": bucket,
                "access_key": access_key,
                "secret_key": secret_key,
                "secure": secure,
                "prefix": prefix,
            },
        }

    raise RuntimeError(
        f"Unsupported storage backend '{backend}' in config file {config_path}"
    )


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
    "STORAGE_CONFIG_SECTION",
    "create_valkey_client",
    "load_config",
]
