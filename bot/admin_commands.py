"""Handlers implementing admin-only features."""

from __future__ import annotations

import asyncio
import base64
import html
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from bot.admin import (
    add_admin,
    clear_application_review,
    fetch_all_submissions,
    get_admins,
    get_super_admins,
    is_admin,
    is_super_admin,
    list_broadcast_records,
    load_broadcast_record,
    mark_application_reviewed,
    recipients_for_audience,
    remove_admin,
    save_broadcast_record,
    update_broadcast_record,
)
from bot.constants import (
    ADMIN_ADD_ADMIN_WAIT_ID,
    ADMIN_BROADCAST_AUDIENCE,
    ADMIN_BROADCAST_CONFIRM,
    ADMIN_BROADCAST_DECISION,
    ADMIN_BROADCAST_MESSAGE,
    ADMIN_BROADCAST_SCHEDULE_TIME,
    ADMIN_REMOVE_ADMIN_WAIT_ID,
    MOSCOW_TZ,
    UTC,
)
from bot.logging import logger
from bot.messages import get_message
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes, ConversationHandler

ADMIN_VIEW_STATE_KEY = "admin_view_state"
BROADCAST_DATA_KEY = "admin_broadcast_data"

BROADCAST_AUDIENCE_ALL = "all"
BROADCAST_AUDIENCE_RECENT = "recent"

BROADCAST_STATUS_QUEUED = "queued"
BROADCAST_STATUS_SCHEDULED = "scheduled"
BROADCAST_STATUS_RUNNING = "running"
BROADCAST_STATUS_SENT = "sent"

_BROADCAST_STATUS_LABEL_KEYS = {
    BROADCAST_STATUS_QUEUED: "admin.history_status_queued",
    BROADCAST_STATUS_SCHEDULED: "admin.history_status_scheduled",
    BROADCAST_STATUS_RUNNING: "admin.history_status_running",
    BROADCAST_STATUS_SENT: "admin.history_status_sent",
}

BROADCAST_RATE_DELAY = 0.1

_PLACEHOLDER_PHOTO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)


async def _resolve_admin_identifier(
    context: ContextTypes.DEFAULT_TYPE, raw_text: str
) -> tuple[int | None, str | None, str]:
    """Resolve an admin identifier provided by a super admin."""

    text = raw_text.strip()
    if not text:
        return None, "invalid", text

    numeric_text = text.removeprefix("+")
    if numeric_text.isdigit():
        return int(numeric_text), None, numeric_text

    username = text if text.startswith("@") else f"@{text}"
    bot = getattr(context, "bot", None)
    if bot is None or not hasattr(bot, "get_chat"):
        logger.warning("Context bot missing while resolving admin identifier %s", text)
        return None, "not_found", username

    try:
        chat = await bot.get_chat(username)
    except BadRequest as exc:
        message = (getattr(exc, "message", None) or str(exc) or "").strip()
        if "chat not found" in message.lower() or "user not found" in message.lower():
            logger.info("Failed to resolve admin identifier %s: %s", username, message)
            return None, "not_found", username
        logger.warning(
            "Bad request while resolving admin identifier %s: %s", username, message
        )
        return None, "lookup_failed", username
    except TelegramError as exc:  # pragma: no cover - network errors
        logger.error("Telegram error resolving admin identifier %s: %s", username, exc)
        return None, "lookup_failed", username

    chat_id = getattr(chat, "id", None)
    chat_type = getattr(chat, "type", None)
    if isinstance(chat_type, ChatType):
        chat_type_str = chat_type.value
        is_private = chat_type is ChatType.PRIVATE
    else:
        chat_type_str = str(chat_type)
        is_private = chat_type_str.lower() == "private"

    if chat_id is None or not is_private:
        logger.info(
            "Resolved identifier %s to unsupported chat %s (type %s)",
            username,
            chat_id,
            chat_type_str,
        )
        return None, "not_found", username

    try:
        return int(chat_id), None, username
    except (TypeError, ValueError):
        logger.warning(
            "Resolved identifier %s with non-integer id %s", username, chat_id
        )
        return None, "not_found", username


async def view_all_applications(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Entry point for the admin application viewer."""

    user = update.effective_user
    message = update.message
    chat = update.effective_chat
    if user is None or message is None or chat is None:
        logger.warning("Admin view invoked without full update: %s", update)
        return

    if not is_admin(context, user.id):
        await message.reply_text(get_message("admin.not_authorized"))
        logger.info(
            "User %s attempted to access admin view without permissions", user.id
        )
        return

    submissions = fetch_all_submissions(context)
    if submissions is None:
        await message.reply_text(get_message("general.storage_unavailable_support"))
        logger.error("Valkey unavailable when admin %s requested submissions", user.id)
        return

    if not submissions:
        await message.reply_text(get_message("admin.no_applications"))
        return

    state = _build_view_state(submissions)
    state["chat_id"] = chat.id
    context.user_data[ADMIN_VIEW_STATE_KEY] = state

    await _render_admin_application(context, state)


async def navigate_applications(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline keyboard callbacks for the admin application viewer."""

    query = update.callback_query
    if query is None:
        logger.warning(
            "Received admin navigation update without callback query: %s", update
        )
        return

    state: dict[str, Any] | None = context.user_data.get(ADMIN_VIEW_STATE_KEY)
    if not state:
        await query.answer()
        try:
            await query.edit_message_caption(get_message("admin.no_applications"))
        except BadRequest:
            pass
        logger.warning(
            "Admin navigation requested without state by user %s",
            getattr(query.from_user, "id", "unknown"),
        )
        return

    data = (query.data or "").split(":")
    if len(data) < 2 or data[0] != "admin_view":
        await query.answer()
        return

    action = data[1]
    if action == "noop":
        await query.answer()
        return

    if action == "n" and len(data) >= 4:
        mode = data[2]
        direction = data[3]
        ordered = state.get("ordered", {})
        if mode not in ordered:
            await query.answer()
            logger.warning("Unknown mode %s in admin navigation", mode)
            return
        indexes = state.setdefault("indexes", {})
        index = indexes.get(mode, 0)
        total = len(ordered.get(mode, []))
        if direction == "next":
            if index >= total - 1:
                await query.answer(
                    get_message("admin.navigation_end"), show_alert=False
                )
                return
            indexes[mode] = index + 1
        elif direction == "prev":
            if index <= 0:
                await query.answer(
                    get_message("admin.navigation_end"), show_alert=False
                )
                return
            indexes[mode] = index - 1
        else:
            await query.answer()
            return
        state["mode"] = mode
        await query.answer()
        await _render_admin_application(context, state)
        return

    if action == "m" and len(data) >= 3:
        mode = data[2]
        if mode == state.get("mode"):
            await query.answer(get_message("admin.already_mode"), show_alert=False)
            return
        ordered = state.get("ordered", {})
        if mode not in ordered:
            await query.answer()
            logger.warning("Unknown admin mode %s", mode)
            return
        state["mode"] = mode
        state.setdefault("indexes", {}).setdefault(mode, 0)
        await query.answer()
        await _render_admin_application(context, state)
        return

    if action == "r" and len(data) >= 4:
        sub_action = data[2]
        session_key = data[3]
        submission = _find_submission(state, session_key)
        if submission is None:
            await query.answer(get_message("admin.review_missing"), show_alert=True)
            logger.warning(
                "Review toggle requested for unknown session %s", session_key
            )
            return
        user = query.from_user
        if user is None:
            await query.answer()
            logger.warning(
                "Review toggle missing user context for session %s", session_key
            )
            return
        if sub_action == "set":
            timestamp = mark_application_reviewed(context, session_key, user.id)
            if not timestamp:
                await query.answer(
                    get_message("admin.review_mark_failed"), show_alert=True
                )
                return
            submission["reviewed_at"] = timestamp
            submission["reviewed_by"] = str(user.id)
            message_key = "admin.review_marked"
        elif sub_action == "clear":
            success = clear_application_review(context, session_key)
            if not success:
                await query.answer(
                    get_message("admin.review_clear_failed"), show_alert=True
                )
                return
            submission.pop("reviewed_at", None)
            submission.pop("reviewed_by", None)
            message_key = "admin.review_cleared"
        else:
            await query.answer()
            return

        _apply_filter(state)
        await query.answer(get_message(message_key), show_alert=False)
        await _render_admin_application(context, state)
        return

    if action == "f" and len(data) >= 3:
        toggle = data[2]
        if toggle != "toggle":
            await query.answer()
            return
        hide_reviewed = not state.get("filter_hide_reviewed", False)
        state["filter_hide_reviewed"] = hide_reviewed
        _apply_filter(state)
        message_key = (
            "admin.filter_enabled" if hide_reviewed else "admin.filter_disabled"
        )
        await query.answer(get_message(message_key), show_alert=False)
        await _render_admin_application(context, state)
        return

    await query.answer()


async def navigate_application_photo_prev(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the previous photo for the current submission."""

    await _navigate_application_photo(update, context, -1)


async def navigate_application_photo_next(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the next photo for the current submission."""

    await _navigate_application_photo(update, context, 1)


async def _navigate_application_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE, direction: int
) -> None:
    query = update.callback_query
    if query is None:
        logger.warning(
            "Received admin photo navigation update without callback query: %s",
            update,
        )
        return

    state: dict[str, Any] | None = context.user_data.get(ADMIN_VIEW_STATE_KEY)
    if not state:
        await query.answer()
        logger.warning(
            "Photo navigation requested without view state by user %s",
            getattr(query.from_user, "id", "unknown"),
        )
        return

    data = (query.data or "").split(":", 1)
    if len(data) != 2:
        await query.answer()
        return

    session_key = data[1]
    if not session_key:
        await query.answer()
        return

    submission = _find_submission(state, session_key)
    if submission is None:
        await query.answer(get_message("admin.review_missing"), show_alert=True)
        logger.warning(
            "Photo navigation requested for unknown session %s",
            session_key,
        )
        return

    photo_paths = _available_photo_paths(submission)
    total = len(photo_paths)
    if total <= 1:
        await query.answer()
        return

    photo_indexes = state.setdefault("photo_indexes", {})
    current_index = photo_indexes.get(session_key, 0) % total
    new_index = (current_index + direction) % total
    photo_indexes[session_key] = new_index

    mode = state.get("mode", "time")
    submissions = state.get("ordered", {}).get(mode, [])
    indexes = state.setdefault("indexes", {})
    if isinstance(submissions, list):
        for idx, candidate in enumerate(submissions):
            if (
                isinstance(candidate, dict)
                and candidate.get("session_key") == session_key
            ):
                indexes[mode] = idx
                break

    state["current_session_key"] = session_key

    await query.answer()
    await _render_admin_application(context, state)


async def start_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start conversation for super admins to add new admins."""

    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("/addadmin invoked without message or user: %s", update)
        return ConversationHandler.END

    if not is_super_admin(context, user.id):
        await update.message.reply_text(get_message("admin.super_admin_required"))
        logger.info(
            "User %s attempted to add admin without super admin rights", user.id
        )
        return ConversationHandler.END

    await update.message.reply_text(get_message("admin.add_prompt"))
    return ADMIN_ADD_ADMIN_WAIT_ID


async def receive_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the user id provided by a super admin."""

    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning("Admin id reception invoked without message or user: %s", update)
        return ConversationHandler.END

    text = (message.text or "").strip()
    new_admin_id, error, identifier = await _resolve_admin_identifier(context, text)
    if new_admin_id is None:
        if error == "not_found":
            await message.reply_text(
                get_message("admin.user_lookup_failed", identifier=identifier or text)
            )
        elif error == "lookup_failed":
            await message.reply_text(get_message("admin.user_lookup_error"))
        else:
            await message.reply_text(get_message("admin.add_invalid"))
        return ADMIN_ADD_ADMIN_WAIT_ID

    if is_admin(context, new_admin_id):
        await message.reply_text(get_message("admin.add_already", user_id=new_admin_id))
        return ConversationHandler.END

    if add_admin(context, new_admin_id):
        await message.reply_text(get_message("admin.add_success", user_id=new_admin_id))
        logger.info("Super admin %s granted admin rights to %s", user.id, new_admin_id)
    else:
        await message.reply_text(get_message("general.storage_unavailable_support"))
        logger.error("Failed to add admin %s due to storage issue", new_admin_id)
    return ConversationHandler.END


async def start_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start conversation for super admins to remove admin rights."""

    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("/removeadmin invoked without message or user: %s", update)
        return ConversationHandler.END

    if not is_super_admin(context, user.id):
        await update.message.reply_text(get_message("admin.super_admin_required"))
        logger.info(
            "User %s attempted to remove admin without super admin rights", user.id
        )
        return ConversationHandler.END

    await update.message.reply_text(get_message("admin.remove_prompt"))
    return ADMIN_REMOVE_ADMIN_WAIT_ID


async def receive_remove_admin_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle the admin id provided for removal by a super admin."""

    message = update.message
    user = update.effective_user
    if message is None or user is None:
        logger.warning("Remove admin invoked without message or user: %s", update)
        return ConversationHandler.END

    text = (message.text or "").strip()
    target_admin_id, error, identifier = await _resolve_admin_identifier(context, text)
    if target_admin_id is None:
        if error == "not_found":
            await message.reply_text(
                get_message("admin.user_lookup_failed", identifier=identifier or text)
            )
        elif error == "lookup_failed":
            await message.reply_text(get_message("admin.user_lookup_error"))
        else:
            await message.reply_text(get_message("admin.add_invalid"))
        return ADMIN_REMOVE_ADMIN_WAIT_ID

    if is_super_admin(context, target_admin_id):
        await message.reply_text(get_message("admin.remove_super_admin"))
        return ConversationHandler.END

    if not is_admin(context, target_admin_id):
        await message.reply_text(
            get_message("admin.remove_missing", user_id=target_admin_id)
        )
        return ConversationHandler.END

    if remove_admin(context, target_admin_id):
        await message.reply_text(
            get_message("admin.remove_success", user_id=target_admin_id)
        )
        logger.info(
            "Super admin %s revoked admin rights from %s",
            user.id,
            target_admin_id,
        )
    else:
        await message.reply_text(get_message("general.storage_unavailable_support"))
        logger.error("Failed to remove admin %s due to storage issue", target_admin_id)
    return ConversationHandler.END


async def cancel_admin_action(update: Update, _: ContextTypes.DEFAULT_TYPE) -> int:
    """Fallback to cancel admin-related conversations."""

    message = update.message
    if message is not None:
        await message.reply_text(get_message("admin.cancelled"))
    return ConversationHandler.END


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Initiate the broadcast conversation."""

    user = update.effective_user
    message = update.message
    if user is None or message is None:
        logger.warning("/broadcast invoked without message or user: %s", update)
        return ConversationHandler.END

    if not is_admin(context, user.id):
        await message.reply_text(get_message("admin.not_authorized"))
        logger.info("User %s tried to start broadcast without rights", user.id)
        return ConversationHandler.END

    context.user_data[BROADCAST_DATA_KEY] = {"sender_id": user.id}

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    get_message("admin.broadcast_audience_all"),
                    callback_data=f"broadcast:audience:{BROADCAST_AUDIENCE_ALL}",
                )
            ],
            [
                InlineKeyboardButton(
                    get_message("admin.broadcast_audience_recent"),
                    callback_data=f"broadcast:audience:{BROADCAST_AUDIENCE_RECENT}",
                )
            ],
        ]
    )
    await message.reply_text(
        get_message("admin.broadcast_audience_prompt"),
        reply_markup=keyboard,
    )
    return ADMIN_BROADCAST_AUDIENCE


async def choose_broadcast_audience(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the broadcast audience and ask for the message body."""

    query = update.callback_query
    data: dict[str, Any] | None = context.user_data.get(BROADCAST_DATA_KEY)
    if query is None or data is None:
        logger.warning("Broadcast audience selection missing context: %s", update)
        return ConversationHandler.END

    await query.answer()

    audience_key = (query.data or "").split(":")[-1]
    if audience_key not in {BROADCAST_AUDIENCE_ALL, BROADCAST_AUDIENCE_RECENT}:
        return ADMIN_BROADCAST_AUDIENCE

    data["audience"] = audience_key

    _, count, audience_label = _resolve_broadcast_recipients(context, audience_key)
    data["recipient_count"] = count
    data["audience_label"] = audience_label

    if count == 0:
        await query.edit_message_text(get_message("admin.broadcast_no_recipients"))
        logger.info(
            "Broadcast cancelled due to zero recipients for audience: %s",
            audience_label,
        )
        context.user_data.pop(BROADCAST_DATA_KEY, None)
        return ConversationHandler.END

    await query.edit_message_text(
        get_message(
            "admin.broadcast_message_prompt_with_count",
            audience=audience_label,
            count=count,
        )
    )
    return ADMIN_BROADCAST_MESSAGE


async def receive_broadcast_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the broadcast text and show preview controls."""

    message = update.message
    data: dict[str, Any] | None = context.user_data.get(BROADCAST_DATA_KEY)
    if message is None or data is None:
        logger.warning("Broadcast message reception missing context: %s", update)
        return ConversationHandler.END

    text = (message.text or "").strip()
    if not text:
        if "recipient_count" in data:
            audience_label = data.get(
                "audience_label",
                _audience_label(data.get("audience", BROADCAST_AUDIENCE_ALL)),
            )
            await message.reply_text(
                get_message(
                    "admin.broadcast_message_prompt_with_count",
                    audience=audience_label,
                    count=data.get("recipient_count", 0),
                )
            )
        else:
            await message.reply_text(get_message("admin.broadcast_message_prompt"))
        return ADMIN_BROADCAST_MESSAGE

    data["text"] = text

    await message.reply_text(get_message("admin.broadcast_preview_title"))
    preview = await message.reply_text(text)
    data["preview_message_id"] = preview.message_id

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    get_message("admin.broadcast_send_now"),
                    callback_data="broadcast:decision:send",
                ),
                InlineKeyboardButton(
                    get_message("admin.broadcast_schedule"),
                    callback_data="broadcast:decision:schedule",
                ),
            ],
            [
                InlineKeyboardButton(
                    get_message("admin.broadcast_cancel"),
                    callback_data="broadcast:decision:cancel",
                )
            ],
        ]
    )
    await message.reply_text(
        get_message("admin.broadcast_preview_controls"),
        reply_markup=keyboard,
    )
    return ADMIN_BROADCAST_DECISION


async def handle_broadcast_decision(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle send/cancel/schedule decision from admin broadcast flow."""

    query = update.callback_query
    data: dict[str, Any] | None = context.user_data.get(BROADCAST_DATA_KEY)
    if query is None or data is None:
        logger.warning("Broadcast decision missing context: %s", update)
        return ConversationHandler.END

    await query.answer()

    decision = (query.data or "").split(":")[-1]
    if decision == "cancel":
        await query.edit_message_text(get_message("admin.broadcast_cancelled"))
        context.user_data.pop(BROADCAST_DATA_KEY, None)
        return ConversationHandler.END

    if decision == "send":
        data["mode"] = "now"
        if query.message is None:
            logger.error("Broadcast decision callback missing message context")
            context.user_data.pop(BROADCAST_DATA_KEY, None)
            return ConversationHandler.END
        await query.edit_message_text(get_message("admin.broadcast_confirm"))
        return await _prompt_broadcast_confirmation(
            query.message.chat.id, context, data
        )

    if decision == "schedule":
        await query.edit_message_text(get_message("admin.broadcast_scheduled_prompt"))
        data["mode"] = "schedule"
        return ADMIN_BROADCAST_SCHEDULE_TIME

    return ADMIN_BROADCAST_DECISION


async def receive_broadcast_schedule(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Parse the scheduled datetime for the broadcast."""

    message = update.message
    data: dict[str, Any] | None = context.user_data.get(BROADCAST_DATA_KEY)
    if message is None or data is None:
        logger.warning("Broadcast schedule input missing context: %s", update)
        return ConversationHandler.END

    text = (message.text or "").strip()
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M")
        localized = naive.replace(tzinfo=MOSCOW_TZ)
    except ValueError:
        await message.reply_text(get_message("admin.broadcast_schedule_invalid"))
        return ADMIN_BROADCAST_SCHEDULE_TIME

    now = datetime.now(MOSCOW_TZ)
    if localized <= now:
        await message.reply_text(get_message("admin.broadcast_schedule_invalid"))
        return ADMIN_BROADCAST_SCHEDULE_TIME

    data["scheduled_time"] = localized.astimezone(UTC).isoformat()
    data["scheduled_time_display"] = localized.strftime("%d.%m.%Y %H:%M")

    return await _prompt_broadcast_confirmation(message.chat.id, context, data)


async def _prompt_broadcast_confirmation(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    data: dict[str, Any],
) -> int:
    """Send summary and confirmation buttons for the broadcast."""

    audience = data.get("audience", BROADCAST_AUDIENCE_ALL)
    recipients, count, audience_label = _resolve_broadcast_recipients(context, audience)

    if not recipients:
        await context.bot.send_message(
            chat_id=chat_id,
            text=get_message("admin.broadcast_no_recipients"),
        )
        logger.info(
            "Broadcast confirmation aborted due to zero recipients for audience: %s",
            audience_label,
        )
        context.user_data.pop(BROADCAST_DATA_KEY, None)
        return ConversationHandler.END

    data["recipient_count"] = count
    data.setdefault("audience_label", audience_label)

    if data.get("mode") == "now":
        schedule_info = get_message("admin.broadcast_schedule_info_now")
    else:
        schedule_info = get_message(
            "admin.broadcast_schedule_info_time",
            time=data.get("scheduled_time_display", ""),
        )

    summary = get_message(
        "admin.broadcast_confirm",
        audience=audience_label,
        count=count,
        schedule_info=schedule_info,
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    get_message("admin.broadcast_confirm_button"),
                    callback_data="broadcast:confirm:confirm",
                ),
                InlineKeyboardButton(
                    get_message("admin.broadcast_confirm_cancel_button"),
                    callback_data="broadcast:confirm:cancel",
                ),
            ]
        ]
    )

    await context.bot.send_message(chat_id=chat_id, text=summary, reply_markup=keyboard)
    return ADMIN_BROADCAST_CONFIRM


async def confirm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finalize broadcast after confirmation."""

    query = update.callback_query
    data: dict[str, Any] | None = context.user_data.get(BROADCAST_DATA_KEY)
    if query is None or data is None:
        logger.warning("Broadcast confirmation missing context: %s", update)
        return ConversationHandler.END

    await query.answer()

    action = (query.data or "").split(":")[-1]
    if action != "confirm":
        await query.edit_message_text(get_message("admin.broadcast_cancelled"))
        context.user_data.pop(BROADCAST_DATA_KEY, None)
        return ConversationHandler.END

    audience = data.get("audience", BROADCAST_AUDIENCE_ALL)
    recipients, count, audience_label = _resolve_broadcast_recipients(context, audience)
    if not recipients:
        await query.edit_message_text(get_message("admin.broadcast_no_recipients"))
        logger.info(
            "Broadcast cancelled due to zero recipients for audience: %s",
            audience_label,
        )
        context.user_data.pop(BROADCAST_DATA_KEY, None)
        return ConversationHandler.END

    broadcast_id = uuid4().hex
    now = datetime.now(UTC)
    scheduled_iso = data.get("scheduled_time")
    scheduled_time = datetime.fromisoformat(scheduled_iso) if scheduled_iso else now
    status = (
        BROADCAST_STATUS_QUEUED
        if data.get("mode") == "now"
        else BROADCAST_STATUS_SCHEDULED
    )
    record = {
        "id": broadcast_id,
        "created_at": now.isoformat(),
        "scheduled_at": scheduled_time.isoformat(),
        "status": status,
        "audience": audience,
        "audience_label": audience_label,
        "text": data.get("text", ""),
        "sender_id": str(data.get("sender_id", "")),
        "recipient_count": str(count),
        "success_count": "0",
        "failed_count": "0",
        "completed_at": "",
    }
    save_broadcast_record(context, record)

    when = 0 if data.get("mode") == "now" else scheduled_time
    context.job_queue.run_once(
        execute_broadcast_job,
        when=when,
        data={"broadcast_id": broadcast_id},
        name=broadcast_id,
    )

    if data.get("mode") == "now":
        await query.edit_message_text(
            get_message("admin.broadcast_started", broadcast_id=broadcast_id)
        )
    else:
        await query.edit_message_text(
            get_message(
                "admin.broadcast_scheduled",
                time=data.get("scheduled_time_display", ""),
                broadcast_id=broadcast_id,
            )
        )

    context.user_data.pop(BROADCAST_DATA_KEY, None)
    return ConversationHandler.END


async def show_scheduled_broadcasts(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Display upcoming scheduled broadcasts."""

    user = update.effective_user
    message = update.message
    if user is None or message is None:
        logger.warning("/scheduled invoked without message or user: %s", update)
        return

    if not is_admin(context, user.id):
        await message.reply_text(get_message("admin.not_authorized"))
        logger.info(
            "User %s tried to access scheduled broadcasts without rights", user.id
        )
        return

    records = list_broadcast_records(context)
    upcoming = [
        record
        for record in records
        if record.get("status") in {BROADCAST_STATUS_SCHEDULED, BROADCAST_STATUS_QUEUED}
    ]

    if not upcoming:
        await message.reply_text(get_message("admin.scheduled_empty"))
        return

    lines = [get_message("admin.scheduled_header")]
    for index, record in enumerate(sorted(upcoming, key=_scheduled_sort_key), start=1):
        scheduled_at = record.get("scheduled_at", "")
        display_time = _format_timestamp(scheduled_at)
        lines.append(
            get_message(
                "admin.scheduled_entry",
                index=index,
                time=display_time,
                audience=record.get(
                    "audience_label", _audience_label(record.get("audience", ""))
                ),
                count=record.get("recipient_count", "0"),
                broadcast_id=record.get("id", ""),
            )
        )

    await message.reply_text("\n".join(lines), parse_mode="Markdown")


async def show_broadcast_history(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Display recent broadcast records for administrators."""

    user = update.effective_user
    message = update.message
    if user is None or message is None:
        logger.warning("/broadcast_history invoked without message or user: %s", update)
        return

    if not is_admin(context, user.id):
        await message.reply_text(get_message("admin.not_authorized"))
        logger.info("User %s tried to access broadcast history without rights", user.id)
        return

    records = list_broadcast_records(context)
    if not records:
        await message.reply_text(get_message("admin.history_empty"))
        return

    lines = [get_message("admin.history_header")]
    for index, record in enumerate(records[:10], start=1):
        created_at = _format_timestamp(record.get("created_at", ""))
        scheduled_at = _format_timestamp(record.get("scheduled_at", ""))
        completed_at = _format_timestamp(record.get("completed_at", ""))
        status_key = record.get("status", "")
        status_label = _BROADCAST_STATUS_LABEL_KEYS.get(status_key)
        if status_label:
            status = get_message(status_label)
        else:
            status = get_message(
                "admin.history_status_unknown",
                status=status_key or get_message("general.placeholder"),
            )

        audience = record.get(
            "audience_label", _audience_label(record.get("audience", ""))
        )
        schedule_info = ""
        if record.get("scheduled_at") and status_key in {
            BROADCAST_STATUS_SCHEDULED,
            BROADCAST_STATUS_QUEUED,
        }:
            schedule_info = " " + get_message(
                "admin.history_schedule_suffix", time=scheduled_at
            )
        completion_info = ""
        if record.get("completed_at"):
            completion_info = " " + get_message(
                "admin.history_completed_suffix", time=completed_at
            )

        lines.append(
            get_message(
                "admin.history_entry",
                index=index,
                created_at=created_at,
                status=status,
                audience=audience,
                count=record.get("recipient_count", "0"),
                success=record.get("success_count", "0"),
                failed=record.get("failed_count", "0"),
                broadcast_id=record.get("id", ""),
                schedule_info=schedule_info,
                completed_info=completion_info,
            )
        )

    await message.reply_text("\n".join(lines), parse_mode="Markdown")


async def show_admin_roster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display current admin and super admin assignments."""

    user = update.effective_user
    message = update.message
    if user is None or message is None:
        logger.warning("/admins invoked without message or user: %s", update)
        return

    if not is_super_admin(context, user.id):
        await message.reply_text(get_message("admin.super_admin_required"))
        logger.info(
            "User %s attempted to view admin roster without super admin rights",
            user.id,
        )
        return

    super_admins = sorted(get_super_admins(context))
    admin_ids = sorted(get_admins(context) - set(super_admins))

    lines = [get_message("admin.roster_header"), ""]
    lines.append(get_message("admin.roster_super_admins"))
    if super_admins:
        for index, admin_id in enumerate(super_admins, start=1):
            lines.append(
                get_message("admin.roster_entry", index=index, user_id=admin_id)
            )
    else:
        lines.append(get_message("admin.roster_empty"))

    lines.append("")
    lines.append(get_message("admin.roster_admins"))
    if admin_ids:
        for index, admin_id in enumerate(admin_ids, start=1):
            lines.append(
                get_message("admin.roster_entry", index=index, user_id=admin_id)
            )
    else:
        lines.append(get_message("admin.roster_empty"))

    await message.reply_text("\n".join(lines), parse_mode="Markdown")


async def execute_broadcast_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send queued broadcasts when the scheduler fires."""

    job = context.job
    if job is None:
        logger.error("Broadcast job executed without job context")
        return

    broadcast_id = job.data.get("broadcast_id") if isinstance(job.data, dict) else None
    if not broadcast_id:
        logger.error("Broadcast job missing broadcast_id in data: %s", job.data)
        return

    record = load_broadcast_record(context, broadcast_id)
    if not record:
        logger.error("Broadcast record %s missing; skipping job", broadcast_id)
        return

    audience = record.get("audience", BROADCAST_AUDIENCE_ALL)
    recipients = recipients_for_audience(context, audience)
    if not recipients:
        update_broadcast_record(
            context,
            broadcast_id,
            status=BROADCAST_STATUS_SENT,
            completed_at=datetime.now(UTC).isoformat(),
            success_count="0",
            failed_count="0",
        )
        return

    update_broadcast_record(
        context,
        broadcast_id,
        status=BROADCAST_STATUS_RUNNING,
    )

    text = record.get("text", "")
    success = 0
    failed = 0
    for recipient in sorted(recipients):
        try:
            await context.bot.send_message(chat_id=recipient, text=text)
            success += 1
        except TelegramError:
            failed += 1
            logger.exception(
                "Failed to deliver broadcast %s to %s", broadcast_id, recipient
            )
        await asyncio.sleep(BROADCAST_RATE_DELAY)

    update_broadcast_record(
        context,
        broadcast_id,
        status=BROADCAST_STATUS_SENT,
        completed_at=datetime.now(UTC).isoformat(),
        success_count=str(success),
        failed_count=str(failed),
    )


def _build_view_state(submissions: list[dict[str, str]]) -> dict[str, Any]:
    labels = _build_user_labels(submissions)
    lookup: dict[str, dict[str, str]] = {}
    for submission in submissions:
        session_key = submission.get("session_key")
        if session_key:
            lookup[session_key] = submission

    time_order = list(submissions)
    user_order = sorted(
        submissions,
        key=lambda item: (
            labels.get(item.get("user_id", ""), ""),
            -_timestamp_key(item.get("created_at", "")),
        ),
    )
    state: dict[str, Any] = {
        "mode": "time",
        "ordered_all": {"time": time_order, "user": user_order},
        "ordered": {},
        "indexes": {"time": 0, "user": 0},
        "labels": labels,
        "message_id": None,
        "empty_message_id": None,
        "filter_hide_reviewed": False,
        "lookup": lookup,
        "photo_indexes": {},
        "current_session_key": None,
    }
    _apply_filter(state)
    return state


def _apply_filter(state: dict[str, Any]) -> None:
    ordered_all_raw = state.get("ordered_all")
    if not isinstance(ordered_all_raw, dict):
        ordered_all_raw = {}
    hide_reviewed = state.get("filter_hide_reviewed", False)
    filtered: dict[str, list[dict[str, str]]] = {}

    for mode, items in ordered_all_raw.items():
        if not isinstance(items, list):
            continue
        if hide_reviewed:
            filtered_items = [
                item
                for item in items
                if isinstance(item, dict) and not item.get("reviewed_at")
            ]
        else:
            filtered_items = [item for item in items if isinstance(item, dict)]
        filtered[mode] = filtered_items

    state["ordered"] = filtered
    indexes = state.setdefault("indexes", {})
    for mode, items in filtered.items():
        if not items:
            indexes[mode] = 0
        else:
            current_index = indexes.get(mode, 0)
            indexes[mode] = max(0, min(current_index, len(items) - 1))


def _find_submission(state: dict[str, Any], session_key: str) -> dict[str, str] | None:
    lookup = state.get("lookup")
    if isinstance(lookup, dict):
        submission = lookup.get(session_key)
        if isinstance(submission, dict):
            return submission

    ordered_all = state.get("ordered_all", {})
    if isinstance(ordered_all, dict):
        for items in ordered_all.values():
            if not isinstance(items, list):
                continue
            for submission in items:
                if (
                    isinstance(submission, dict)
                    and submission.get("session_key") == session_key
                ):
                    return submission
    return None


def _build_user_labels(submissions: list[dict[str, str]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    numbered: list[str] = []
    for submission in submissions:
        user_id = submission.get("user_id", "")
        username = submission.get("username", "").strip()
        if not user_id or user_id in labels:
            continue
        if username:
            labels[user_id] = get_message(
                "admin.user_label_username", username=username
            )
        else:
            numbered.append(user_id)
    unique_numbered = list(dict.fromkeys(uid for uid in numbered if uid))

    def sort_key(value: str) -> tuple[int, str]:
        try:
            return (0, f"{int(value):010d}")
        except ValueError:
            return (1, value)

    for index, user_id in enumerate(sorted(unique_numbered, key=sort_key), start=1):
        labels[user_id] = get_message(
            "admin.user_label_numbered", number=index, user_id=user_id
        )
    return labels


def _timestamp_key(value: str) -> float:
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    return timestamp.timestamp()


def _format_timestamp(value: str) -> str:
    if not value:
        return get_message("general.placeholder")
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _build_caption(
    state: dict[str, Any],
    submission: dict[str, str],
    mode: str,
    index: int,
    photo_index: int,
    photo_total: int,
) -> str:
    total = len(state["ordered"].get(mode, []))
    mode_label = (
        get_message("admin.mode_time")
        if mode == "time"
        else get_message("admin.mode_user")
    )
    user_label = state["labels"].get(
        submission.get("user_id", ""), get_message("general.placeholder")
    )

    def field(name: str) -> str:
        return submission.get(name, "") or get_message("general.placeholder")

    if photo_total <= 0:
        photo_info = get_message("admin.photo_missing")
    else:
        normalized_index = max(0, min(photo_index, photo_total - 1)) + 1
        photo_info = get_message(
            "admin.photo_counter", current=normalized_index, total=photo_total
        )

    revoked_at = submission.get("revoked_at", "")
    if revoked_at:
        status = get_message(
            "admin.status_revoked",
            revoked_at=html.escape(_format_timestamp(revoked_at)),
        )
    else:
        reviewed_at = submission.get("reviewed_at", "")
        if reviewed_at:
            reviewer = submission.get("reviewed_by", "") or get_message(
                "general.placeholder"
            )
            status = get_message(
                "admin.status_reviewed",
                reviewed_at=html.escape(_format_timestamp(reviewed_at)),
                reviewed_by=html.escape(str(reviewer)),
            )
        else:
            status = get_message("admin.status_active")

    return get_message(
        "admin.view_caption",
        index=index + 1,
        total=total,
        mode=html.escape(mode_label),
        user_label=html.escape(user_label),
        created_at=html.escape(_format_timestamp(submission.get("created_at", ""))),
        status=html.escape(status),
        position=html.escape(field("position")),
        condition=html.escape(field("condition")),
        size=html.escape(field("size")),
        material=html.escape(field("material")),
        description=html.escape(field("description")),
        price=html.escape(field("price")),
        contacts=html.escape(field("contacts")),
        photo_count=html.escape(photo_info),
    )


def _build_keyboard(
    state: dict[str, Any],
    mode: str,
    index: int,
    submission: dict[str, str] | None,
    _photo_index: int = 0,
    photo_total: int = 0,
) -> InlineKeyboardMarkup:
    submissions = state.get("ordered", {}).get(mode, [])
    total = len(submissions)
    buttons: list[list[InlineKeyboardButton]] = []
    if total > 1:
        nav_row: list[InlineKeyboardButton] = []
        nav_row.append(
            InlineKeyboardButton("«", callback_data=f"admin_view:n:{mode}:prev")
        )
        nav_row.append(
            InlineKeyboardButton(
                get_message("list.page_indicator", current=index + 1, total=total),
                callback_data="admin_view:noop",
            )
        )
        nav_row.append(
            InlineKeyboardButton("»", callback_data=f"admin_view:n:{mode}:next")
        )
        buttons.append(nav_row)

    if submission is not None:
        session_key = submission.get("session_key", "")
        if session_key:
            reviewed = bool(submission.get("reviewed_at"))
            label_key = (
                "admin.review_button_clear" if reviewed else "admin.review_button_mark"
            )
            action = "clear" if reviewed else "set"
            buttons.append(
                [
                    InlineKeyboardButton(
                        get_message(label_key),
                        callback_data=f"admin_view:r:{action}:{session_key}",
                    )
                ]
            )
            if photo_total > 1:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            get_message("admin.photo_prev_button"),
                            callback_data=f"admin_app_photo_prev:{session_key}",
                        ),
                        InlineKeyboardButton(
                            get_message("admin.photo_next_button"),
                            callback_data=f"admin_app_photo_next:{session_key}",
                        ),
                    ]
                )

    filter_text = get_message("admin.filter_hide_reviewed")
    if state.get("filter_hide_reviewed"):
        filter_text = f"✓ {filter_text}"
    buttons.append(
        [
            InlineKeyboardButton(
                filter_text,
                callback_data="admin_view:f:toggle",
            )
        ]
    )

    mode_row = [
        InlineKeyboardButton(
            ("✓ " if mode == "time" else "") + get_message("admin.mode_time_button"),
            callback_data="admin_view:m:time",
        ),
        InlineKeyboardButton(
            ("✓ " if mode == "user" else "") + get_message("admin.mode_user_button"),
            callback_data="admin_view:m:user",
        ),
    ]
    buttons.append(mode_row)
    return InlineKeyboardMarkup(buttons)


def _photo_paths(submission: dict[str, str]) -> list[Path]:
    raw = submission.get("photos", "")
    paths: list[Path] = []
    for chunk in raw.split(","):
        candidate = chunk.strip()
        if candidate:
            paths.append(Path(candidate))
    return paths


def _available_photo_paths(submission: dict[str, str]) -> list[Path]:
    return [path for path in _photo_paths(submission) if path.exists()]


def _open_photo_stream(
    submission: dict[str, str],
    photo_index: int = 0,
    photo_paths: Sequence[Path] | None = None,
) -> BytesIO | Any:
    paths = (
        list(photo_paths)
        if photo_paths is not None
        else _available_photo_paths(submission)
    )
    if paths:
        if photo_index < 0:
            photo_index = 0
        if photo_index >= len(paths):
            photo_index = len(paths) - 1
        stream = paths[photo_index].open("rb")
        stream.seek(0)
        return stream
    placeholder = BytesIO(_PLACEHOLDER_PHOTO)
    placeholder.name = "placeholder.png"  # type: ignore[attr-defined]
    placeholder.seek(0)
    return placeholder


def _audience_label(audience: str) -> str:
    if audience == BROADCAST_AUDIENCE_RECENT:
        return get_message("admin.broadcast_audience_recent")
    return get_message("admin.broadcast_audience_all")


def _resolve_broadcast_recipients(
    context: ContextTypes.DEFAULT_TYPE, audience_key: str
) -> tuple[set[int], int, str]:
    recipients = recipients_for_audience(context, audience_key)
    count = len(recipients)
    return recipients, count, _audience_label(audience_key)


def _scheduled_sort_key(record: dict[str, str]) -> float:
    return _timestamp_key(record.get("scheduled_at", ""))


__all__ = [
    "cancel_admin_action",
    "confirm_broadcast",
    "execute_broadcast_job",
    "navigate_applications",
    "receive_admin_id",
    "receive_broadcast_message",
    "receive_broadcast_schedule",
    "receive_remove_admin_id",
    "show_broadcast_history",
    "show_admin_roster",
    "show_scheduled_broadcasts",
    "start_add_admin",
    "start_remove_admin",
    "start_broadcast",
    "view_all_applications",
    "choose_broadcast_audience",
    "handle_broadcast_decision",
    "navigate_application_photo_next",
    "navigate_application_photo_prev",
]


async def _render_admin_application(
    context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]
) -> None:
    mode = state.get("mode", "time")
    submissions = state.get("ordered", {}).get(mode, [])
    chat_id = state.get("chat_id")
    if chat_id is None:
        logger.error("Admin view state missing chat_id")
        return

    message_id = state.get("message_id")
    empty_message_id = state.get("empty_message_id")

    if not submissions:
        keyboard = _build_keyboard(state, mode, 0, None)
        text = get_message("admin.no_filtered_applications")
        state["current_session_key"] = None
        if message_id:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except TelegramError as exc:  # pragma: no cover - network errors
                if "message to delete not found" not in str(exc).lower():
                    logger.exception(
                        "Failed to delete application photo message %s", message_id
                    )
            state["message_id"] = None

        if empty_message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=empty_message_id,
                    text=text,
                    reply_markup=keyboard,
                )
            except BadRequest as exc:
                lowered = str(exc).lower()
                if "message is not modified" in lowered:
                    return
                if "message to edit not found" in lowered:
                    empty_message_id = None
                    state["empty_message_id"] = None
                else:
                    raise

        if not empty_message_id:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
            )
            state["empty_message_id"] = sent.message_id
        return

    indexes = state.setdefault("indexes", {})
    index = indexes.get(mode, 0)
    index = max(0, min(index, len(submissions) - 1))
    indexes[mode] = index
    submission = submissions[index]

    session_key = str(submission.get("session_key", ""))
    previous_session_key = state.get("current_session_key")
    state["current_session_key"] = session_key or None
    photo_indexes = state.setdefault("photo_indexes", {})
    if session_key and (
        session_key not in photo_indexes or session_key != previous_session_key
    ):
        photo_indexes[session_key] = 0
    photo_paths = _available_photo_paths(submission)
    photo_total = len(photo_paths)
    if session_key:
        photo_index = photo_indexes.get(session_key, 0)
    else:
        photo_index = 0
    if photo_total:
        photo_index %= photo_total
        if session_key:
            photo_indexes[session_key] = photo_index
    elif session_key:
        photo_indexes[session_key] = 0

    caption = _build_caption(state, submission, mode, index, photo_index, photo_total)
    keyboard = _build_keyboard(state, mode, index, submission, photo_index, photo_total)
    if empty_message_id:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=empty_message_id,
            )
        except TelegramError as exc:  # pragma: no cover - network errors
            if "message to delete not found" not in str(exc).lower():
                logger.exception(
                    "Failed to delete empty state message %s", empty_message_id
                )
        state["empty_message_id"] = None

    photo_stream = _open_photo_stream(submission, photo_index, photo_paths)
    try:
        if message_id:
            media = InputMediaPhoto(
                media=photo_stream, caption=caption, parse_mode="HTML"
            )
            try:
                await context.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=message_id,
                    media=media,
                    reply_markup=keyboard,
                )
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
                raise
        else:
            sent = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_stream,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            state["message_id"] = sent.message_id
    finally:
        try:
            photo_stream.close()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to close photo stream")
