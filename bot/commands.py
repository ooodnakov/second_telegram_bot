"""Command handlers for the Telegram bot."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from bot.admin import (
    fetch_user_submissions,
    is_admin,
    is_super_admin,
    mark_application_revoked,
    record_active_user,
)
from bot.constants import LIST_PAGE_SIZE, MEDIA_ROOT, MOSCOW_TZ, POSITION, UTC
from bot.logging import logger
from bot.messages import get_message
from bot.storage import get_application_store
from bot.workflow import _send_submission_photos
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

REVOKE_CACHE_KEY = "revoke_submissions"
LIST_STATE_KEY = "list_state"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display a greeting message when the user invokes /start."""

    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Received /start update without message or user: {}", update)
        return ConversationHandler.END

    logger.info("User {} invoked /start", user.id)
    record_active_user(context, user.id)

    await update.message.reply_text(get_message("start.greeting"))
    await update.message.reply_text(get_message("start.new_instruction"))
    return ConversationHandler.END


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kick off a new submission workflow."""

    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Received /new update without message or user: {}", update)
        return ConversationHandler.END

    try:
        store = get_application_store(context)
    except RuntimeError:
        logger.exception("Failed to obtain application store for user %s", user.id)
        await update.message.reply_text(
            get_message("general.storage_unavailable_support")
        )
        return ConversationHandler.END

    record_active_user(context, user.id)

    await update.message.reply_text(
        get_message("workflow.position_prompt"),
        parse_mode="Markdown",
    )
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    session_dir = MEDIA_ROOT / f"{user.id}_{timestamp}_{uuid4().hex[:6]}"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_key = session_dir.name
    logger.info(
        "Starting new submission workflow for user {} with session {}",
        user.id,
        session_key,
    )
    store.init_session(
        user.id,
        {
            "photos": [],
            "session_dir": session_dir,
            "session_key": session_key,
        },
    )
    return POSITION


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply with help text for the bot."""

    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Received /help update without message or user: {}", update)
        return ConversationHandler.END

    logger.info("User {} requested help", user.id)
    text_lines = [get_message("help.text"), ""]

    if is_admin(context, user.id):
        text_lines.append(get_message("help.admin_header"))
        text_lines.append(get_message("help.admin_view"))
        text_lines.append(get_message("help.admin_broadcast"))
        text_lines.append(get_message("help.admin_history"))
        text_lines.append(get_message("help.admin_scheduled"))
        text_lines.append("")

    if is_super_admin(context, user.id):
        text_lines.append(get_message("help.super_admin_header"))
        text_lines.append(get_message("help.super_admin_add"))
        text_lines.append(get_message("help.super_admin_remove"))
        text_lines.append(get_message("help.super_admin_list"))

    text = "\n".join(text_lines).strip()
    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except BadRequest as exc:
        _MARKDOWN_PARSE_ERROR = "can't parse entities"
        if _MARKDOWN_PARSE_ERROR in str(exc).lower():
            logger.warning(
                "Failed to send help text with Markdown for user {}: {}", user.id, exc
            )
            await update.message.reply_text(text)
        else:
            raise
    return ConversationHandler.END


def _get_or_create_list_state(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[str, Any]:
    state = context.user_data.get(LIST_STATE_KEY)  # type: ignore[index]
    if isinstance(state, dict):
        return state
    state = {}
    context.user_data[LIST_STATE_KEY] = state  # type: ignore[index]
    return state


def _set_list_state_submissions(
    state: dict[str, Any], submissions: list[dict[str, str]]
) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for submission in submissions:
        session_key = submission.get("session_key")
        if session_key:
            lookup[session_key] = submission
    state["submissions"] = submissions
    state["lookup"] = lookup
    return lookup


def _ensure_submissions_loaded(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    user_id: int,
) -> list[dict[str, str]] | None:
    submissions = state.get("submissions")
    if isinstance(submissions, list):
        return submissions
    submissions = fetch_user_submissions(context, user_id)
    if submissions is None:
        return None
    _set_list_state_submissions(state, submissions)
    return submissions


def _get_submission_from_state(
    state: dict[str, Any], session_key: str
) -> dict[str, str] | None:
    lookup = state.get("lookup")
    if isinstance(lookup, dict):
        submission = lookup.get(session_key)
        if submission:
            return submission
    submissions = state.get("submissions")
    if isinstance(submissions, list):
        for submission in submissions:
            if submission.get("session_key") == session_key:
                if isinstance(lookup, dict):
                    lookup[session_key] = submission
                else:
                    state["lookup"] = {session_key: submission}
                return submission
    return None


def _get_submission_with_cache(
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
    user_id: int,
    session_key: str,
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, str] | None, bool]:
    """Retrieve a submission from cache, optionally refreshing from storage.

    Returns a tuple of ``(submission, storage_error)``.
    """

    submission = _get_submission_from_state(state, session_key)
    if submission is not None:
        return submission, False

    if force_refresh:
        submissions = fetch_user_submissions(context, user_id)
        if submissions is None:
            return None, True
        _set_list_state_submissions(state, submissions)
    else:
        submissions = _ensure_submissions_loaded(context, state, user_id)
        if submissions is None:
            return None, True

    return _get_submission_from_state(state, session_key), False


def _extract_photo_paths(submission: dict[str, str]) -> list[Path]:
    raw_value = submission.get("photos", "")
    if not raw_value:
        return []
    paths: list[Path] = []
    for item in raw_value.split(","):
        stripped = item.strip()
        if stripped:
            paths.append(Path(stripped))
    return paths


def _format_detail_status(submission: dict[str, str]) -> str:
    revoked_at = submission.get("revoked_at", "")
    if revoked_at:
        return get_message(
            "list.detail_status_revoked",
            revoked_at=_format_created_at(revoked_at),
        )
    reviewed_at = submission.get("reviewed_at", "")
    if reviewed_at:
        reviewer = submission.get("reviewed_by", "")
        reviewer_text = reviewer or get_message("general.placeholder")
        return get_message(
            "list.detail_status_reviewed",
            reviewed_at=_format_created_at(reviewed_at),
            reviewer=reviewer_text,
        )
    return get_message("list.detail_status_active")


def _format_detail_text(submission: dict[str, str]) -> str:
    placeholder = get_message("general.placeholder")
    position = submission.get("position") or placeholder
    created_at = _format_created_at(submission.get("created_at", ""))
    lines = [get_message("list.detail_title", position=position)]
    lines.append(_format_detail_status(submission))
    lines.append(get_message("list.detail_created", created_at=created_at))
    lines.append("")
    fields = [
        ("list.detail_condition", submission.get("condition", "")),
        ("list.detail_size", submission.get("size", "")),
        ("list.detail_material", submission.get("material", "")),
        ("list.detail_description", submission.get("description", "")),
        ("list.detail_price", submission.get("price", "")),
        ("list.detail_contacts", submission.get("contacts", "")),
    ]
    for key, value in fields:
        lines.append(get_message(key, value=value or placeholder))
    photos = _extract_photo_paths(submission)
    lines.append(get_message("list.detail_photos", count=len(photos)))
    return "\n".join(lines)


def _build_detail_keyboard(
    session_key: str, page: int, user_id: int
) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                get_message("list.edit_position_button"),
                callback_data=f"edit:position:{session_key}",
            ),
            InlineKeyboardButton(
                get_message("list.edit_condition_button"),
                callback_data=f"edit:condition:{session_key}",
            ),
        ],
        [
            InlineKeyboardButton(
                get_message("list.edit_description_button"),
                callback_data=f"edit:description:{session_key}",
            ),
            InlineKeyboardButton(
                get_message("list.edit_photos_button"),
                callback_data=f"edit:photos:{session_key}",
            ),
        ],
        [
            InlineKeyboardButton(
                get_message("list.back_to_list_button"),
                callback_data=f"list:page:{page}:{user_id}",
            )
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def _clamp_page_index(submissions: list[dict[str, str]], page: int) -> int:
    if not submissions:
        return 0
    total_pages = (len(submissions) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
    total_pages = max(1, total_pages)
    return max(0, min(page, total_pages - 1))


async def list_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display a paginated list of previous submissions."""

    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Received /list update without message or user: {}", update)
        return ConversationHandler.END

    submissions = fetch_user_submissions(context, user.id)
    if submissions is None:
        await update.message.reply_text(get_message("general.storage_unavailable"))
        logger.error("Valkey unavailable when fetching list for user {}", user.id)
        return ConversationHandler.END
    if not submissions:
        await update.message.reply_text(get_message("general.no_submissions"))
        logger.info("User {} has no submissions stored", user.id)
        return ConversationHandler.END

    context.user_data.pop("list_submissions", None)  # type: ignore[arg-type]
    state = _get_or_create_list_state(context)
    _set_list_state_submissions(state, submissions)
    state["user_id"] = user.id
    state["page"] = 0
    state.pop("detail_message_id", None)
    state.pop("chat_id", None)
    text, keyboard, current_page = _render_applications_page(submissions, 0, user.id)
    state["page"] = current_page
    await update.message.reply_text(text, reply_markup=keyboard)
    logger.debug("Displayed submissions page 1 for user {}", user.id)
    return ConversationHandler.END


async def paginate_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks for paginating submissions."""

    query = update.callback_query
    if query is None:
        logger.warning("Received pagination update without callback query: {}", update)
        return

    data = query.data or ""
    try:
        prefix, action, page_raw, user_id_raw = data.split(":", 3)
        if prefix != "list" or action != "page":
            raise ValueError("Unexpected prefix")
        page = int(page_raw)
        expected_user_id = int(user_id_raw)
    except (ValueError, AttributeError):
        await query.answer()
        logger.warning("Received malformed pagination payload: {}", data)
        return

    user = query.from_user
    if user is None or user.id != expected_user_id:
        await query.answer(get_message("general.navigation_denied"), show_alert=True)
        logger.warning(
            "User {} attempted to paginate submissions for user {}",
            getattr(user, "id", "unknown"),
            expected_user_id,
        )
        return

    await query.answer()

    state = _get_or_create_list_state(context)
    state["user_id"] = user.id
    submissions = _ensure_submissions_loaded(context, state, user.id)
    if submissions is None:
        await query.edit_message_text(get_message("general.storage_unavailable"))
        logger.error("Valkey unavailable during pagination for user {}", user.id)
        return
    if not submissions:
        await query.edit_message_text(get_message("general.no_submissions"))
        logger.info("User {} has no submissions to paginate", user.id)
        return

    text, keyboard, current_page = _render_applications_page(submissions, page, user.id)
    state["page"] = current_page
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            logger.debug(
                "Pagination request for user {} ignored because message not modified",
                user.id,
            )
            return
        raise
    logger.debug("User {} navigated to submissions page {}", user.id, current_page + 1)


async def show_application_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Render a single submission selected from the list view."""

    query = update.callback_query
    if query is None:
        logger.warning("Received detail update without callback query: {}", update)
        return

    data = query.data or ""
    try:
        prefix, action, session_key, page_raw, user_id_raw = data.split(":", 4)
        if prefix != "list" or action != "view":
            raise ValueError("Unexpected prefix")
        page = int(page_raw)
        expected_user_id = int(user_id_raw)
    except (ValueError, AttributeError):
        await query.answer()
        logger.warning("Received malformed detail payload: {}", data)
        return

    user = query.from_user
    if user is None or user.id != expected_user_id:
        await query.answer(get_message("general.navigation_denied"), show_alert=True)
        logger.warning(
            "User {} attempted to view submission {} for user {}",
            getattr(user, "id", "unknown"),
            session_key,
            expected_user_id,
        )
        return

    state = _get_or_create_list_state(context)
    state["user_id"] = user.id
    submissions = _ensure_submissions_loaded(context, state, user.id)
    if submissions is None:
        await query.answer(get_message("general.storage_unavailable"), show_alert=True)
        logger.error("Valkey unavailable when rendering detail for user {}", user.id)
        return
    if not submissions:
        await query.edit_message_text(get_message("general.no_submissions"))
        logger.info("User {} has no submissions to display", user.id)
        return

    current_page = _clamp_page_index(submissions, page)
    state["page"] = current_page

    submission, storage_error = _get_submission_with_cache(
        context, state, user.id, session_key, force_refresh=True
    )
    if storage_error:
        await query.answer(get_message("general.storage_unavailable"), show_alert=True)
        logger.error("Valkey unavailable when refreshing detail for user {}", user.id)
        return

    if submission is None:
        await query.answer(get_message("general.session_missing"), show_alert=True)
        logger.warning(
            "Submission %s not found for detail view by user %s",
            session_key,
            user.id,
        )
        return

    if submission.get("user_id") != str(user.id):
        await query.answer(get_message("general.navigation_denied"), show_alert=True)
        logger.warning(
            "User %s attempted to view submission %s owned by %s",
            user.id,
            session_key,
            submission.get("user_id"),
        )
        return

    await query.answer()

    text = _format_detail_text(submission)
    keyboard = _build_detail_keyboard(session_key, current_page, user.id)
    message = query.message
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    if message is not None:
        state["detail_message_id"] = message.message_id
        try:
            state["chat_id"] = message.chat_id  # type: ignore[attr-defined]
        except AttributeError:
            chat = getattr(message, "chat", None)
            if chat is not None:
                state["chat_id"] = getattr(chat, "id", None)
    state["current_session_key"] = session_key

    photos = _extract_photo_paths(submission)
    chat_id = state.get("chat_id")
    if photos and isinstance(chat_id, int):
        await _send_submission_photos(context.bot, chat_id, photos)

    logger.debug(
        "Displayed submission %s details for user %s",
        session_key,
        user.id,
    )


async def refresh_application_detail(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_key: str,
    *,
    send_photos: bool = False,
) -> None:
    """Re-render the detail view after an edit operation."""

    state = _get_or_create_list_state(context)
    if state.get("user_id") != user_id:
        logger.debug(
            "Skipping detail refresh for user %s due to missing state", user_id
        )
        return

    submission, storage_error = _get_submission_with_cache(
        context, state, user_id, session_key
    )
    if storage_error:
        logger.error(
            "Valkey unavailable when refreshing submission %s for user %s",
            session_key,
            user_id,
        )
        return
    if submission is None:
        logger.warning(
            "Submission %s missing from cache during refresh for user %s",
            session_key,
            user_id,
        )
        return

    chat_id = state.get("chat_id")
    message_id = state.get("detail_message_id")
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        logger.debug(
            "Detail refresh skipped for user %s due to missing message context",
            user_id,
        )
        return

    page = state.get("page", 0)
    text = _format_detail_text(submission)
    keyboard = _build_detail_keyboard(session_key, page, user_id)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise

    state["current_session_key"] = session_key

    if send_photos:
        photos = _extract_photo_paths(submission)
        if photos:
            await _send_submission_photos(context.bot, chat_id, photos)


def get_cached_submission(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_key: str,
) -> dict[str, str] | None:
    """Return a cached submission for editing flows if available."""

    state = _get_or_create_list_state(context)
    if state.get("user_id") != user_id:
        return None
    submission, storage_error = _get_submission_with_cache(
        context, state, user_id, session_key
    )
    if storage_error:
        return None
    return submission


def update_cached_submission(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_key: str,
    **fields: str,
) -> None:
    """Apply updated fields to the cached submission data."""

    submission = get_cached_submission(context, user_id, session_key)
    if submission is None:
        return
    for field, value in fields.items():
        submission[field] = value


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled exceptions raised during update processing."""

    logger.opt(exception=context.error).error(
        "Unhandled exception during update processing for update: {}",
        update,
    )


def _render_applications_page(
    submissions: list[dict[str, str]],
    page: int,
    user_id: int,
) -> tuple[str, InlineKeyboardMarkup | None, int]:
    total = len(submissions)
    if total == 0:
        return get_message("general.no_submissions"), None, 0

    current_page = _clamp_page_index(submissions, page)
    start_index = current_page * LIST_PAGE_SIZE
    end_index = start_index + LIST_PAGE_SIZE
    page_items = submissions[start_index:end_index]

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    placeholder = get_message("general.placeholder")
    for index, submission in enumerate(page_items, start=start_index + 1):
        session_key = submission.get("session_key")
        if not session_key:
            continue
        position = submission.get("position", placeholder) or placeholder
        row.append(
            InlineKeyboardButton(
                get_message("list.entry_button", index=index, position=position),
                callback_data=f"list:view:{session_key}:{current_page}:{user_id}",
            )
        )
        if len(row) == 2:
            keyboard_rows.append(row)
            row = []
    if row:
        keyboard_rows.append(row)

    total_pages = (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
    if total_pages > 1:
        controls: list[InlineKeyboardButton] = []
        if current_page > 0:
            controls.append(
                InlineKeyboardButton(
                    get_message("list.back_button"),
                    callback_data=f"list:page:{current_page - 1}:{user_id}",
                )
            )
        controls.append(
            InlineKeyboardButton(
                get_message(
                    "list.page_indicator",
                    current=current_page + 1,
                    total=total_pages,
                ),
                callback_data=f"list:page:{current_page}:{user_id}",
            )
        )
        if current_page < total_pages - 1:
            controls.append(
                InlineKeyboardButton(
                    get_message("list.forward_button"),
                    callback_data=f"list:page:{current_page + 1}:{user_id}",
                )
            )
        keyboard_rows.append(controls)

    if not keyboard_rows:
        return get_message("general.no_submissions"), None, current_page

    text = get_message("list.instructions")
    return text, InlineKeyboardMarkup(keyboard_rows), current_page


def _format_created_at(value: str) -> str:
    if not value:
        return get_message("general.placeholder")
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _format_submission_status(submission: dict[str, str]) -> str:
    revoked_at = submission.get("revoked_at", "")
    if not revoked_at:
        return ""
    return get_message(
        "list.status_revoked",
        revoked_at=_format_created_at(revoked_at),
    )


def _build_revoke_cache(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> dict[str, dict[str, str]] | None:
    """Populate the revoke cache with the user's submissions if available.

    Args:
        context: Telegram context storing per-user state.
        user_id: Identifier of the user whose submissions should be cached.

    Returns:
        A mapping of session keys to submission metadata, or ``None`` when the
        storage backend is unavailable.
    """
    submissions = fetch_user_submissions(context, user_id)
    if submissions is None:
        return None
    cache: dict[str, dict[str, str]] = {}
    for submission in submissions:
        session_key = submission.get("session_key")
        if not session_key:
            continue
        cache[session_key] = submission
    context.user_data[REVOKE_CACHE_KEY] = cache  # type: ignore[index]
    return cache


def _get_revoke_cache(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> dict[str, dict[str, str]]:
    cache = context.user_data.get(REVOKE_CACHE_KEY)  # type: ignore[index]
    if isinstance(cache, dict):
        return cache
    rebuilt = _build_revoke_cache(context, user_id)
    return rebuilt or {}


async def revoke_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    if user is None or message is None:
        logger.warning("Received /revoke update without message or user: {}", update)
        return ConversationHandler.END

    cache = _build_revoke_cache(context, user.id)
    if cache is None:
        await message.reply_text(get_message("general.storage_unavailable"))
        logger.error("Valkey unavailable when user %s attempted revocation", user.id)
        return ConversationHandler.END

    active_items = [
        submission for submission in cache.values() if not submission.get("revoked_at")
    ]
    if not active_items:
        await message.reply_text(get_message("revoke.no_active"))
        logger.info("User %s has no active submissions to revoke", user.id)
        return ConversationHandler.END

    lines = [get_message("revoke.prompt")]
    keyboard: list[list[InlineKeyboardButton]] = []
    for index, submission in enumerate(active_items, start=1):
        position = submission.get("position", get_message("general.placeholder"))
        created_at = _format_created_at(submission.get("created_at", ""))
        lines.append(
            get_message(
                "revoke.list_entry",
                index=index,
                position=position,
                created_at=created_at,
            )
        )
        session_key = submission.get("session_key", "")
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{index}. {position}",
                    callback_data=f"revoke:select:{session_key}",
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                get_message("revoke.button_cancel"), callback_data="revoke:cancel"
            )
        ]
    )

    text = "\n".join(lines)
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    logger.debug("Presented revocation options to user %s", user.id)
    return ConversationHandler.END


async def handle_revoke_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        logger.warning("Received revoke callback without query: {}", update)
        return

    data = (query.data or "").split(":")
    if not data or data[0] != "revoke":
        return

    await query.answer()

    user = query.from_user
    if user is None:
        logger.warning("Revoke callback missing user: %s", update)
        return

    if len(data) >= 2 and data[1] == "cancel":
        await query.edit_message_text(get_message("revoke.cancelled"))
        context.user_data.pop(REVOKE_CACHE_KEY, None)  # type: ignore[index]
        logger.debug("User %s cancelled revocation", user.id)
        return

    if len(data) >= 3 and data[1] == "select":
        session_key = data[2]
        cache = _get_revoke_cache(context, user.id)
        submission = cache.get(session_key)
        if not submission:
            cache = _build_revoke_cache(context, user.id) or {}
            submission = cache.get(session_key)
        if submission is None:
            await query.edit_message_text(get_message("general.no_submissions"))
            logger.warning(
                "User %s attempted to revoke missing submission %s",
                user.id,
                session_key,
            )
            return
        if submission.get("revoked_at"):
            await query.edit_message_text(get_message("revoke.already_revoked"))
            return
        text = get_message(
            "revoke.confirm",
            position=submission.get("position", get_message("general.placeholder")),
            created_at=_format_created_at(submission.get("created_at", "")),
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        get_message("revoke.button_confirm"),
                        callback_data=f"revoke:confirm:{session_key}",
                    ),
                    InlineKeyboardButton(
                        get_message("revoke.button_decline"),
                        callback_data="revoke:cancel",
                    ),
                ]
            ]
        )
        await query.edit_message_text(text, reply_markup=keyboard)
        logger.debug(
            "User %s prompted to confirm revocation of submission %s",
            user.id,
            session_key,
        )
        return

    if len(data) >= 3 and data[1] == "confirm":
        session_key = data[2]
        cache = _get_revoke_cache(context, user.id)
        submission = cache.get(session_key)
        if not submission:
            cache = _build_revoke_cache(context, user.id) or {}
            submission = cache.get(session_key)
        if submission and submission.get("revoked_at"):
            await query.edit_message_text(get_message("revoke.already_revoked"))
            return

        success = mark_application_revoked(context, session_key, user.id)
        if not success:
            await query.edit_message_text(
                get_message("general.storage_unavailable_support")
            )
            return

        cache = _build_revoke_cache(context, user.id) or {}
        updated = cache.get(session_key)
        status = _format_submission_status(updated or {})
        message_text = get_message("revoke.success")
        status_suffix = status.strip()
        if status_suffix:
            message_text = f"{message_text} {html.escape(status_suffix)}"
        await query.edit_message_text(message_text)
        logger.info("User %s revoked submission %s", user.id, session_key)
        return


__all__ = [
    "error_handler",
    "help_command",
    "list_applications",
    "new",
    "paginate_list",
    "refresh_application_detail",
    "revoke_application",
    "handle_revoke_callback",
    "get_cached_submission",
    "update_cached_submission",
    "show_application_detail",
    "start",
]
