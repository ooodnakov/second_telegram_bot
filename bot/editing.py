"""Conversation handlers for editing existing submissions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bot.admin import update_application_fields
from bot.commands import (
    get_cached_submission,
    refresh_application_detail,
    update_cached_submission,
)
from bot.constants import (
    EDIT_CONDITION,
    EDIT_DESCRIPTION,
    EDIT_PHOTOS,
    EDIT_POSITION,
    SKIP_KEYWORD,
)
from bot.logging import logger
from bot.media_storage import get_media_storage
from bot.messages import get_message
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes, ConversationHandler

EDIT_STATE_KEY = "edit_state"
_MAX_PHOTO_COUNT = 5


def _store_edit_state(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    session_key: str,
    user_id: int,
) -> dict[str, Any]:
    state = {
        "session_key": session_key,
        "user_id": user_id,
        "photos": [],
    }
    context.user_data[EDIT_STATE_KEY] = state  # type: ignore[index]
    return state


def _get_edit_state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    state = context.user_data.get(EDIT_STATE_KEY)  # type: ignore[index]
    if isinstance(state, dict):
        return state
    return {}


def _clear_edit_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(EDIT_STATE_KEY, None)  # type: ignore[arg-type]


def _ensure_submission(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_key: str,
) -> dict[str, str] | None:
    submission = get_cached_submission(context, user_id, session_key)
    if submission is None:
        logger.warning(
            f"Submission {session_key} missing for editing request by user {user_id}"
        )
    elif submission.get("user_id") != str(user_id):
        logger.warning(
            f"User {user_id} attempted to edit submission {session_key} owned by "
            f"{submission.get('user_id')}"
        )
        return None
    return submission


async def start_edit_position(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning(f"Position edit invoked without callback query: {update}")
        return ConversationHandler.END

    data = (query.data or "").split(":", 2)
    if len(data) != 3 or data[0] != "edit" or data[1] != "position":
        await query.answer()
        logger.warning(f"Unexpected position edit payload: {query.data}")
        return ConversationHandler.END

    session_key = data[2]
    user_id = query.from_user.id
    submission = _ensure_submission(context, user_id, session_key)
    if submission is None:
        await query.answer(get_message("general.session_missing"), show_alert=True)
        return ConversationHandler.END

    _store_edit_state(context, session_key=session_key, user_id=user_id)
    await query.answer()
    current_value = submission.get("position") or get_message("general.placeholder")
    prompt = get_message("edit.position_prompt", current=current_value)
    if query.message is not None:
        await query.message.reply_text(prompt)
    logger.debug(f"Prompted user {user_id} to edit position for {session_key}")
    return EDIT_POSITION


async def receive_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning(f"Position update received without message or user: {update}")
        return ConversationHandler.END

    state = _get_edit_state(context)
    session_key = state.get("session_key")
    if not session_key or state.get("user_id") != user.id:
        logger.warning(f"Position update missing edit state for user {user.id}")
        await message.reply_text(get_message("general.session_missing"))
        return ConversationHandler.END

    new_value = message.text or ""
    success = update_application_fields(
        context, session_key, user.id, position=new_value
    )
    if not success:
        await message.reply_text(get_message("edit.update_failed"))
        _clear_edit_state(context)
        return ConversationHandler.END

    update_cached_submission(context, user.id, session_key, position=new_value)
    await refresh_application_detail(context, user.id, session_key)
    await message.reply_text(get_message("edit.position_saved"))
    logger.info(f"User {user.id} updated position for {session_key}")
    _clear_edit_state(context)
    return ConversationHandler.END


async def start_edit_description(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning(f"Description edit invoked without callback query: {update}")
        return ConversationHandler.END

    data = (query.data or "").split(":", 2)
    if len(data) != 3 or data[0] != "edit" or data[1] != "description":
        await query.answer()
        logger.warning(f"Unexpected description edit payload: {query.data}")
        return ConversationHandler.END

    session_key = data[2]
    user_id = query.from_user.id
    submission = _ensure_submission(context, user_id, session_key)
    if submission is None:
        await query.answer(get_message("general.session_missing"), show_alert=True)
        return ConversationHandler.END

    _store_edit_state(context, session_key=session_key, user_id=user_id)
    await query.answer()
    current_value = submission.get("description") or get_message("general.placeholder")
    prompt = get_message("edit.description_prompt", current=current_value)
    if query.message is not None:
        await query.message.reply_text(prompt)
    logger.debug(f"Prompted user {user_id} to edit description for {session_key}")
    return EDIT_DESCRIPTION


async def receive_description(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning(f"Description update received without message: {update}")
        return ConversationHandler.END

    state = _get_edit_state(context)
    session_key = state.get("session_key")
    if not session_key or state.get("user_id") != user.id:
        await message.reply_text(get_message("general.session_missing"))
        return ConversationHandler.END

    new_value = message.text or ""
    success = update_application_fields(
        context, session_key, user.id, description=new_value
    )
    if not success:
        await message.reply_text(get_message("edit.update_failed"))
        _clear_edit_state(context)
        return ConversationHandler.END

    update_cached_submission(context, user.id, session_key, description=new_value)
    await refresh_application_detail(context, user.id, session_key)
    await message.reply_text(get_message("edit.description_saved"))
    logger.info(f"User {user.id} updated description for {session_key}")
    _clear_edit_state(context)
    return ConversationHandler.END


async def start_edit_condition(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning(f"Condition edit invoked without callback query: {update}")
        return ConversationHandler.END

    data = (query.data or "").split(":", 2)
    if len(data) != 3 or data[0] != "edit" or data[1] != "condition":
        await query.answer()
        logger.warning(f"Unexpected condition edit payload: {query.data}")
        return ConversationHandler.END

    session_key = data[2]
    user_id = query.from_user.id
    submission = _ensure_submission(context, user_id, session_key)
    if submission is None:
        await query.answer(get_message("general.session_missing"), show_alert=True)
        return ConversationHandler.END

    _store_edit_state(context, session_key=session_key, user_id=user_id)
    await query.answer()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    get_message("workflow.condition_used"),
                    callback_data=f"edit_condition:set:{session_key}:used",
                ),
                InlineKeyboardButton(
                    get_message("workflow.condition_new"),
                    callback_data=f"edit_condition:set:{session_key}:new",
                ),
            ]
        ]
    )
    if query.message is not None:
        await query.message.reply_text(
            get_message("edit.condition_prompt"), reply_markup=keyboard
        )
    logger.debug(f"Prompted user {user_id} to edit condition for {session_key}")
    return EDIT_CONDITION


async def receive_condition_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning(f"Condition choice received without callback query: {update}")
        return ConversationHandler.END

    data = (query.data or "").split(":", 4)
    if len(data) != 4 or data[0] != "edit_condition" or data[1] != "set":
        await query.answer()
        logger.warning(f"Unexpected condition selection payload: {query.data}")
        return ConversationHandler.END

    session_key = data[2]
    condition_key = data[3]
    user_id = query.from_user.id

    state = _get_edit_state(context)
    if state.get("session_key") != session_key or state.get("user_id") != user_id:
        await query.answer(get_message("general.session_missing"), show_alert=True)
        return ConversationHandler.END

    condition_map = {
        "used": get_message("workflow.condition_used"),
        "new": get_message("workflow.condition_new"),
    }
    condition_value = condition_map.get(condition_key)
    if condition_value is None:
        await query.answer()
        logger.warning(f"Unknown condition key {condition_key} in edit flow")
        return ConversationHandler.END

    await query.answer()

    success = update_application_fields(
        context, session_key, user_id, condition=condition_value
    )
    if not success:
        await query.answer(get_message("edit.update_failed"), show_alert=True)
        _clear_edit_state(context)
        return ConversationHandler.END

    update_cached_submission(context, user_id, session_key, condition=condition_value)
    await refresh_application_detail(context, user_id, session_key)
    try:
        await query.edit_message_text(get_message("edit.condition_saved"))
    except BadRequest:
        logger.debug(f"Condition prompt message missing for user {user_id}")
    logger.info(f"User {user_id} updated condition for {session_key}")
    _clear_edit_state(context)
    return ConversationHandler.END


async def start_edit_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning(f"Photo edit invoked without callback query: {update}")
        return ConversationHandler.END

    data = (query.data or "").split(":", 2)
    if len(data) != 3 or data[0] != "edit" or data[1] != "photos":
        await query.answer()
        logger.warning(f"Unexpected photo edit payload: {query.data}")
        return ConversationHandler.END

    session_key = data[2]
    user_id = query.from_user.id
    submission = _ensure_submission(context, user_id, session_key)
    if submission is None:
        await query.answer(get_message("general.session_missing"), show_alert=True)
        return ConversationHandler.END

    storage = get_media_storage(context)
    storage.get_session(session_key)

    state = _store_edit_state(context, session_key=session_key, user_id=user_id)
    state["photos"] = []

    await query.answer()
    if query.message is not None:
        await query.message.reply_text(
            get_message("edit.photos_prompt", keyword=SKIP_KEYWORD)
        )
    logger.debug(f"Prompted user {user_id} to upload new photos for {session_key}")
    return EDIT_PHOTOS


async def receive_photo_upload(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning(f"Photo upload received without message: {update}")
        return ConversationHandler.END

    state = _get_edit_state(context)
    session_key = state.get("session_key")
    if not session_key or state.get("user_id") != user.id:
        await message.reply_text(get_message("general.session_missing"))
        return ConversationHandler.END

    if not message.photo:
        await message.reply_text(get_message("edit.photos_expected"))
        return EDIT_PHOTOS

    photos: list[str] = state.setdefault("photos", [])
    if len(photos) >= _MAX_PHOTO_COUNT:
        await message.reply_text(get_message("edit.photos_limit", keyword=SKIP_KEYWORD))
        return EDIT_PHOTOS

    storage = get_media_storage(context)
    session = storage.get_session(session_key)

    try:
        telegram_file = await context.bot.get_file(message.photo[-1].file_id)
        suffix = Path(telegram_file.file_path or "").suffix or ".jpg"
        filename = f"update_{len(photos) + 1:02d}{suffix}"
        target_path = storage.allocate_path(session, filename)
        await telegram_file.download_to_drive(custom_path=str(target_path))
        handle = storage.finalize_upload(session, target_path)
        photos.append(handle)
        logger.info(
            f"User {user.id} uploaded photo {filename} for session {session_key}"
        )
    except (TelegramError, OSError):  # pragma: no cover - network/IO related
        logger.exception(f"Failed to download photo for session {session_key} during edit")
        await message.reply_text(get_message("edit.update_failed"))
        _clear_edit_state(context)
        return ConversationHandler.END

    if len(photos) >= _MAX_PHOTO_COUNT:
        await message.reply_text(
            get_message("edit.photos_limit_reached", keyword=SKIP_KEYWORD)
        )
    else:
        await message.reply_text(
            get_message("edit.photos_more_prompt", keyword=SKIP_KEYWORD)
        )
    return EDIT_PHOTOS


async def finalize_photo_upload(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning(f"Photo finalization received without message: {update}")
        return ConversationHandler.END

    state = _get_edit_state(context)
    session_key = state.get("session_key")
    if not session_key or state.get("user_id") != user.id:
        await message.reply_text(get_message("general.session_missing"))
        return ConversationHandler.END

    photos: list[str] = state.get("photos", [])
    if not photos:
        await message.reply_text(get_message("edit.photos_min_required"))
        return EDIT_PHOTOS

    photo_strings = [str(path) for path in photos]
    success = update_application_fields(
        context, session_key, user.id, photos=photo_strings
    )
    if not success:
        await message.reply_text(get_message("edit.update_failed"))
        _clear_edit_state(context)
        return ConversationHandler.END

    update_cached_submission(
        context, user.id, session_key, photos=",".join(photo_strings)
    )
    await refresh_application_detail(context, user.id, session_key, send_photos=True)
    await message.reply_text(get_message("edit.photos_saved"))
    logger.info(f"User {user.id} updated photos for {session_key}")
    _clear_edit_state(context)
    return ConversationHandler.END


async def cancel_editing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_edit_state(context)
    if update.message is not None:
        await update.message.reply_text(get_message("edit.cancelled"))
    elif update.callback_query is not None:
        await update.callback_query.answer(
            get_message("edit.cancelled"), show_alert=True
        )
    return ConversationHandler.END


__all__ = [
    "EDIT_CONDITION",
    "EDIT_DESCRIPTION",
    "EDIT_PHOTOS",
    "EDIT_POSITION",
    "cancel_editing",
    "finalize_photo_upload",
    "receive_condition_choice",
    "receive_description",
    "receive_photo_upload",
    "receive_position",
    "start_edit_condition",
    "start_edit_description",
    "start_edit_photos",
    "start_edit_position",
]
