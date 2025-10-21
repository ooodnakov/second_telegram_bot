## Missing Features & Follow-ups

- Improve photo flow UX: allow completion buttons, handle non-photo inputs, enforce limits, and support skips/corrections (`bot/main.py`).
- Validate/sanitize free-text inputs (price format, Markdown escaping, etc.) and provide feedback on invalid data (`bot/main.py`).
- Introduce logging/monitoring hooks and commands for health/status reporting (`bot/main.py`) use logger from `bot/logger_setup.py`.
- Fill out `README.md` with setup, deployment, and configuration guidance.
- Split dev-only tools (e.g., `ruff`, `asyncio`) into the dev dependency group and add automated tests covering the conversation flow.

## Done

- Forward completed submissions to moderator/admin destinations and documented related config entries (`bot/main.py`, `config.ini`).
- Replaced the in-memory `applications` dict with Valkey-backed session storage so conversations survive restarts (`bot/main.py`).
- Added `CallbackQueryHandler` so condition button presses advance the conversation (`bot/main.py`).
