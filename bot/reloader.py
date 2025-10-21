"""Development entrypoint with hot reload support."""

from __future__ import annotations

from watchfiles import PythonFilter, run_process


def main() -> None:
    """Run the bot with automatic reload on Python file changes."""
    run_process(
        "bot",
        target="python -m bot.main",
        watch_filter=PythonFilter(),
    )


if __name__ == "__main__":
    main()
