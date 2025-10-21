import json
import os
from configparser import ConfigParser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

from valkey import Valkey
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from bot.logger_setup import setup_logger


logger = setup_logger()


# Этапы диалога
(POSITION, CONDITION, PHOTOS, SIZE, MATERIAL, DESCRIPTION, PRICE, CONTACTS) = range(8)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.ini"
CONFIG_SECTION = "telegram"
VALKEY_CONFIG_SECTION = "valkey"
MEDIA_ROOT = Path(__file__).resolve().parent.parent / "media"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load bot configuration from ini file."""
    config_source = path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    config_path = Path(config_source).expanduser()

    parser = ConfigParser()
    if not parser.read(config_path, encoding="utf-8"):
        raise RuntimeError(f"Config file not found or unreadable: {config_path}")

    if not parser.has_section(CONFIG_SECTION):
        raise RuntimeError(f"Section '{CONFIG_SECTION}' missing in config file.")

    token = parser.get(CONFIG_SECTION, "token", fallback="").strip()
    if not token:
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
                raise RuntimeError(f"Invalid moderator chat id: {candidate!r}") from exc

    if not parser.has_section(VALKEY_CONFIG_SECTION):
        raise RuntimeError(f"Section '{VALKEY_CONFIG_SECTION}' missing in config file.")

    host = parser.get(VALKEY_CONFIG_SECTION, "valkey_host", fallback="").strip()
    if not host:
        raise RuntimeError("Valkey host is missing in the config file.")

    try:
        port = parser.getint(VALKEY_CONFIG_SECTION, "valkey_port")
    except ValueError as exc:
        raise RuntimeError("Valkey port must be an integer.") from exc

    password = parser.get(VALKEY_CONFIG_SECTION, "valkey_pass", fallback="").strip()
    prefix = (
        parser.get(
            VALKEY_CONFIG_SECTION, "redis_prefix", fallback="telegram_auto_poster"
        ).strip()
        or "telegram_auto_poster"
    )

    return {
        "token": token,
        "moderator_chat_ids": moderators,
        "valkey": {
            "host": host,
            "port": port,
            "password": password or None,
            "prefix": prefix,
        },
    }


class InMemoryValkey:
    """Fallback store that mimics Valkey calls used by the bot."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set[str]] = {}

    def hset(self, name: str, mapping: dict[str, str]) -> None:
        self._hashes.setdefault(name, {}).update(mapping)

    def hgetall(self, name: str) -> dict[str, str]:
        return self._hashes.get(name, {}).copy()

    def sadd(self, name: str, key: str) -> None:
        self._sets.setdefault(name, set()).add(key)

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

    def set_fields(self, user_id: int, **fields: Any) -> None:
        if not fields:
            return
        key = self._session_key(user_id)
        self._client.hset(key, mapping=self._serialize(fields))

    def append_photo(self, user_id: int, photo_path: Path) -> list[Path]:
        session = self.get(user_id)
        photos = session.get("photos", [])
        photos.append(photo_path)
        self.set_fields(user_id, photos=photos)
        return photos

    def get(self, user_id: int) -> dict[str, Any]:
        key = self._session_key(user_id)
        raw = self._client.hgetall(key)
        if not raw:
            return {}
        session = self._deserialize(raw)
        session.setdefault("photos", [])
        return session

    def clear(self, user_id: int) -> None:
        self._client.delete(self._session_key(user_id))

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

    def _deserialize(self, data: dict[str, Any]) -> dict[str, Any]:
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


def create_valkey_client(settings: dict[str, Any]) -> Valkey | InMemoryValkey:
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
    except Exception:  # noqa: BLE001
        logger.exception("Valkey connection failed. Falling back to in-memory store")
        return InMemoryValkey()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    await update.message.reply_text("Привет! Добро пожаловать.")
    await update.message.reply_text(
        "Чтобы отправить новую заявку, используйте команду /new."
    )
    return ConversationHandler.END


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    store = _get_application_store(context)
    await update.message.reply_text(
        "Введите *название позиции:*",
        parse_mode="Markdown",
    )
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    session_dir = MEDIA_ROOT / f"{user.id}_{timestamp}_{uuid4().hex[:6]}"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_key = session_dir.name
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
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    await update.message.reply_text(
        "Доступные команды:\n"
        "/start — приветствие и краткая инструкция.\n"
        "/new — отправить новую заявку.\n"
        "/cancel — отменить текущую заявку.",
    )
    return ConversationHandler.END


async def get_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, position=update.message.text)
    keyboard = [
        [
            InlineKeyboardButton("Б/У", callback_data="Б/У"),
            InlineKeyboardButton("Новое", callback_data="Новое"),
        ]
    ]
    await update.message.reply_text(
        "Выберите состояние:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONDITION


async def get_condition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    store = _get_application_store(context)
    store.set_fields(query.from_user.id, condition=query.data, photos=[])
    await query.edit_message_text("Отправьте 2–5 фото позиции")
    return PHOTOS


async def get_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    store = _get_application_store(context)
    user_data = store.get(user.id)
    if not user_data:
        await update.message.reply_text("Сессия не найдена, отправьте /new")
        return ConversationHandler.END

    photo_file_id = update.message.photo[-1].file_id
    session_dir: Path | None = user_data.get("session_dir")
    if session_dir is None:
        await update.message.reply_text("Ошибка сохранения фото, отправьте /new")
        return ConversationHandler.END

    photo_index = len(user_data["photos"]) + 1

    telegram_file = await context.bot.get_file(photo_file_id)
    file_suffix = Path(telegram_file.file_path or "").suffix or ".jpg"
    filename = f"photo_{photo_index:02d}{file_suffix}"
    saved_path = session_dir / filename
    await telegram_file.download_to_drive(custom_path=str(saved_path))

    photos = store.append_photo(user.id, saved_path)
    user_data["photos"] = photos

    count = len(photos)
    if count < 2:
        await _send_photo_prompt(
            update,
            context,
            user_data,
            "Фото получено. Ещё минимум одно фото.",
        )
        return PHOTOS
    elif count < 5:
        await _send_photo_prompt(
            update,
            context,
            user_data,
            f"Получено {count} фото. Если хотите добавить ещё — пришлите. "
            "Когда хватит, напишите 'далее'.",
        )
        return PHOTOS
    else:
        await _send_photo_prompt(
            update,
            context,
            user_data,
            "Максимум 5 фото получено. Теперь введите *размер:*",
            parse_mode="Markdown",
        )
        return SIZE


async def skip_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите *размер:*", parse_mode="Markdown")
    return SIZE


async def get_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, size=update.message.text)
    await update.message.reply_text("Введите *материал:*", parse_mode="Markdown")
    return MATERIAL


async def get_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, material=update.message.text)
    await update.message.reply_text(
        "Введите *короткое описание:*", parse_mode="Markdown"
    )
    return DESCRIPTION


async def get_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, description=update.message.text)
    await update.message.reply_text("Введите *цену:*", parse_mode="Markdown")
    return PRICE


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, price=update.message.text)
    await update.message.reply_text(
        "Теперь оставьте ваши контакты (телефон, @username и т.д.)"
    )
    return CONTACTS


async def get_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END

    store = _get_application_store(context)
    user_id = user.id
    store.set_fields(user_id, contacts=update.message.text)
    user_data = store.get(user_id)
    if not user_data:
        await update.message.reply_text("Сессия не найдена, отправьте /new")
        return ConversationHandler.END

    text = (
        f"*Новая заявка:*\n\n"
        f"Позиция: {user_data['position']}\n"
        f"Состояние: {user_data['condition']}\n"
        f"Размер: {user_data['size']}\n"
        f"Материал: {user_data['material']}\n"
        f"Описание: {user_data['description']}\n"
        f"Цена: {user_data['price']}\n"
        f"Контакты: {user_data['contacts']}"
    )

    photos = [Path(photo_path) for photo_path in user_data["photos"]]
    await _send_submission_photos(
        context.bot,
        update.effective_chat.id if update.effective_chat else user_id,
        photos,
    )

    await update.message.reply_text(text, parse_mode="Markdown")
    _persist_application(update, context, user_data)
    await _forward_to_moderators(context, text, photos)
    await update.message.reply_text("Заявка успешно отправлена! Спасибо.")
    store.clear(user_id)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Заявка отменена")
    return ConversationHandler.END


def main():
    try:
        config = load_config()
    except RuntimeError as exc:
        raise SystemExit(f"Failed to load configuration: {exc}") from exc

    app = ApplicationBuilder().token(config["token"]).build()

    valkey_client = create_valkey_client(config)
    app.bot_data["valkey_client"] = valkey_client
    app.bot_data["valkey_prefix"] = config["valkey"]["prefix"]
    app.bot_data["moderator_chat_ids"] = config.get("moderator_chat_ids", [])

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("new", new)],
        states={
            POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_position)],
            CONDITION: [CallbackQueryHandler(get_condition)],
            PHOTOS: [
                MessageHandler(filters.PHOTO, get_photos),
                MessageHandler(filters.Regex("(?i)^далее$"), skip_photos),
            ],
            SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_size)],
            MATERIAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_material)],
            DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_description)
            ],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_price)],
            CONTACTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contacts)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    app.run_polling()


async def _send_photo_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_data: dict,
    text: str,
    parse_mode: str | None = None,
):
    previous_message_id = user_data.get("_photo_prompt_message_id")
    if isinstance(previous_message_id, str):
        try:
            previous_message_id = int(previous_message_id)
        except ValueError:
            previous_message_id = None

    if previous_message_id:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id, message_id=previous_message_id
            )
        except BadRequest:
            pass

    message = await update.message.reply_text(text, parse_mode=parse_mode)
    user_data["_photo_prompt_message_id"] = message.message_id
    user = update.effective_user
    if user is not None:
        store = _get_application_store(context)
        store.set_fields(user.id, _photo_prompt_message_id=message.message_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.opt(exception=context.error).error(
        "Unhandled exception during update processing for update: {}",
        update,
    )


async def _send_submission_photos(
    bot,
    chat_id: int,
    photos: list[Path],
) -> None:
    existing_photos = [photo_path for photo_path in photos if photo_path.exists()]
    if not existing_photos:
        return

    if len(existing_photos) > 1:
        media_group: list[InputMediaPhoto] = []
        open_files = []
        try:
            for photo_path in existing_photos:
                photo_file = photo_path.open("rb")
                photo_file.seek(0)
                open_files.append(photo_file)
                media_group.append(
                    InputMediaPhoto(
                        media=photo_file,
                    )
                )
            try:
                await bot.send_media_group(chat_id=chat_id, media=media_group)
                return
            except BadRequest:
                logger.exception(
                    "Failed to send media group to chat %s; sending individually",
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


async def _send_photos_individually(
    bot,
    chat_id: int,
    photos: list[Path],
) -> None:
    for photo_path in photos:
        if not photo_path.exists():
            logger.warning(
                "Skipping missing photo %s when sending to chat %s",
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
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to send individual photo %s to chat %s", photo_path, chat_id
            )


async def _forward_to_moderators(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    photos: list[Path],
) -> None:
    chat_ids: list[int] = context.application.bot_data.get("moderator_chat_ids") or []
    if not chat_ids:
        return

    for chat_id in chat_ids:
        try:
            await _send_submission_photos(context.bot, chat_id, photos)
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to forward submission to moderator chat %s", chat_id
            )


def _persist_application(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_data: dict[str, Any],
) -> None:
    valkey_client: Valkey | None = context.application.bot_data.get("valkey_client")
    if not valkey_client:
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

    try:
        valkey_client.hset(valkey_key, mapping=record)
        valkey_client.sadd(f"{prefix}:applications", valkey_key)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist application data to Valkey")


def _get_application_store(context: ContextTypes.DEFAULT_TYPE) -> ApplicationStore:
    client = context.application.bot_data.get("valkey_client")
    if client is None:
        raise RuntimeError("Valkey client is not configured")
    prefix = context.application.bot_data.get("valkey_prefix", "telegram_auto_poster")
    return ApplicationStore(client, prefix)


if __name__ == "__main__":
    main()
