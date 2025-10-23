"""Application bootstrap for the Telegram bot."""

from __future__ import annotations

from bot.admin_commands import (
    cancel_admin_action,
    choose_broadcast_audience,
    confirm_broadcast,
    handle_broadcast_decision,
    navigate_application_photo_next,
    navigate_application_photo_prev,
    navigate_applications,
    receive_admin_id,
    receive_broadcast_message,
    receive_broadcast_schedule,
    receive_remove_admin_id,
    show_admin_roster,
    show_broadcast_history,
    show_scheduled_broadcasts,
    start_add_admin,
    start_broadcast,
    start_remove_admin,
    view_all_applications,
)
from bot.commands import (
    error_handler,
    handle_revoke_callback,
    help_command,
    list_applications,
    new,
    paginate_list,
    revoke_application,
    show_application_detail,
    start,
)
from bot.config import create_valkey_client, load_config
from bot.constants import (
    ADMIN_ADD_ADMIN_WAIT_ID,
    ADMIN_BROADCAST_AUDIENCE,
    ADMIN_BROADCAST_CONFIRM,
    ADMIN_BROADCAST_DECISION,
    ADMIN_BROADCAST_MESSAGE,
    ADMIN_BROADCAST_SCHEDULE_TIME,
    ADMIN_REMOVE_ADMIN_WAIT_ID,
    CONDITION,
    CONTACTS,
    DESCRIPTION,
    EDIT_CONDITION,
    EDIT_DESCRIPTION,
    EDIT_PHOTOS,
    EDIT_POSITION,
    MATERIAL,
    PHOTOS,
    POSITION,
    PRICE,
    SIZE,
    SKIP_KEYWORD_PATTERN,
)
from bot.editing import (
    cancel_editing,
    finalize_photo_upload,
    receive_condition_choice,
    receive_description,
    receive_photo_upload,
    receive_position,
    start_edit_condition,
    start_edit_description,
    start_edit_photos,
    start_edit_position,
)
from bot.logging import logger
from bot.media_storage import create_media_storage
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
    media_storage = create_media_storage(config.get("storage"))
    app.bot_data["media_storage"] = media_storage
    app.bot_data["moderator_chat_ids"] = config.get("moderator_chat_ids", [])
    app.bot_data["super_admin_ids"] = config.get("super_admin_ids", [])
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
    app.add_handler(CommandHandler("revoke", revoke_application))
    app.add_handler(CommandHandler("applications", view_all_applications))
    app.add_handler(CommandHandler("admins", show_admin_roster))
    app.add_handler(CommandHandler("broadcast_history", show_broadcast_history))
    app.add_handler(CommandHandler("scheduled", show_scheduled_broadcasts))
    app.add_handler(
        CallbackQueryHandler(show_application_detail, pattern=r"^list:view:")
    )
    app.add_handler(CallbackQueryHandler(paginate_list, pattern=r"^list:page:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_revoke_callback, pattern=r"^revoke:"))
    app.add_handler(
        CallbackQueryHandler(
            navigate_application_photo_prev, pattern=r"^admin_app_photo_prev:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            navigate_application_photo_next, pattern=r"^admin_app_photo_next:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(navigate_applications, pattern=r"^admin_view:")
    )

    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_edit_position, pattern=r"^edit:position:"),
            CallbackQueryHandler(start_edit_description, pattern=r"^edit:description:"),
            CallbackQueryHandler(start_edit_condition, pattern=r"^edit:condition:"),
            CallbackQueryHandler(start_edit_photos, pattern=r"^edit:photos:"),
        ],
        states={
            EDIT_POSITION: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_position,
                )
            ],
            EDIT_DESCRIPTION: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_description,
                )
            ],
            EDIT_CONDITION: [
                CallbackQueryHandler(
                    receive_condition_choice,
                    pattern=r"^edit_condition:set:",
                )
            ],
            EDIT_PHOTOS: [
                MessageHandler(filters.PHOTO, receive_photo_upload),
                MessageHandler(
                    filters.Regex(SKIP_KEYWORD_PATTERN), finalize_photo_upload
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_editing)],
        per_message=False,
    )

    add_admin_conv = ConversationHandler(
        entry_points=[CommandHandler("addadmin", start_add_admin)],
        states={
            ADMIN_ADD_ADMIN_WAIT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_admin_id)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        per_message=False,
    )

    remove_admin_conv = ConversationHandler(
        entry_points=[CommandHandler("removeadmin", start_remove_admin)],
        states={
            ADMIN_REMOVE_ADMIN_WAIT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_remove_admin_id)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        per_message=False,
    )

    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", start_broadcast)],
        states={
            ADMIN_BROADCAST_AUDIENCE: [
                CallbackQueryHandler(
                    choose_broadcast_audience, pattern=r"^broadcast:audience:"
                )
            ],
            ADMIN_BROADCAST_MESSAGE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_broadcast_message
                )
            ],
            ADMIN_BROADCAST_DECISION: [
                CallbackQueryHandler(
                    handle_broadcast_decision, pattern=r"^broadcast:decision:"
                )
            ],
            ADMIN_BROADCAST_SCHEDULE_TIME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_broadcast_schedule
                )
            ],
            ADMIN_BROADCAST_CONFIRM: [
                CallbackQueryHandler(confirm_broadcast, pattern=r"^broadcast:confirm:")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        per_message=False,
    )

    app.add_handler(conv_handler)
    app.add_handler(add_admin_conv)
    app.add_handler(remove_admin_conv)
    app.add_handler(broadcast_conv)
    app.add_handler(edit_conv)
    app.add_error_handler(error_handler)
    logger.info("Handlers registered; starting polling")
    app.run_polling()


if __name__ == "__main__":
    main()
