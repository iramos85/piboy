import logging
import time
from typing import Optional

from injector import inject
from PIL import Image, ImageDraw

from app.App import App
from core.decorator import override
from core import resources
from data.BatteryStatusProvider import BatteryStatusProvider

logger = logging.getLogger("app")


class PowerApp(App):
    """Retro-futuristic reactor themed power status screen."""

    @inject
    def __init__(self, battery_status_provider: BatteryStatusProvider):
        self.__battery_status_provider = battery_status_provider
        self.__last_percent: Optional[int] = None
        self.__last_time: Optional[float] = None
        self.__decay_rate_per_hour: Optional[float] = None

    @property
    @override
    def title(self) -> str:
        return "PWR"

    def __safe_percent(self) -> Optional[int]:
        try:
            soc = self.__battery_status_provider.get_state_of_charge()
            if soc is None:
                return None
            soc = max(0.0, min(1.0, float(soc)))
            return int(round(soc * 100))
        except Exception as ex:
            logger.warning("PowerApp: unable to read state of charge: %s", ex)
            return None

    def __safe_voltage(self) -> Optional[float]:
        """
        Tries a few likely battery provider method names so this app can survive
        across slightly different provider implementations.
        """
        provider = self.__battery_status_provider

        for attr in [
            "get_battery_voltage",
            "get_voltage",
            "get_cell_voltage",
            "get_estimated_battery_voltage",
        ]:
            fn = getattr(provider, attr, None)
            if callable(fn):
                try:
                    value = fn()
                    if value is not None:
                        return float(value)
                except Exception as ex:
                    logger.warning("PowerApp: voltage read failed via %s: %s", attr, ex)

        return None

    def __safe_status_text(self, percent: Optional[int]) -> str:
        try:
            status = self.__battery_status_provider.get_device_status()
            status_str = str(status).upper()

            if "CHARG" in status_str:
                return "CHARGING"
            if "EXTERNAL" in status_str or "PLUG" in status_str:
                return "EXTERNAL FEED"
            if "LOW" in status_str:
                return "LOW RESERVE"
            if "CRIT" in status_str:
                return "CRITICAL"
        except Exception:
            pass

        if percent is None:
            return "UNKNOWN"
        if percent <= 10:
            return "CRITICAL"
        if percent <= 25:
            return "LOW RESERVE"
        return "NOMINAL"

    def __safe_feed_text(self) -> str:
        try:
            status = self.__battery_status_provider.get_device_status()
            status_str = str(status).upper()

            if "CHARG" in status_str or "EXTERNAL" in status_str or "PLUG" in status_str:
                return "EXTERNAL"
        except Exception:
            pass

        return "INTERNAL"

    def __update_decay_rate(self, percent: Optional[int]) -> None:
        now = time.time()

        if percent is None:
            return

        if self.__last_percent is None or self.__last_time is None:
            self.__last_percent = percent
            self.__last_time = now
            return

        elapsed = now - self.__last_time
        if elapsed < 300:
            return

        delta = self.__last_percent - percent
        if elapsed > 0 and delta >= 0:
            hours = elapsed / 3600.0
            if hours > 0:
                self.__decay_rate_per_hour = delta / hours

        self.__last_percent = percent
        self.__last_time = now

    def __format_runtime(self, percent: Optional[int]) -> str:
        if percent is None:
            return "--H --M"

        rate = self.__decay_rate_per_hour
        if rate is None or rate <= 0.1:
            return "--H --M"

        hours_left = percent / rate
        total_minutes = max(0, int(hours_left * 60))
        hours = total_minutes // 60
        minutes = total_minutes % 60
        return f"{hours}H {minutes:02d}M"

    def __format_decay(self) -> str:
        if self.__decay_rate_per_hour is None:
            return "--.-%/HR"
        return f"{self.__decay_rate_per_hour:.1f}%/HR"

    def __draw_segments(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        width: int,
        height: int,
        percent: int,
        segments: int = 10,
    ) -> None:
        gap = 3
        seg_w = max(4, (width - ((segments - 1) * gap)) // segments)
        filled = round((percent / 100) * segments)

        for i in range(segments):
            sx = x + i * (seg_w + gap)
            sy = y
            ex = sx + seg_w
            ey = sy + height

            draw.rectangle((sx, sy, ex, ey), outline=255, width=1)

            if i < filled:
                draw.rectangle((sx + 1, sy + 1, ex - 1, ey - 1), fill=255)

    def __draw_trefoil(self, draw: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
        r = 8
        draw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=255)
        draw.arc((cx - r, cy - r - 6, cx + r, cy + r - 6), start=20, end=160, fill=255, width=2)
        draw.arc((cx - r - 6, cy - r + 2, cx + r - 6, cy + r + 2), start=260, end=40, fill=255, width=2)
        draw.arc((cx - r + 6, cy - r + 2, cx + r + 6, cy + r + 2), start=140, end=280, fill=255, width=2)

    @override
    def draw(self, image: Image.Image, partial=False) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)
        width, height = image.size

        font_big = resources.roboto_bold(22)
        font_med = resources.roboto_bold(15)
        font_small = resources.roboto(12)

        percent = self.__safe_percent()
        volts = self.__safe_voltage()
        feed = self.__safe_feed_text()
        state = self.__safe_status_text(percent)

        self.__update_decay_rate(percent)
        runtime = self.__format_runtime(percent)
        decay = self.__format_decay()

        # Header
        draw.line((6, 8, width - 6, 8), fill=255, width=1)
        draw.text((12, 14), "PWR", font=font_big, fill=255)
        self.__draw_trefoil(draw, width - 22, 24)
        draw.line((6, 42, width - 6, 42), fill=255, width=1)

        # Main charge block
        main_label_y = 52
        draw.text((12, main_label_y), "CORE CHARGE", font=font_med, fill=255)

        percent_text = "--%" if percent is None else f"{percent}%"
        bbox = draw.textbbox((0, 0), percent_text, font=font_big)
        pct_w = bbox[2] - bbox[0]
        draw.text((width - pct_w - 12, 48), percent_text, font=font_big, fill=255)

        bar_x = 12
        bar_y = 78
        bar_w = width - 24
        bar_h = 16
        self.__draw_segments(draw, bar_x, bar_y, bar_w, bar_h, percent or 0, segments=10)

        # Optional warning blink feel for low battery
        blink_on = int(time.time()) % 2 == 0
        if percent is not None and percent <= 25 and blink_on:
            draw.text((12, 100), "!! LOW RESERVE !!", font=font_small, fill=255)
        elif state == "CRITICAL" and blink_on:
            draw.text((12, 100), "!! CRITICAL CORE !!", font=font_small, fill=255)

        # Detail grid
        left_x = 12
        right_x = width // 2 + 4
        row1_y = 118
        row_gap = 22

        draw.text((left_x, row1_y), "CELL VOLT", font=font_small, fill=255)
        draw.text(
            (right_x, row1_y),
            "--.--V" if volts is None else f"{volts:.2f}V",
            font=font_small,
            fill=255,
        )

        draw.text((left_x, row1_y + row_gap), "POWER FEED", font=font_small, fill=255)
        draw.text((right_x, row1_y + row_gap), feed, font=font_small, fill=255)

        draw.text((left_x, row1_y + row_gap * 2), "REACTOR STATE", font=font_small, fill=255)
        draw.text((right_x, row1_y + row_gap * 2), state, font=font_small, fill=255)

        draw.text((left_x, row1_y + row_gap * 3), "CORE LIFE", font=font_small, fill=255)
        draw.text((right_x, row1_y + row_gap * 3), runtime, font=font_small, fill=255)

        draw.text((left_x, row1_y + row_gap * 4), "DECAY RATE", font=font_small, fill=255)
        draw.text((right_x, row1_y + row_gap * 4), decay, font=font_small, fill=255)

        return image, 0, 0