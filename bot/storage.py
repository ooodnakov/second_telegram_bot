"""Persistence helpers backed by Valkey or in-memory storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from bot.logging import logger
from telegram.ext import ContextTypes
from valkey import Valkey


class InMemoryValkey:
    """Fallback store that mimics the subset of Valkey used by the bot."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set[str]] = {}

    def hset(self, name: str, mapping: dict[str, str]) -> None:
        self._hashes.setdefault(name, {}).update(mapping)

    def hdel(self, name: str, *fields: str) -> int:
        target = self._hashes.get(name)
        if not target:
            return 0
        removed = 0
        for field in fields:
            if field in target:
                del target[field]
                removed += 1
        if not target:
            self._hashes.pop(name, None)
        return removed

    def hgetall(self, name: str) -> dict[str, str]:
        return self._hashes.get(name, {}).copy()

    def sadd(self, name: str, key: str) -> int:
        target = self._sets.setdefault(name, set())
        before = len(target)
        target.add(key)
        return int(len(target) > before)

    def srem(self, name: str, key: str) -> int:
        target = self._sets.get(name)
        if not target or key not in target:
            return 0
        target.remove(key)
        return 1

    def smembers(self, name: str) -> set[str]:
        return self._sets.get(name, set()).copy()

    def delete(self, *names: str) -> None:
        for name in names:
            self._hashes.pop(name, None)
            self._sets.pop(name, None)

    def ping(self) -> bool:
        return True


class ApplicationStore:
    """Read/write per-user submission state backed by Valkey."""

    _LIST_FIELDS = {"photos"}
    _INT_FIELDS = {"_photo_prompt_message_id"}

    def __init__(self, client: Valkey | InMemoryValkey, prefix: str) -> None:
        self._client = client
        self._prefix = prefix

    def _session_key(self, user_id: int) -> str:
        return f"{self._prefix}:session:{user_id}"

    def init_session(self, user_id: int, data: dict[str, Any]) -> None:
        key = self._session_key(user_id)
        self._client.delete(key)
        self._client.hset(key, mapping=self._serialize(data))
        logger.debug(
            "Initialized session {} for user {} with data keys {}",
            key,
            user_id,
            sorted(data.keys()),
        )

    def set_fields(self, user_id: int, **fields: Any) -> None:
        if not fields:
            return
        key = self._session_key(user_id)
        self._client.hset(key, mapping=self._serialize(fields))
        logger.debug(
            "Updated session {} for user {} with field keys {}",
            key,
            user_id,
            sorted(fields.keys()),
        )

    def append_photo(self, user_id: int, photo_path: Path) -> list[Path]:
        session = self.get(user_id)
        photos = session.get("photos", [])
        photos.append(photo_path)
        self.set_fields(user_id, photos=photos)
        logger.debug(
            "Appended photo {} for user {} (total now {})",
            photo_path,
            user_id,
            len(photos),
        )
        return photos

    def get(self, user_id: int) -> dict[str, Any]:
        key = self._session_key(user_id)
        raw = self._client.hgetall(key)
        if not raw:
            logger.debug("No existing session found for user {}", user_id)
            return {}
        session = self._deserialize(raw)  # type: ignore[arg-type]
        session.setdefault("photos", [])
        logger.debug(
            "Loaded session {} for user {} with keys {}",
            key,
            user_id,
            sorted(session.keys()),
        )
        return session

    def clear(self, user_id: int) -> None:
        self._client.delete(self._session_key(user_id))
        logger.debug("Cleared session for user {}", user_id)

    def _serialize(self, data: dict[str, Any]) -> dict[str, str]:
        serialized: dict[str, str] = {}
        for field, value in data.items():
            if field in self._LIST_FIELDS:
                serialized[field] = json.dumps([str(Path(item)) for item in value])
            elif field in self._INT_FIELDS:
                serialized[field] = "" if value is None else str(value)
            elif isinstance(value, Path):
                serialized[field] = str(value)
            elif value is None:
                serialized[field] = ""
            else:
                serialized[field] = str(value)
        return serialized

    def _deserialize(self, data: Mapping[str | bytes, str | bytes]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for raw_key, raw_value in data.items():
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            value = raw_value.decode() if isinstance(raw_value, bytes) else raw_value
            if key in self._LIST_FIELDS:
                if value:
                    result[key] = [Path(item) for item in json.loads(value)]
                else:
                    result[key] = []
            elif key in self._INT_FIELDS:
                result[key] = int(value) if value else None
            elif key == "session_dir":
                result[key] = Path(value) if value else None
            else:
                result[key] = value
        return result


def get_application_store(context: ContextTypes.DEFAULT_TYPE) -> ApplicationStore:
    """Retrieve the :class:`ApplicationStore` for the current bot context."""

    client = context.application.bot_data.get("valkey_client")
    if client is None:
        raise RuntimeError("Valkey client is not configured")
    prefix = context.application.bot_data.get("valkey_prefix", "second_hand")
    return ApplicationStore(client, prefix)


__all__ = ["ApplicationStore", "InMemoryValkey", "get_application_store"]
