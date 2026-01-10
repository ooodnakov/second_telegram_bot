"""Helper utilities for administrator functionality."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from bot.constants import UTC
from bot.logging import logger
from valkey.exceptions import ValkeyError

BROADCAST_INDEX_SUFFIX = "broadcasts"
BROADCAST_KEY_PREFIX = "broadcast"
ADMIN_SET_SUFFIX = "admins"
ADMIN_USERNAME_TO_ID_SUFFIX = "admin_usernames"
ADMIN_ID_TO_USERNAME_SUFFIX = "admin_ids"
USERS_SET_SUFFIX = "users"
USERS_USERNAME_TO_ID_SUFFIX = "user_usernames"
USERS_ID_TO_USERNAME_SUFFIX = "user_ids"
APPLICATIONS_SET_SUFFIX = "applications"


def _get_prefix(context: Any) -> str:
    return context.application.bot_data.get("valkey_prefix", "second_hand")


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
    if removed:
        _clear_admin_username(context, user_id)
    return bool(removed)


def _normalize_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


def _username_key(context: Any, suffix: str) -> str:
    return f"{_get_prefix(context)}:{suffix}"


def _id_key(context: Any, suffix: str) -> str:
    return f"{_get_prefix(context)}:{suffix}"


def set_admin_username(context: Any, user_id: int, username: str) -> bool:
    client = _get_client(context)
    if client is None:
        logger.warning("Valkey client missing while storing admin username")
        return False
    normalized = _normalize_username(username)
    if not normalized:
        return False
    try:
        client.hset(  # type: ignore[attr-defined]
            _username_key(context, ADMIN_USERNAME_TO_ID_SUFFIX),
            mapping={normalized: str(user_id)},
        )
        client.hset(  # type: ignore[attr-defined]
            _id_key(context, ADMIN_ID_TO_USERNAME_SUFFIX),
            mapping={str(user_id): normalized},
        )
    except ValkeyError:
        logger.exception("Failed to store username mapping for admin {}", user_id)
        return False
    return True


def get_admin_id_for_username(context: Any, username: str) -> int | None:
    client = _get_client(context)
    if client is None:
        return None
    normalized = _normalize_username(username)
    if not normalized:
        return None
    try:
        raw = client.hget(  # type: ignore[attr-defined]
            _username_key(context, ADMIN_USERNAME_TO_ID_SUFFIX),
            normalized,
        )
    except ValkeyError:
        logger.exception("Failed to resolve admin id for username {}", normalized)
        return None
    if raw is None:
        return None
    try:
        return int(_decode(raw))
    except (TypeError, ValueError):
        return None


def get_admin_username_for_id(context: Any, user_id: int) -> str | None:
    client = _get_client(context)
    if client is None:
        return None
    try:
        raw = client.hget(  # type: ignore[attr-defined]
            _id_key(context, ADMIN_ID_TO_USERNAME_SUFFIX),
            str(user_id),
        )
    except ValkeyError:
        logger.exception("Failed to resolve username for admin id {}", user_id)
        return None
    if not raw:
        return None
    return _decode(raw)


def _clear_admin_username(context: Any, user_id: int) -> None:
    client = _get_client(context)
    if client is None:
        return
    try:
        raw = client.hget(  # type: ignore[attr-defined]
            _id_key(context, ADMIN_ID_TO_USERNAME_SUFFIX),
            str(user_id),
        )
        if raw:
            username = _decode(raw)
            client.hdel(  # type: ignore[attr-defined]
                _username_key(context, ADMIN_USERNAME_TO_ID_SUFFIX),
                username,
            )
        client.hdel(  # type: ignore[attr-defined]
            _id_key(context, ADMIN_ID_TO_USERNAME_SUFFIX),
            str(user_id),
        )
    except ValkeyError:
        logger.exception("Failed to clear username mapping for admin {}", user_id)


def set_user_username(context: Any, user_id: int, username: str | None) -> bool:
    client = _get_client(context)
    if client is None:
        return False
    normalized = _normalize_username(username or "")
    if not normalized:
        return False
    try:
        client.hset(  # type: ignore[attr-defined]
            _username_key(context, USERS_USERNAME_TO_ID_SUFFIX),
            mapping={normalized: str(user_id)},
        )
        client.hset(  # type: ignore[attr-defined]
            _id_key(context, USERS_ID_TO_USERNAME_SUFFIX),
            mapping={str(user_id): normalized},
        )
    except ValkeyError:
        logger.exception("Failed to store username mapping for user {}", user_id)
        return False
    return True


def get_user_id_for_username(context: Any, username: str) -> int | None:
    client = _get_client(context)
    if client is None:
        return None
    normalized = _normalize_username(username)
    if not normalized:
        return None
    try:
        raw = client.hget(  # type: ignore[attr-defined]
            _username_key(context, USERS_USERNAME_TO_ID_SUFFIX),
            normalized,
        )
    except ValkeyError:
        logger.exception("Failed to resolve user id for username {}", normalized)
        return None
    if raw is None:
        return None
    try:
        return int(_decode(raw))
    except (TypeError, ValueError):
        return None


def get_username_for_user_id(context: Any, user_id: int) -> str | None:
    client = _get_client(context)
    if client is None:
        return None
    try:
        raw = client.hget(  # type: ignore[attr-defined]
            _id_key(context, USERS_ID_TO_USERNAME_SUFFIX),
            str(user_id),
        )
    except ValkeyError:
        logger.exception("Failed to resolve username for user id {}", user_id)
        return None
    if not raw:
        return None
    return _decode(raw)


def record_active_user(context: Any, user_id: int, username: str | None = None) -> None:
    client = _get_client(context)
    if client is None:
        return
    key = f"{_get_prefix(context)}:{USERS_SET_SUFFIX}"
    try:
        client.sadd(key, str(user_id))  # type: ignore[attr-defined]
        if username:
            set_user_username(context, user_id, username)
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
        if record and not record.get("deleted_at"):
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
            f"Valkey client missing while revoking application {session_key}"
        )
        return False

    prefix = _get_prefix(context)
    key = f"{prefix}:{session_key}"
    record = load_application(client, key)
    if not record:
        logger.warning(f"Application {key} not found for revocation")
        return False

    owner = record.get("user_id")
    if owner != str(user_id):
        logger.warning(
            f"User {user_id} attempted to revoke application {key} owned by {owner}"
        )
        return False

    if record.get("revoked_at"):
        logger.info(f"Application {key} is already revoked")
        return False

    timestamp = datetime.now(UTC).isoformat()
    if not timestamp:
        logger.error(f"Failed to generate revocation timestamp for {key}")
        return False

    try:
        client.hset(  # type: ignore[attr-defined] - runtime Valkey client exposes hset
            key,
            mapping={"revoked_at": timestamp, "revoked_by": str(user_id)},
        )
    except ValkeyError:
        logger.exception(f"Failed to mark application {key} as revoked")
        return False

    logger.info(f"Application {key} revoked by user {user_id}")
    return True


def mark_application_deleted(context: Any, session_key: str, user_id: int) -> bool:
    client = _get_client(context)
    if client is None:
        logger.warning(
            f"Valkey client missing while deleting application {session_key}"
        )
        return False

    prefix = _get_prefix(context)
    key = f"{prefix}:{session_key}"
    record = load_application(client, key)
    if not record:
        logger.warning(f"Application {key} not found for deletion")
        return False

    owner = record.get("user_id")
    if owner != str(user_id):
        logger.warning(
            f"User {user_id} attempted to delete application {key} owned by {owner}"
        )
        return False

    if record.get("deleted_at"):
        logger.info(f"Application {key} is already deleted")
        return False

    timestamp = datetime.now(UTC).isoformat()
    if not timestamp:
        logger.error(f"Failed to generate deletion timestamp for {key}")
        return False

    try:
        client.hset(  # type: ignore[attr-defined] - runtime Valkey client exposes hset
            key,
            mapping={"deleted_at": timestamp, "deleted_by": str(user_id)},
        )
    except ValkeyError:
        logger.exception(f"Failed to mark application {key} as deleted")
        return False

    logger.info(f"Application {key} deleted by user {user_id}")
    return True


def update_application_fields(
    context: Any, session_key: str, user_id: int, **fields: Any
) -> bool:
    """Persist updates to an application owned by the current user."""

    if not fields:
        logger.debug(
            f"No fields provided for application {session_key} update; treating as "
            "success"
        )
        return True

    client = _get_client(context)
    if client is None:
        logger.warning(
            f"Valkey client missing while updating application {session_key}"
        )
        return False

    prefix = _get_prefix(context)
    key = f"{prefix}:{session_key}"
    record = load_application(client, key)
    if not record:
        logger.warning(f"Application {key} not found for update")
        return False

    owner = record.get("user_id")
    if owner != str(user_id):
        logger.warning(
            f"User {user_id} attempted to update application {key} owned by {owner}"
        )
        return False

    serialized: dict[str, str] = {}
    for field, value in fields.items():
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            serialized[field] = ",".join(str(item) for item in value)
        elif value is None:
            serialized[field] = ""
        else:
            serialized[field] = str(value)

    try:
        client.hset(key, mapping=serialized)  # type: ignore[attr-defined]
    except ValkeyError:
        logger.exception(f"Failed to update application {key}")
        return False

    logger.info(
        f"Application {key} updated by user {user_id} with fields "
        f"{sorted(serialized.keys())}"
    )
    return True


def mark_application_reviewed(
    context: Any, session_key: str, reviewer_id: int
) -> str | None:
    """Mark an application as reviewed by an admin.

    Args:
        context: Telegram context providing Valkey access.
        session_key: Identifier of the submission session to update.
        reviewer_id: Administrator ID applying the review flag.

    Returns:
        ISO8601 timestamp of the review if the update succeeds, otherwise
        ``None``.
    """

    client = _get_client(context)
    if client is None:
        logger.warning(
            f"Valkey client missing while marking application {session_key} reviewed"
        )
        return None

    prefix = _get_prefix(context)
    key = f"{prefix}:{session_key}"
    record = load_application(client, key)
    if not record:
        logger.warning(f"Application {key} not found for review")
        return None

    timestamp = datetime.now(UTC).isoformat()

    try:
        client.hset(  # type: ignore[attr-defined]
            key,
            mapping={"reviewed_at": timestamp, "reviewed_by": str(reviewer_id)},
        )
    except ValkeyError:
        logger.exception(f"Failed to mark application {key} as reviewed")
        return None

    logger.info(f"Application {key} marked reviewed by admin {reviewer_id}")
    return timestamp


def clear_application_review(context: Any, session_key: str) -> bool:
    """Remove the review flag from an application."""

    client = _get_client(context)
    if client is None:
        logger.warning(
            f"Valkey client missing while clearing review flag for {session_key}"
        )
        return False

    prefix = _get_prefix(context)
    key = f"{prefix}:{session_key}"
    record = load_application(client, key)
    if not record:
        logger.warning(f"Application {key} not found while clearing review")
        return False

    try:
        client.hdel(  # type: ignore[attr-defined]
            key,
            "reviewed_at",
            "reviewed_by",
        )
    except ValkeyError:
        logger.exception(f"Failed to clear review flag for application {key}")
        return False

    logger.info(f"Cleared review flag for application {key}")
    return True


__all__ = [
    "add_admin",
    "clear_application_review",
    "fetch_all_submissions",
    "fetch_user_submissions",
    "get_admins",
    "get_admin_id_for_username",
    "get_admin_username_for_id",
    "get_super_admins",
    "get_user_id_for_username",
    "get_username_for_user_id",
    "is_admin",
    "is_super_admin",
    "list_active_users",
    "list_application_keys",
    "list_broadcast_records",
    "load_broadcast_record",
    "remove_admin",
    "mark_application_revoked",
    "mark_application_deleted",
    "mark_application_reviewed",
    "record_active_user",
    "recipients_for_audience",
    "save_broadcast_record",
    "set_admin_username",
    "set_user_username",
    "update_broadcast_record",
    "update_application_fields",
]
