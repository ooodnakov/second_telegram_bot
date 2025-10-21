import os
from configparser import ConfigParser
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# Этапы диалога
(POSITION, CONDITION, PHOTOS, SIZE, MATERIAL, DESCRIPTION, PRICE, CONTACTS) = range(8)

# Временное хранилище заявок
applications = {}

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.ini"
CONFIG_SECTION = "telegram"


def load_config(path: str | Path | None = None) -> dict[str, str]:
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

    return {"token": token}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь заявку.\n\nВведите *название позиции:*",
        parse_mode="Markdown",
    )
    applications[update.message.from_user.id] = {}
    return POSITION


async def get_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = applications[update.message.from_user.id]
    user_data["position"] = update.message.text
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
    user_data = applications[query.from_user.id]
    user_data["condition"] = query.data
    await query.edit_message_text("Отправьте 2–5 фото позиции")
    user_data["photos"] = []
    return PHOTOS


async def get_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = applications[update.message.from_user.id]
    photo_file_id = update.message.photo[-1].file_id
    user_data["photos"].append(photo_file_id)

    count = len(user_data["photos"])
    if count < 2:
        await update.message.reply_text("Фото получено. Ещё минимум одно фото.")
        return PHOTOS
    elif count < 5:
        await update.message.reply_text(
            f"Получено {count} фото. Если хотите добавить ещё — пришлите. "
            "Когда хватит, напишите 'далее'."
        )
        return PHOTOS
    else:
        await update.message.reply_text(
            "Максимум 5 фото получено. Теперь введите *размер:*", parse_mode="Markdown"
        )
        return SIZE


async def skip_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите *размер:*", parse_mode="Markdown")
    return SIZE


async def get_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = applications[update.message.from_user.id]
    user_data["size"] = update.message.text
    await update.message.reply_text("Введите *материал:*", parse_mode="Markdown")
    return MATERIAL


async def get_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = applications[update.message.from_user.id]
    user_data["material"] = update.message.text
    await update.message.reply_text(
        "Введите *короткое описание:*", parse_mode="Markdown"
    )
    return DESCRIPTION


async def get_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = applications[update.message.from_user.id]
    user_data["description"] = update.message.text
    await update.message.reply_text("Введите *цену:*", parse_mode="Markdown")
    return PRICE


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = applications[update.message.from_user.id]
    user_data["price"] = update.message.text
    await update.message.reply_text(
        "Теперь оставьте ваши контакты (телефон, @username и т.д.)"
    )
    return CONTACTS


async def get_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = applications[update.message.from_user.id]
    user_data["contacts"] = update.message.text

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

    # Отправим все фото + текст пользователю (или админу)
    media_group = [user_data["photos"][0]]
    for photo_id in user_data["photos"]:
        await update.message.bot.send_photo(
            chat_id=update.message.chat_id, photo=photo_id
        )

    await update.message.reply_text(text, parse_mode="Markdown")
    await update.message.reply_text("Заявка успешно отправлена! Спасибо.")
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

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_position)],
            CONDITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_condition)],
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

    app.add_handler(conv_handler)
    app.run_polling()


if __name__ == "__main__":
    main()
