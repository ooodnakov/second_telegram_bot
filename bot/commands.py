"""Command handlers for the Telegram bot."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from bot.admin import (
    fetch_user_submissions,
    is_admin,
    is_super_admin,
    record_active_user,
)
from bot.constants import LIST_PAGE_SIZE, MEDIA_ROOT, MOSCOW_TZ, POSITION, UTC
from bot.logging import logger
from bot.messages import get_message
from bot.storage import get_application_store
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler


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
    await update.message.reply_text(text, parse_mode="Markdown")
    return ConversationHandler.END


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

    context.user_data["list_submissions"] = submissions  # type: ignore[index]
    text, keyboard = _render_applications_page(submissions, 0, user.id)
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
        _, page_raw, user_id_raw = data.split(":", 2)
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

    submissions = context.user_data.get("list_submissions")  # type: ignore[index]
    if not isinstance(submissions, list):
        submissions = fetch_user_submissions(context, user.id)

    await query.answer()

    if submissions is None:
        await query.edit_message_text(get_message("general.storage_unavailable"))
        logger.error("Valkey unavailable during pagination for user {}", user.id)
        return
    if not submissions:
        await query.edit_message_text(get_message("general.no_submissions"))
        logger.info("User {} has no submissions to paginate", user.id)
        return

    text, keyboard = _render_applications_page(submissions, page, user.id)
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
    logger.debug("User {} navigated to submissions page {}", user.id, page + 1)


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
) -> tuple[str, InlineKeyboardMarkup | None]:
    total = len(submissions)
    if total == 0:
        return get_message("general.no_submissions"), None

    start_index = page * LIST_PAGE_SIZE
    end_index = start_index + LIST_PAGE_SIZE
    page_items = submissions[start_index:end_index]

    lines = [get_message("list.title")]
    for index, submission in enumerate(page_items, start=start_index + 1):
        lines.append(
            get_message(
                "list.entry_format",
                index=index,
                position=submission.get("position", get_message("general.placeholder")),
                created_at=_format_created_at(submission.get("created_at", "")),
            )
        )
    text = "\n".join(lines)

    total_pages = (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
    if total_pages <= 1:
        return text, None

    prev_page = max(0, page - 1)
    next_page = min(total_pages - 1, page + 1)
    keyboard: list[list[InlineKeyboardButton]] = []

    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(
            InlineKeyboardButton(
                "«",
                callback_data=f"list:{prev_page}:{user_id}",
            )
        )
    row.append(
        InlineKeyboardButton(
            get_message("list.page_indicator", current=page + 1, total=total_pages),
            callback_data=f"list:{page}:{user_id}",
        )
    )
    if page < total_pages - 1:
        row.append(
            InlineKeyboardButton(
                "»",
                callback_data=f"list:{next_page}:{user_id}",
            )
        )

    keyboard.append(row)
    return text, InlineKeyboardMarkup(keyboard)


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


__all__ = [
    "error_handler",
    "help_command",
    "list_applications",
    "new",
    "paginate_list",
    "start",
]
