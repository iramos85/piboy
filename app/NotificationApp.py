from __future__ import annotations

from PIL import Image, ImageDraw

from app.App import App
from core.decorator import override
from data.NotificationManager import NotificationManager


class NotificationApp(App):
    """Displays recent PiBoy notifications."""

    def __init__(self, notification_manager: NotificationManager, title: str = "NOTIF"):
        self.__notification_manager = notification_manager
        self.__title = title

    @property
    @override
    def title(self) -> str:
        return self.__title

    @override
    def draw(self, image: Image.Image, partial=False) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)
        width, height = image.size

        notifications = self.__notification_manager.get_all()
        unread_count = self.__notification_manager.get_unread_count()

        # Header
        draw.text((8, 4), "NOTIFICATIONS", fill=255)
        draw.text((width - 70, 4), f"NEW:{unread_count}", fill=255)

        y = 24
        line_height = 16
        max_lines = max(1, (height - y - 12) // line_height)

        if not notifications:
            draw.text((8, y), "NO NOTIFICATIONS", fill=255)
            return image, 0, 0

        for entry in notifications[:max_lines]:
            time_str = entry.timestamp.strftime("%H:%M")
            line = f"{time_str} {entry.category:<4} {entry.severity:<5} {entry.message}"
            draw.text((8, y), line[:48], fill=255)
            y += line_height

        return image, 0, 0

    def acknowledge_all(self) -> None:
        self.__notification_manager.mark_all_read()