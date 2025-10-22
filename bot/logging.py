"""Shared logging utilities for the Telegram bot."""

from __future__ import annotations

from bot.logger_setup import setup_logger

logger = setup_logger()

__all__ = ["logger"]
