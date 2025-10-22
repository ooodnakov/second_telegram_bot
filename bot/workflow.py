"""Conversation workflow handlers for the Telegram bot."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from bot.constants import (
    CONDITION,
    CONTACTS,
    DESCRIPTION,
    MATERIAL,
    PHOTOS,
    PRICE,
    SIZE,
    SKIP_KEYWORD,
    UTC,
)
from bot.logging import logger
from bot.messages import get_message
from bot.storage import get_application_store
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler
from valkey import Valkey


async def get_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Position handler called without message or user: {}", update)
        return ConversationHandler.END

    store = get_application_store(context)
    store.set_fields(user.id, position=update.message.text)
    logger.info(
        "User {} provided position: {}",
        user.id,
        update.message.text,
    )
    used_condition = get_message("workflow.condition_used")
    new_condition = get_message("workflow.condition_new")
    keyboard = [
        [
            InlineKeyboardButton(used_condition, callback_data="used"),
            InlineKeyboardButton(new_condition, callback_data="new"),
        ]
    ]
    await update.message.reply_text(
        get_message("workflow.condition_prompt"),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CONDITION


async def get_condition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.from_user is None:
        logger.warning("Condition handler invoked without callback query: {}", update)
        return ConversationHandler.END

    await query.answer()
    store = get_application_store(context)
    condition_map = {
        "used": get_message("workflow.condition_used"),
        "new": get_message("workflow.condition_new"),
    }
    condition_key = query.data
    condition_text = condition_map.get(condition_key, condition_key)
    store.set_fields(query.from_user.id, condition=condition_text, photos=[])
    logger.info(
        "User {} selected condition key {} (value: {})",
        query.from_user.id,
        condition_key,
        condition_text,
    )
    await query.edit_message_text(get_message("workflow.photos_initial_prompt"))
    return PHOTOS


async def get_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Photo handler called without message or user: {}", update)
        return ConversationHandler.END

    store = get_application_store(context)
    user_data = store.get(user.id)
    if not user_data:
        logger.warning("Photo handler could not locate session for user {}", user.id)
        await update.message.reply_text(get_message("general.session_missing"))
        return ConversationHandler.END

    if not update.message.photo:
        logger.warning("User {} sent non-photo message during photo step", user.id)
        await update.message.reply_text(get_message("general.photo_required"))
        return PHOTOS

    photo_file_id = update.message.photo[-1].file_id
    session_dir: Path | None = user_data.get("session_dir")
    if session_dir is None:
        logger.error("Session directory missing for user {}", user.id)
        await update.message.reply_text(get_message("general.photo_save_error"))
        return ConversationHandler.END

    photo_index = len(user_data["photos"]) + 1

    telegram_file = await context.bot.get_file(photo_file_id)
    file_suffix = Path(telegram_file.file_path or "").suffix or ".jpg"
    filename = f"photo_{photo_index:02d}{file_suffix}"
    saved_path = session_dir / filename
    await telegram_file.download_to_drive(custom_path=str(saved_path))
    logger.info(
        "Saved photo {} for user {} to {}",
        photo_index,
        user.id,
        saved_path,
    )

    photos = store.append_photo(user.id, saved_path)
    user_data["photos"] = photos

    count = len(photos)
    if count < 2:
        logger.debug("User {} has {} photos; requesting more", user.id, count)
        await _send_photo_prompt(
            update,
            context,
            user_data,
            get_message("workflow.photos_additional_required"),
        )
        return PHOTOS
    elif count < 5:
        logger.debug(
            "User {} has {} photos; prompting for optional uploads", user.id, count
        )
        await _send_photo_prompt(
            update,
            context,
            user_data,
            get_message(
                "workflow.photos_additional_optional",
                count=count,
                keyword=SKIP_KEYWORD,
            ),
        )
        return PHOTOS
    else:
        logger.debug("User {} reached maximum photo count", user.id)
        await _send_photo_prompt(
            update,
            context,
            user_data,
            get_message("workflow.photos_max_prompt"),
            parse_mode="Markdown",
        )
        return SIZE


async def skip_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(
        "User {} opted to skip additional photos",
        getattr(update.effective_user, "id", "unknown"),
    )
    await update.message.reply_text(
        get_message("workflow.size_prompt"), parse_mode="Markdown"
    )  # type: ignore[arg-type]
    return SIZE


async def get_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Size handler called without message or user: {}", update)
        return ConversationHandler.END

    store = get_application_store(context)
    store.set_fields(user.id, size=update.message.text)
    logger.info("User {} provided size: {}", user.id, update.message.text)
    await update.message.reply_text(
        get_message("workflow.material_prompt"), parse_mode="Markdown"
    )
    return MATERIAL


async def get_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Material handler called without message or user: {}", update)
        return ConversationHandler.END

    store = get_application_store(context)
    store.set_fields(user.id, material=update.message.text)
    logger.info("User {} provided material: {}", user.id, update.message.text)
    await update.message.reply_text(
        get_message("workflow.description_prompt"), parse_mode="Markdown"
    )
    return DESCRIPTION


async def get_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Description handler called without message or user: {}", update)
        return ConversationHandler.END

    store = get_application_store(context)
    store.set_fields(user.id, description=update.message.text)
    logger.info("User {} provided description", user.id)
    await update.message.reply_text(
        get_message("workflow.price_prompt"), parse_mode="Markdown"
    )
    return PRICE


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Price handler called without message or user: {}", update)
        return ConversationHandler.END

    store = get_application_store(context)
    store.set_fields(user.id, price=update.message.text)
    logger.info("User {} provided price: {}", user.id, update.message.text)
    await update.message.reply_text(
        get_message("workflow.contacts_prompt"), parse_mode="Markdown"
    )
    return CONTACTS


async def get_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Contacts handler called without message or user: {}", update)
        return ConversationHandler.END

    store = get_application_store(context)
    user_data = store.get(user.id)
    if not user_data:
        logger.warning("Contacts handler could not locate session for user {}", user.id)
        await update.message.reply_text(get_message("general.session_missing"))
        return ConversationHandler.END

    store.set_fields(user.id, contacts=update.message.text)
    user_data["contacts"] = update.message.text
    logger.info("User {} provided contacts", user.id)

    text_lines = [get_message("workflow.summary_header")]
    fields = [
        ("workflow.summary_position", user_data.get("position", "")),
        ("workflow.summary_condition", user_data.get("condition", "")),
        ("workflow.summary_size", user_data.get("size", "")),
        ("workflow.summary_material", user_data.get("material", "")),
        ("workflow.summary_description", user_data.get("description", "")),
        ("workflow.summary_price", user_data.get("price", "")),
        ("workflow.summary_contacts", update.message.text),
    ]
    for key, value in fields:
        text_lines.append(
            get_message(key, value=value or get_message("general.placeholder"))
        )
    text = "\n".join(text_lines)

    photos: list[Path] = [Path(photo) for photo in user_data.get("photos", [])]
    user_data["photos"] = photos
    logger.debug("User {} submission includes {} photos", user.id, len(photos))

    chat = update.effective_chat
    chat_id = chat.id if chat is not None else user.id
    await _send_submission_photos(context.bot, chat_id, photos)

    await update.message.reply_text(text, parse_mode="Markdown")

    _persist_application(update, context, user_data)
    await _forward_to_moderators(context, text, photos)
    await update.message.reply_text(
        get_message("workflow.submission_received"), parse_mode="Markdown"
    )
    logger.info("Submission for user {} processed successfully", user.id)
    store.clear(user.id)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(
        "User {} cancelled submission",
        getattr(update.effective_user, "id", "unknown"),
    )
    await update.message.reply_text(get_message("general.submission_cancelled"))  # type: ignore[arg-type]
    return ConversationHandler.END


async def _send_photo_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_data: dict[str, Any],
    text: str,
    parse_mode: str | None = None,
) -> None:
    previous_message_id = user_data.get("_photo_prompt_message_id")
    if isinstance(previous_message_id, str):
        try:
            previous_message_id = int(previous_message_id)
        except ValueError:
            previous_message_id = None

    if previous_message_id:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,  # type: ignore[arg-type]
                message_id=previous_message_id,  # type: ignore[arg-type]
            )
        except BadRequest:
            pass

    message = await update.message.reply_text(text, parse_mode=parse_mode)  # type: ignore[arg-type]
    user_data["_photo_prompt_message_id"] = message.message_id
    user = update.effective_user
    if user is not None:
        store = get_application_store(context)
        store.set_fields(user.id, _photo_prompt_message_id=message.message_id)
    logger.debug(
        "Sent photo prompt to user {} with message id {}",
        getattr(update.effective_user, "id", "unknown"),
        message.message_id,
    )


async def _forward_to_moderators(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    photos: list[Path],
) -> None:
    chat_ids: list[int] = context.application.bot_data.get("moderator_chat_ids") or []
    if not chat_ids:
        logger.debug("No moderator chat ids configured; skipping forwarding")
        return

    for chat_id in chat_ids:
        try:
            await _send_submission_photos(context.bot, chat_id, photos)
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info("Forwarded submission to moderator chat {}", chat_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to forward submission to moderator chat {}", chat_id
            )


async def _send_submission_photos(
    bot,
    chat_id: int,
    photos: list[Path],
) -> None:
    existing_photos = [photo_path for photo_path in photos if photo_path.exists()]
    if not existing_photos:
        logger.debug("No existing photos to send to chat {}", chat_id)
        return

    logger.debug(
        "Sending {} photos to chat {}",
        len(existing_photos),
        chat_id,
    )
    if len(existing_photos) > 1:
        media_group: list[InputMediaPhoto] = []
        open_files = []
        try:
            for photo_path in existing_photos:
                photo_file = photo_path.open("rb")
                photo_file.seek(0)
                open_files.append(photo_file)
                media_group.append(InputMediaPhoto(media=photo_file))
            try:
                await bot.send_media_group(chat_id=chat_id, media=media_group)
                logger.debug(
                    "Sent media group with {} photos to chat {}",
                    len(existing_photos),
                    chat_id,
                )
                return
            except BadRequest:
                logger.exception(
                    "Failed to send media group to chat {}; sending individually",
                    chat_id,
                )
                await _send_photos_individually(bot, chat_id, existing_photos)
        finally:
            for photo_file in open_files:
                photo_file.close()
    else:
        with existing_photos[0].open("rb") as photo_file:
            photo_file.seek(0)
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo_file,
            )
        logger.debug("Sent single photo to chat {}", chat_id)


async def _send_photos_individually(
    bot,
    chat_id: int,
    photos: list[Path],
) -> None:
    for photo_path in photos:
        if not photo_path.exists():
            logger.warning(
                "Skipping missing photo {} when sending to chat {}",
                photo_path,
                chat_id,
            )
            continue
        try:
            with photo_path.open("rb") as photo_file:
                photo_file.seek(0)
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_file,
                )
            logger.debug(
                "Sent fallback individual photo {} to chat {}", photo_path, chat_id
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to send individual photo {} to chat {}", photo_path, chat_id
            )


def _persist_application(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_data: dict[str, Any],
) -> None:
    valkey_client: Valkey | None = context.application.bot_data.get("valkey_client")
    if not valkey_client:
        logger.warning("Valkey client missing; skipping persistence")
        return

    prefix = context.application.bot_data.get("valkey_prefix", "telegram_auto_poster")
    user = update.effective_user
    if user is None:
        logger.warning("Effective user missing; skipping Valkey persistence")
        return

    session_key = user_data.get("session_key")
    session_dir: Path | None = user_data.get("session_dir")
    photos = [str(Path(photo_path)) for photo_path in user_data.get("photos", [])]

    record = {
        "session_key": session_key or uuid4().hex,
        "user_id": str(user.id),
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "position": user_data.get("position", ""),
        "condition": user_data.get("condition", ""),
        "size": user_data.get("size", ""),
        "material": user_data.get("material", ""),
        "description": user_data.get("description", ""),
        "price": user_data.get("price", ""),
        "contacts": user_data.get("contacts", ""),
        "photos": ",".join(photos),
        "session_dir": str(session_dir) if session_dir else "",
        "created_at": datetime.now(UTC).isoformat(),
    }

    valkey_key = f"{prefix}:{record['session_key']}"
    logger.debug(
        "Persisting submission for user {} under key {}",
        user.id,
        valkey_key,
    )

    try:
        valkey_client.hset(valkey_key, mapping=record)
        valkey_client.sadd(f"{prefix}:applications", valkey_key)
        logger.info(
            "Persisted submission for user {} under key {}",
            user.id,
            valkey_key,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist application data to Valkey")


__all__ = [
    "cancel",
    "get_condition",
    "get_contacts",
    "get_description",
    "get_material",
    "get_photos",
    "get_position",
    "get_price",
    "get_size",
    "skip_photos",
]
