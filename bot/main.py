import json
import os
from configparser import ConfigParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

try:
    from datetime import UTC
except ImportError:  # pragma: no cover - python <3.11 fallback
    UTC = timezone.utc  # type: ignore[assignment]

from bot.logger_setup import setup_logger

try:
    from zoneinfo import ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - python <3.11 fallback
    ZoneInfoNotFoundError = Exception  # type: ignore
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from valkey import Valkey
from valkey.exceptions import ConnectionError as ValkeyConnectionError


LIST_PAGE_SIZE = 5
try:
    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
    _MOSCOW_TZ_FALLBACK = False
except ZoneInfoNotFoundError:  # pragma: no cover - depends on system tzdata
    MOSCOW_TZ = timezone(timedelta(hours=3))
    _MOSCOW_TZ_FALLBACK = True
logger = setup_logger()
if _MOSCOW_TZ_FALLBACK:
    logger.warning(
        "Timezone data for Europe/Moscow not found; falling back to UTC+3 offset."
    )


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
    logger.debug("Loading configuration from {}", config_path)

    parser = ConfigParser()
    if not parser.read(config_path, encoding="utf-8"):
        logger.error("Config file not found or unreadable: {}", config_path)
        raise RuntimeError(f"Config file not found or unreadable: {config_path}")

    if not parser.has_section(CONFIG_SECTION):
        logger.error(
            "Section '{}' missing in config file {}", CONFIG_SECTION, config_path
        )
        raise RuntimeError(f"Section '{CONFIG_SECTION}' missing in config file.")

    token = parser.get(CONFIG_SECTION, "token", fallback="").strip()
    if not token:
        logger.error("Telegram bot token is missing in the config file {}", config_path)
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
                logger.error("Invalid moderator chat id encountered: {}", candidate)
                raise RuntimeError(f"Invalid moderator chat id: {candidate!r}") from exc

    if not parser.has_section(VALKEY_CONFIG_SECTION):
        logger.error(
            "Section '{}' missing in config file {}", VALKEY_CONFIG_SECTION, config_path
        )
        raise RuntimeError(f"Section '{VALKEY_CONFIG_SECTION}' missing in config file.")

    host = parser.get(VALKEY_CONFIG_SECTION, "valkey_host", fallback="").strip()
    if not host:
        logger.error("Valkey host is missing in the config file {}", config_path)
        raise RuntimeError("Valkey host is missing in the config file.")

    try:
        port = parser.getint(VALKEY_CONFIG_SECTION, "valkey_port")
    except ValueError as exc:
        logger.error("Valkey port must be an integer in {}", config_path)
        raise RuntimeError("Valkey port must be an integer.") from exc

    password = parser.get(VALKEY_CONFIG_SECTION, "valkey_pass", fallback="").strip()
    prefix = (
        parser.get(
            VALKEY_CONFIG_SECTION, "redis_prefix", fallback="telegram_auto_poster"
        ).strip()
        or "telegram_auto_poster"
    )

    logger.info(
        "Configuration loaded for {} moderators and Valkey host {}:{} with prefix '{}'",
        len(moderators),
        host,
        port,
        prefix,
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
        session = self._deserialize(raw)  # type: ignore
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
        logger.warning("Received /start update without message or user: {}", update)
        return ConversationHandler.END

    logger.info("User {} invoked /start", user.id)
    await update.message.reply_text("Привет! Добро пожаловать.")
    await update.message.reply_text(
        "Чтобы отправить новую заявку, используйте команду /new."
    )
    return ConversationHandler.END


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Received /new update without message or user: {}", update)
        return ConversationHandler.END

    try:
        store = _get_application_store(context)
    except RuntimeError as exc:
        logger.exception("Failed to obtain application store for user %s", user.id)
        await update.message.reply_text(
            "Хранилище недоступно, попробуйте позже или обратитесь к администратору."
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "Введите *название позиции:*",
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
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Received /help update without message or user: {}", update)
        return ConversationHandler.END

    logger.info("User {} requested help", user.id)
    await update.message.reply_text(
        "Доступные команды:\n"
        "/start — приветствие и краткая инструкция.\n"
        "/new — отправить новую заявку.\n"
        "/list — показать отправленные заявки.\n"
        "/cancel — отменить текущую заявку.",
    )
    return ConversationHandler.END


def _format_created_at(value: str) -> str:
    if not value:
        return "—"
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _fetch_user_submissions(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> list[dict[str, str]] | None:
    valkey_client = context.application.bot_data.get("valkey_client")
    if valkey_client is None:
        logger.warning(
            "Valkey client not configured; cannot fetch submissions for user {}",
            user_id,
        )
        return None

    prefix = context.application.bot_data.get("valkey_prefix", "telegram_auto_poster")
    applications_key = f"{prefix}:applications"

    try:
        raw_keys = valkey_client.smembers(applications_key)  # type: ignore[attr-defined]
    except Exception:
        logger.exception("Failed to fetch application list from Valkey")
        return None

    if not raw_keys:
        logger.info("No submissions stored for user {}", user_id)
        return []

    submissions: list[dict[str, str]] = []
    for raw_key in raw_keys:
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        try:
            raw_data = valkey_client.hgetall(key)
        except Exception:
            logger.exception("Failed to fetch application data for key {}", key)
            continue
        if not raw_data:
            continue

        record: dict[str, str] = {}
        for raw_field, raw_value in raw_data.items():
            field = raw_field.decode() if isinstance(raw_field, bytes) else raw_field
            value = raw_value.decode() if isinstance(raw_value, bytes) else raw_value
            record[field] = value

        if record.get("user_id") != str(user_id):
            continue
        submissions.append(record)

    submissions.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    logger.info(
        "Fetched {} submissions for user {} from Valkey",
        len(submissions),
        user_id,
    )
    return submissions


def _render_applications_page(
    submissions: list[dict[str, str]],
    page: int,
    user_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    total = len(submissions)
    if total == 0:
        logger.debug("Rendering empty applications page for user {}", user_id)
        return ("У вас пока нет отправленных заявок.", None)

    total_pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start_index = page * LIST_PAGE_SIZE
    end_index = start_index + LIST_PAGE_SIZE
    page_entries = submissions[start_index:end_index]

    lines: list[str] = []
    for offset, record in enumerate(page_entries, start=1):
        ordinal = start_index + offset
        created_at = _format_created_at(record.get("created_at", ""))
        position = record.get("position", "—")
        condition = record.get("condition", "—")
        price = record.get("price", "—")
        contacts = record.get("contacts", "—")
        lines.append(
            f"{ordinal}. {position} ({condition}) — {price}\n"
            f"   Контакты: {contacts}\n"
            f"   Отправлено: {created_at}"
        )

    header = f"Ваши заявки (страница {page + 1} из {total_pages}, всего {total})"
    text = "\n\n".join([header, *lines])

    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton("← Назад", callback_data=f"list:{page - 1}:{user_id}")
        )
    if page < total_pages - 1:
        buttons.append(
            InlineKeyboardButton("Вперёд →", callback_data=f"list:{page + 1}:{user_id}")
        )

    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
    logger.debug(
        "Rendered applications page {} for user {} ({} total submissions)",
        page + 1,
        user_id,
        total,
    )
    return text, keyboard


async def list_applications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Received /list update without message or user: {}", update)
        return ConversationHandler.END

    logger.info("User {} requested submission list", user.id)
    submissions = _fetch_user_submissions(context, user.id)
    if submissions is None:
        await update.message.reply_text("Хранилище недоступно, попробуйте позже.")
        logger.error("Valkey unavailable when fetching list for user {}", user.id)
        return ConversationHandler.END
    if not submissions:
        await update.message.reply_text("У вас пока нет отправленных заявок.")
        logger.info("User {} has no submissions stored", user.id)
        return ConversationHandler.END

    context.user_data["list_submissions"] = submissions  # type: ignore
    text, keyboard = _render_applications_page(submissions, 0, user.id)
    await update.message.reply_text(text, reply_markup=keyboard)
    logger.debug("Displayed submissions page 1 for user {}", user.id)
    return ConversationHandler.END


async def paginate_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await query.answer("Эта навигация недоступна.", show_alert=True)
        logger.warning(
            "User {} attempted to paginate submissions for user {}",
            getattr(user, "id", "unknown"),
            expected_user_id,
        )
        return

    submissions = context.user_data.get("list_submissions")  # type: ignore
    if not isinstance(submissions, list):
        submissions = _fetch_user_submissions(context, user.id)

    await query.answer()

    if submissions is None:
        await query.edit_message_text("Хранилище недоступно, попробуйте позже.")
        logger.error("Valkey unavailable during pagination for user {}", user.id)
        return
    if not submissions:
        await query.edit_message_text("У вас пока нет отправленных заявок.")
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


async def get_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Position handler called without message or user: {}", update)
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, position=update.message.text)
    logger.info(
        "User {} provided position: {}",
        user.id,
        update.message.text,
    )
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
    if query is None or query.from_user is None:
        logger.warning("Condition handler invoked without callback query: {}", update)
        return ConversationHandler.END

    await query.answer()
    store = _get_application_store(context)
    store.set_fields(query.from_user.id, condition=query.data, photos=[])
    logger.info(
        "User {} selected condition {}",
        query.from_user.id,
        query.data,
    )
    await query.edit_message_text("Отправьте 2–5 фото позиции")
    return PHOTOS


async def get_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Photo handler called without message or user: {}", update)
        return ConversationHandler.END

    store = _get_application_store(context)
    user_data = store.get(user.id)
    if not user_data:
        logger.warning("Photo handler could not locate session for user {}", user.id)
        await update.message.reply_text("Сессия не найдена, отправьте /new")
        return ConversationHandler.END

    if not update.message.photo:
        logger.warning("User {} sent non-photo message during photo step", user.id)
        await update.message.reply_text("Пожалуйста, отправьте фото позиции")
        return PHOTOS

    photo_file_id = update.message.photo[-1].file_id
    session_dir: Path | None = user_data.get("session_dir")
    if session_dir is None:
        logger.error("Session directory missing for user {}", user.id)
        await update.message.reply_text("Ошибка сохранения фото, отправьте /new")
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
            "Фото получено. Ещё минимум одно фото.",
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
            f"Получено {count} фото. Если хотите добавить ещё — пришлите. "
            "Когда хватит, напишите 'далее'.",
        )
        return PHOTOS
    else:
        logger.debug("User {} reached maximum photo count", user.id)
        await _send_photo_prompt(
            update,
            context,
            user_data,
            "Максимум 5 фото получено. Теперь введите *размер:*",
            parse_mode="Markdown",
        )
        return SIZE


async def skip_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(
        "User {} opted to skip additional photos",
        getattr(update.effective_user, "id", "unknown"),
    )
    await update.message.reply_text("Введите *размер:*", parse_mode="Markdown")  # type: ignore
    return SIZE


async def get_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Size handler called without message or user: {}", update)
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, size=update.message.text)
    logger.info("User {} provided size: {}", user.id, update.message.text)
    await update.message.reply_text("Введите *материал:*", parse_mode="Markdown")
    return MATERIAL


async def get_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Material handler called without message or user: {}", update)
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, material=update.message.text)
    logger.info("User {} provided material: {}", user.id, update.message.text)
    await update.message.reply_text(
        "Введите *короткое описание:*", parse_mode="Markdown"
    )
    return DESCRIPTION


async def get_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Description handler called without message or user: {}", update)
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, description=update.message.text)
    logger.info("User {} provided description", user.id)
    await update.message.reply_text("Введите *цену:*", parse_mode="Markdown")
    return PRICE


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Price handler called without message or user: {}", update)
        return ConversationHandler.END

    store = _get_application_store(context)
    store.set_fields(user.id, price=update.message.text)
    logger.info("User {} provided price: {}", user.id, update.message.text)
    await update.message.reply_text(
        "Теперь оставьте ваши контакты (телефон, @username и т.д.)"
    )
    return CONTACTS


async def get_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        logger.warning("Contacts handler called without message or user: {}", update)
        return ConversationHandler.END

    store = _get_application_store(context)
    user_id = user.id
    store.set_fields(user_id, contacts=update.message.text)
    logger.info("User {} provided contact details", user_id)
    user_data = store.get(user_id)
    if not user_data:
        logger.error("Submission data missing when finalizing for user {}", user_id)
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
    logger.debug(
        "User {} submission includes {} photos",
        user_id,
        len(photos),
    )
    await _send_submission_photos(
        context.bot,
        update.effective_chat.id if update.effective_chat else user_id,
        photos,
    )

    await update.message.reply_text(text, parse_mode="Markdown")
    logger.info(
        "Persisting submission for user {} with session {}",
        user_id,
        user_data.get("session_key"),
    )
    _persist_application(update, context, user_data)
    await _forward_to_moderators(context, text, photos)
    await update.message.reply_text("Заявка успешно отправлена! Спасибо.")
    logger.info("Submission for user {} processed successfully", user_id)
    store.clear(user_id)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(
        "User {} cancelled submission",
        getattr(update.effective_user, "id", "unknown"),
    )
    await update.message.reply_text("Заявка отменена")  # type: ignore
    return ConversationHandler.END


def main():
    logger.info("Starting Telegram bot initialization")
    try:
        config = load_config()
    except RuntimeError as exc:
        raise SystemExit(f"Failed to load configuration: {exc}") from exc

    app = ApplicationBuilder().token(config["token"]).build()
    logger.debug("Application builder created")

    valkey_client = create_valkey_client(config)
    app.bot_data["valkey_client"] = valkey_client
    app.bot_data["valkey_prefix"] = config["valkey"]["prefix"]
    app.bot_data["moderator_chat_ids"] = config.get("moderator_chat_ids", [])
    logger.debug(
        "Bot data configured with Valkey prefix {} and {} moderator chats",
        config["valkey"]["prefix"],
        len(app.bot_data["moderator_chat_ids"]),
    )

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
        per_message=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_applications))
    app.add_handler(CallbackQueryHandler(paginate_list, pattern=r"^list:\\d+:\\d+$"))
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    logger.info("Handlers registered; starting polling")
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
                chat_id=update.effective_chat.id,  # type: ignore
                message_id=previous_message_id,  # type: ignore
            )
        except BadRequest:
            pass

    message = await update.message.reply_text(text, parse_mode=parse_mode)  # type: ignore
    user_data["_photo_prompt_message_id"] = message.message_id
    user = update.effective_user
    if user is not None:
        store = _get_application_store(context)
        store.set_fields(user.id, _photo_prompt_message_id=message.message_id)
    logger.debug(
        "Sent photo prompt to user {} with message id {}",
        getattr(update.effective_user, "id", "unknown"),
        message.message_id,
    )


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
                media_group.append(
                    InputMediaPhoto(
                        media=photo_file,
                    )
                )
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


def _get_application_store(context: ContextTypes.DEFAULT_TYPE) -> ApplicationStore:
    client = context.application.bot_data.get("valkey_client")
    if client is None:
        raise RuntimeError("Valkey client is not configured")
    prefix = context.application.bot_data.get("valkey_prefix", "telegram_auto_poster")
    return ApplicationStore(client, prefix)


if __name__ == "__main__":
    main()
