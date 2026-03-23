from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class NotificationEntry:
    timestamp: datetime
    category: str  # SYS, NET, GPS, PWR, ENV, APP
    severity: str  # INFO, WARN, ERROR
    message: str
    unread: bool = True


class NotificationManager:
    """Stores recent PiBoy notifications in memory."""

    def __init__(self, max_entries: int = 100):
        self.__entries = deque(maxlen=max_entries)
        self.__lock = threading.Lock()

    def push(self, category: str, severity: str, message: str) -> None:
        category = category[:4].upper()
        severity = severity.upper()

        with self.__lock:
            if self.__entries:
                latest = self.__entries[0]
                if (
                    latest.category == category
                    and latest.severity == severity
                    and latest.message == message
                ):
                    return

            self.__entries.appendleft(
                NotificationEntry(
                    timestamp=datetime.now(),
                    category=category,
                    severity=severity,
                    message=message,
                    unread=True,
                )
            )

    def get_latest(self) -> Optional[NotificationEntry]:
        with self.__lock:
            return self.__entries[0] if self.__entries else None

    def get_all(self) -> list[NotificationEntry]:
        with self.__lock:
            return list(self.__entries)

    def get_unread_count(self) -> int:
        with self.__lock:
            return sum(1 for entry in self.__entries if entry.unread)

    def mark_all_read(self) -> None:
        with self.__lock:
            for entry in self.__entries:
                entry.unread = False

    def clear(self) -> None:
        with self.__lock:
            self.__entries.clear()