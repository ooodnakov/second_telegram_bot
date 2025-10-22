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
    MEDIA_ROOT,
    SKIP_KEYWORD,
)
from bot.logging import logger
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
    session_dir: Path | None = None,
) -> dict[str, Any]:
    state = {
        "session_key": session_key,
        "user_id": user_id,
        "photos": [],
    }
    if session_dir is not None:
        state["session_dir"] = session_dir
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
            "Submission %s missing for editing request by user %s",
            session_key,
            user_id,
        )
    elif submission.get("user_id") != str(user_id):
        logger.warning(
            "User %s attempted to edit submission %s owned by %s",
            user_id,
            session_key,
            submission.get("user_id"),
        )
        return None
    return submission


async def start_edit_position(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning("Position edit invoked without callback query: %s", update)
        return ConversationHandler.END

    data = (query.data or "").split(":", 2)
    if len(data) != 3 or data[0] != "edit" or data[1] != "position":
        await query.answer()
        logger.warning("Unexpected position edit payload: %s", query.data)
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
    logger.debug("Prompted user %s to edit position for %s", user_id, session_key)
    return EDIT_POSITION


async def receive_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning("Position update received without message or user: %s", update)
        return ConversationHandler.END

    state = _get_edit_state(context)
    session_key = state.get("session_key")
    if not session_key or state.get("user_id") != user.id:
        logger.warning("Position update missing edit state for user %s", user.id)
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
    logger.info("User %s updated position for %s", user.id, session_key)
    _clear_edit_state(context)
    return ConversationHandler.END


async def start_edit_description(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning("Description edit invoked without callback query: %s", update)
        return ConversationHandler.END

    data = (query.data or "").split(":", 2)
    if len(data) != 3 or data[0] != "edit" or data[1] != "description":
        await query.answer()
        logger.warning("Unexpected description edit payload: %s", query.data)
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
    logger.debug("Prompted user %s to edit description for %s", user_id, session_key)
    return EDIT_DESCRIPTION


async def receive_description(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning("Description update received without message: %s", update)
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
    logger.info("User %s updated description for %s", user.id, session_key)
    _clear_edit_state(context)
    return ConversationHandler.END


async def start_edit_condition(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning("Condition edit invoked without callback query: %s", update)
        return ConversationHandler.END

    data = (query.data or "").split(":", 2)
    if len(data) != 3 or data[0] != "edit" or data[1] != "condition":
        await query.answer()
        logger.warning("Unexpected condition edit payload: %s", query.data)
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
    logger.debug("Prompted user %s to edit condition for %s", user_id, session_key)
    return EDIT_CONDITION


async def receive_condition_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning("Condition choice received without callback query: %s", update)
        return ConversationHandler.END

    data = (query.data or "").split(":", 4)
    if len(data) != 4 or data[0] != "edit_condition" or data[1] != "set":
        await query.answer()
        logger.warning("Unexpected condition selection payload: %s", query.data)
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
        logger.warning("Unknown condition key %s in edit flow", condition_key)
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
        logger.debug("Condition prompt message missing for user %s", user_id)
    logger.info("User %s updated condition for %s", user_id, session_key)
    _clear_edit_state(context)
    return ConversationHandler.END


async def start_edit_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning("Photo edit invoked without callback query: %s", update)
        return ConversationHandler.END

    data = (query.data or "").split(":", 2)
    if len(data) != 3 or data[0] != "edit" or data[1] != "photos":
        await query.answer()
        logger.warning("Unexpected photo edit payload: %s", query.data)
        return ConversationHandler.END

    session_key = data[2]
    user_id = query.from_user.id
    submission = _ensure_submission(context, user_id, session_key)
    if submission is None:
        await query.answer(get_message("general.session_missing"), show_alert=True)
        return ConversationHandler.END

    session_dir_raw = submission.get("session_dir", "")
    session_dir = Path(session_dir_raw) if session_dir_raw else MEDIA_ROOT / session_key
    session_dir.mkdir(parents=True, exist_ok=True)

    state = _store_edit_state(
        context, session_key=session_key, user_id=user_id, session_dir=session_dir
    )
    state["photos"] = []

    await query.answer()
    if query.message is not None:
        await query.message.reply_text(
            get_message("edit.photos_prompt", keyword=SKIP_KEYWORD)
        )
    logger.debug("Prompted user %s to upload new photos for %s", user_id, session_key)
    return EDIT_PHOTOS


async def receive_photo_upload(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning("Photo upload received without message: %s", update)
        return ConversationHandler.END

    state = _get_edit_state(context)
    session_key = state.get("session_key")
    if not session_key or state.get("user_id") != user.id:
        await message.reply_text(get_message("general.session_missing"))
        return ConversationHandler.END

    if not message.photo:
        await message.reply_text(get_message("edit.photos_expected"))
        return EDIT_PHOTOS

    photos: list[Path] = state.setdefault("photos", [])
    if len(photos) >= _MAX_PHOTO_COUNT:
        await message.reply_text(get_message("edit.photos_limit", keyword=SKIP_KEYWORD))
        return EDIT_PHOTOS

    session_dir: Path | None = state.get("session_dir")
    if session_dir is None:
        await message.reply_text(get_message("edit.update_failed"))
        logger.error("Session directory missing for photo edit %s", session_key)
        _clear_edit_state(context)
        return ConversationHandler.END

    try:
        telegram_file = await context.bot.get_file(message.photo[-1].file_id)
        suffix = Path(telegram_file.file_path or "").suffix or ".jpg"
        filename = f"update_{len(photos) + 1:02d}{suffix}"
        target_path = session_dir / filename
        await telegram_file.download_to_drive(custom_path=str(target_path))
        photos.append(target_path)
        logger.info(
            "User %s uploaded photo %s for session %s", user.id, filename, session_key
        )
    except (TelegramError, OSError):  # pragma: no cover - network/IO related
        logger.exception(
            "Failed to download photo for session %s during edit", session_key
        )
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
        logger.warning("Photo finalization received without message: %s", update)
        return ConversationHandler.END

    state = _get_edit_state(context)
    session_key = state.get("session_key")
    if not session_key or state.get("user_id") != user.id:
        await message.reply_text(get_message("general.session_missing"))
        return ConversationHandler.END

    photos: list[Path] = state.get("photos", [])
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
    logger.info("User %s updated photos for %s", user.id, session_key)
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
