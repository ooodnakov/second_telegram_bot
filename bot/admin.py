"""Helper utilities for administrator functionality."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from bot.constants import UTC
from bot.logging import logger
from valkey.exceptions import ValkeyError

BROADCAST_INDEX_SUFFIX = "broadcasts"
BROADCAST_KEY_PREFIX = "broadcast"
ADMIN_SET_SUFFIX = "admins"
USERS_SET_SUFFIX = "users"
APPLICATIONS_SET_SUFFIX = "applications"


def _get_prefix(context: Any) -> str:
    return context.application.bot_data.get("valkey_prefix", "telegram_auto_poster")


def _get_client(context: Any) -> Any | None:
    return context.application.bot_data.get("valkey_client")


def get_super_admins(context: Any) -> set[int]:
    return set(context.application.bot_data.get("super_admin_ids", []))


def is_super_admin(context: Any, user_id: int) -> bool:
    return user_id in get_super_admins(context)


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def get_admins(context: Any) -> set[int]:
    client = _get_client(context)
    if client is None:
        logger.warning("Valkey client missing while fetching admins")
        return set()
    key = f"{_get_prefix(context)}:{ADMIN_SET_SUFFIX}"
    try:
        raw = client.smembers(key)  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to load admin set from Valkey")
        return set()
    return {int(_decode(item)) for item in raw}


def is_admin(context: Any, user_id: int) -> bool:
    if is_super_admin(context, user_id):
        return True
    return user_id in get_admins(context)


def add_admin(context: Any, user_id: int) -> bool:
    client = _get_client(context)
    if client is None:
        logger.warning("Valkey client missing while adding admin")
        return False
    key = f"{_get_prefix(context)}:{ADMIN_SET_SUFFIX}"
    try:
        added = client.sadd(key, str(user_id))  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to add admin {} to Valkey", user_id)
        return False
    return bool(added)


def remove_admin(context: Any, user_id: int) -> bool:
    client = _get_client(context)
    if client is None:
        logger.warning("Valkey client missing while removing admin")
        return False
    key = f"{_get_prefix(context)}:{ADMIN_SET_SUFFIX}"
    try:
        removed = client.srem(key, str(user_id))  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to remove admin {} from Valkey", user_id)
        return False
    return bool(removed)


def record_active_user(context: Any, user_id: int) -> None:
    client = _get_client(context)
    if client is None:
        return
    key = f"{_get_prefix(context)}:{USERS_SET_SUFFIX}"
    try:
        client.sadd(key, str(user_id))  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to track active user {} in Valkey", user_id)


def list_active_users(context: Any) -> set[int]:
    client = _get_client(context)
    if client is None:
        return set()
    key = f"{_get_prefix(context)}:{USERS_SET_SUFFIX}"
    try:
        raw = client.smembers(key)  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to read active users from Valkey")
        return set()
    return {int(_decode(item)) for item in raw}


def list_application_keys(context: Any) -> list[str]:
    client = _get_client(context)
    if client is None:
        return []
    key = f"{_get_prefix(context)}:{APPLICATIONS_SET_SUFFIX}"
    try:
        raw = client.smembers(key)  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to load application key set from Valkey")
        return []
    return sorted(_decode(item) for item in raw)


def load_application(client: Any, key: str) -> dict[str, str]:
    try:
        raw_data: Mapping[Any, Any] = client.hgetall(key)  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to load application {} from Valkey", key)
        return {}
    if not raw_data:
        return {}
    result: dict[str, str] = {}
    for raw_field, raw_value in raw_data.items():
        field = _decode(raw_field)
        result[field] = _decode(raw_value)
    return result


def fetch_all_submissions(context: Any) -> list[dict[str, str]] | None:
    client = _get_client(context)
    if client is None:
        logger.warning("Valkey client missing while fetching submissions")
        return None
    submissions: list[dict[str, str]] = []
    for key in list_application_keys(context):
        record = load_application(client, key)
        if record:
            submissions.append(record)
    submissions.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return submissions


def fetch_user_submissions(context: Any, user_id: int) -> list[dict[str, str]] | None:
    submissions = fetch_all_submissions(context)
    if submissions is None:
        return None
    return [item for item in submissions if item.get("user_id") == str(user_id)]


def recipients_for_audience(context: Any, audience: str) -> set[int]:
    audience = audience or ""
    if audience == "all":
        return list_active_users(context)
    if audience == "recent":
        cutoff = datetime.now(UTC) - timedelta(days=30)
        recipients: set[int] = set()
        submissions = fetch_all_submissions(context)
        if not submissions:
            return set()
        for submission in submissions:
            created_at = submission.get("created_at", "")
            try:
                timestamp = datetime.fromisoformat(created_at)
            except ValueError:
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            if timestamp >= cutoff:
                try:
                    recipients.add(int(submission.get("user_id", "0")))
                except ValueError:
                    continue
        return recipients
    return set()


def save_broadcast_record(context: Any, record: dict[str, str]) -> None:
    client = _get_client(context)
    if client is None:
        logger.warning("Valkey client missing while storing broadcast record")
        return
    prefix = _get_prefix(context)
    key = f"{prefix}:{BROADCAST_KEY_PREFIX}:{record['id']}"
    index_key = f"{prefix}:{BROADCAST_INDEX_SUFFIX}"
    try:
        client.hset(key, mapping=record)  # type: ignore[attr-defined]
        client.sadd(index_key, key)  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to persist broadcast record {}", record.get("id"))


def update_broadcast_record(context: Any, broadcast_id: str, **fields: str) -> None:
    client = _get_client(context)
    if client is None:
        return
    key = f"{_get_prefix(context)}:{BROADCAST_KEY_PREFIX}:{broadcast_id}"
    try:
        client.hset(key, mapping=fields)  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to update broadcast {}", broadcast_id)


def load_broadcast_record(context: Any, broadcast_id: str) -> dict[str, str]:
    client = _get_client(context)
    if client is None:
        return {}
    key = f"{_get_prefix(context)}:{BROADCAST_KEY_PREFIX}:{broadcast_id}"
    return load_application(client, key)


def list_broadcast_records(context: Any) -> list[dict[str, str]]:
    client = _get_client(context)
    if client is None:
        return []
    prefix = _get_prefix(context)
    index_key = f"{prefix}:{BROADCAST_INDEX_SUFFIX}"
    try:
        raw_keys = client.smembers(index_key)  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception("Failed to list broadcast records")
        return []
    broadcasts: list[dict[str, str]] = []
    for raw_key in raw_keys:
        key = _decode(raw_key)
        record = load_application(client, key)
        if record:
            broadcasts.append(record)
    broadcasts.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return broadcasts


def mark_application_revoked(context: Any, session_key: str, user_id: int) -> bool:
    client = _get_client(context)
    if client is None:
        logger.warning(
            "Valkey client missing while revoking application %s", session_key
        )
        return False

    prefix = _get_prefix(context)
    key = f"{prefix}:{session_key}"
    record = load_application(client, key)
    if not record:
        logger.warning("Application %s not found for revocation", key)
        return False

    owner = record.get("user_id")
    if owner != str(user_id):
        logger.warning(
            "User %s attempted to revoke application %s owned by %s",
            user_id,
            key,
            owner,
        )
        return False

    if record.get("revoked_at"):
        logger.info("Application %s is already revoked", key)
        return False

    timestamp = datetime.now(UTC).isoformat()
    if not timestamp:
        logger.error("Failed to generate revocation timestamp for %s", key)
        return False

    try:
        client.hset(  # type: ignore[attr-defined] - runtime Valkey client exposes hset
            key,
            mapping={"revoked_at": timestamp, "revoked_by": str(user_id)},
        )
    except ValkeyError:
        logger.exception("Failed to mark application %s as revoked", key)
        return False

    logger.info("Application %s revoked by user %s", key, user_id)
    return True


__all__ = [
    "add_admin",
    "fetch_all_submissions",
    "fetch_user_submissions",
    "get_admins",
    "get_super_admins",
    "is_admin",
    "is_super_admin",
    "list_active_users",
    "list_application_keys",
    "list_broadcast_records",
    "load_broadcast_record",
    "remove_admin",
    "mark_application_revoked",
    "record_active_user",
    "recipients_for_audience",
    "save_broadcast_record",
    "update_broadcast_record",
]
