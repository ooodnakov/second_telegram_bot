"""Application bootstrap for the Telegram bot."""

from __future__ import annotations

from bot.commands import (
    error_handler,
    help_command,
    list_applications,
    new,
    paginate_list,
    start,
)
from bot.config import create_valkey_client, load_config
from bot.constants import (
    CONDITION,
    CONTACTS,
    DESCRIPTION,
    MATERIAL,
    PHOTOS,
    POSITION,
    PRICE,
    SIZE,
    SKIP_KEYWORD_PATTERN,
)
from bot.logging import logger
from bot.workflow import (
    cancel,
    get_condition,
    get_contacts,
    get_description,
    get_material,
    get_photos,
    get_position,
    get_price,
    get_size,
    skip_photos,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)


def main() -> None:
    """Entry point used by ``python -m bot.main``."""

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
                MessageHandler(filters.Regex(SKIP_KEYWORD_PATTERN), skip_photos),
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
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_applications))
    app.add_handler(CallbackQueryHandler(paginate_list, pattern=r"^list:\d+:\d+$"))
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    logger.info("Handlers registered; starting polling")
    app.run_polling()


if __name__ == "__main__":
    main()
