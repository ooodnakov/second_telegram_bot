from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


def extract_messages(events: list[object]) -> list[str]:
    messages: list[str] = []
    for event in events:
        try:
            record = event.record  # type: ignore[attr-defined]
            messages.append(str(record["message"]))
        except AttributeError:
            messages.append(str(event))
    return messages


@contextmanager
def capture_logs(logger: Any, level: str = "DEBUG") -> Iterator[list[object]]:
    events: list[object] = []

    def sink(message: object) -> None:
        events.append(message)

    handler_id = logger.add(sink, level=level)
    try:
        yield events
    finally:
        logger.remove(handler_id)
