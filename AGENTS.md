# AGENTS.md

## Project overview
- This repository contains a Telegram bot that guides users through submitting classified-style listings via a multi-step conversation.
- Configuration values (bot token, moderator chat IDs, Valkey connection) are read from `config.ini` or the `CONFIG_PATH` environment variable. A template is available as `config.ini.example`.
- Persistent state for pending listings is stored in Valkey (or the in-memory fallback in tests), while uploaded media files are written under the repository `media/` directory.
- The `bot/` package provides the runtime code (`main.py` with the conversation handlers, `logger_setup.py` configuring Loguru logging, and `reloader.py` for hot-reload tooling). Tests that stub external services live in `test/`.

## Local development tips
- Install dependencies with [uv](https://github.com/astral-sh/uv): `uv sync` will materialize the virtual environment declared in `pyproject.toml`/`uv.lock`.
- Copy `config.ini.example` to `config.ini` (or point `CONFIG_PATH` to another file) and fill in secrets before running the bot locally.
- Start the bot with `uv run python -m bot.main`; this will initialize the Telegram handlers and connect to Valkey using the configured credentials.

## Quality, formatting, and tests
Run the following commands before submitting changes (in this order):
1. `uv run ruff check --select I --fix` – auto-sorts imports to keep the codebase consistent.
2. `uv run ruff check` – runs the full Ruff lint suite configured in `pyproject.toml`.
3. `uv run ruff format` – applies the repository formatting conventions.
4. `uv run pytest` – executes the test suite under `test/` with external integrations stubbed out.

## Contribution etiquette
- Keep user-facing strings localized where the conversation already provides translations or Russian text, and prefer reusing existing helper functions instead of duplicating logic in new handlers.
- When touching conversation state transitions, ensure that `LIST_PAGE_SIZE` pagination and Valkey cleanup still operate correctly; add unit coverage in `test/test_logging_and_config.py` or new files under `test/` where appropriate.
- Document any new environment variables or configuration knobs in `config.ini.example` so operators can discover them easily.
