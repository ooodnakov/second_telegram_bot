"""Utility helpers for loading user-facing messages."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomllib

DEFAULT_MESSAGES_PATH = Path(__file__).resolve().parent / "messages.toml"


class _FormatDict(dict[str, Any]):
    """Dictionary that preserves unknown placeholders during formatting."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def load_messages(path: str | Path | None = None) -> dict[str, Any]:
    """Load message catalog from a TOML file."""

    source = path or os.environ.get("MESSAGES_PATH") or DEFAULT_MESSAGES_PATH
    target = Path(source).expanduser()
    try:
        with target.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError as exc:  # pragma: no cover - misconfiguration
        raise RuntimeError(f"Messages file not found: {target}") from exc
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - misconfiguration
        raise RuntimeError(f"Failed to parse messages file {target}: {exc}") from exc


_MESSAGES: dict[str, Any] = load_messages()


def get_message(key: str, **params: Any) -> str:
    """Retrieve a message by dotted key and format it with provided parameters."""

    value: Any = _MESSAGES
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(f"Message key '{key}' not found")
        value = value[part]

    if not isinstance(value, str):
        raise TypeError(f"Message '{key}' is not a string")

    if params:
        return value.format_map(_FormatDict(params))
    return value


__all__ = ["DEFAULT_MESSAGES_PATH", "get_message", "load_messages"]
