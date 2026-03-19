import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from injector import inject
from PIL import Image, ImageDraw

from app.App import App
from core.decorator import override
from core.data import ConnectionStatus, DeviceStatus
from data.BatteryStatusProvider import BatteryStatusProvider
from data.EnvironmentDataProvider import EnvironmentDataProvider
from data.LocationProvider import LocationProvider
from data.NetworkStatusProvider import NetworkStatusProvider
from environment import AppConfig

logger = logging.getLogger("app")

LOCAL_TZ = ZoneInfo("America/Chicago")


class DashboardApp(App):
    @inject
    def __init__(
        self,
        app_config: AppConfig,
        network_status_provider: NetworkStatusProvider,
        location_provider: LocationProvider,
        battery_status_provider: BatteryStatusProvider,
        environment_data_provider: EnvironmentDataProvider,
    ):
        self.__app_config = app_config
        self.__network_status_provider = network_status_provider
        self.__location_provider = location_provider
        self.__battery_status_provider = battery_status_provider
        self.__environment_data_provider = environment_data_provider

    @property
    @override
    def title(self) -> str:
        return "HOME"

    def __safe_call(self, fn, default=None):
        try:
            return fn()
        except Exception as ex:
            logger.debug("DashboardApp safe call failed: %s", ex)
            return default

    def __text_width(self, font, text: str) -> int:
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]

    def __draw_box(self, draw: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int, color):
        draw.rectangle((x0, y0, x1, y1), outline=color, width=1)

    def __draw_label(self, draw: ImageDraw.ImageDraw, x: int, y: int, text: str, color):
        draw.text((x, y), text, fill=color, font=self.__app_config.font_header)

    def __draw_text(self, draw: ImageDraw.ImageDraw, x: int, y: int, text: str, color):
        draw.text((x, y), text, fill=color, font=self.__app_config.font_standard)

    def __draw_meter(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        w: int,
        h: int,
        value: float | None,
        color,
        segments: int = 10,
    ):
        draw.rectangle((x, y, x + w, y + h), outline=color, width=1)

        if value is None:
            return

        value = max(0.0, min(1.0, float(value)))
        filled = int(round(value * segments))

        inner_pad = 2
        gap = 2
        inner_w = w - inner_pad * 2
        seg_w = max(1, (inner_w - (segments - 1) * gap) // segments)

        sx = x + inner_pad
        sy = y + inner_pad
        sh = h - inner_pad * 2

        for i in range(segments):
            x0 = sx + i * (seg_w + gap)
            x1 = x0 + seg_w
            y0 = sy
            y1 = sy + sh
            if i < filled:
                draw.rectangle((x0, y0, x1, y1), fill=color)

    def __draw_signal_bars(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        color,
        level: int,
    ):
        level = max(0, min(4, level))
        bar_w = 5
        gap = 3
        heights = [5, 9, 13, 17]

        for i, h in enumerate(heights):
            x0 = x + i * (bar_w + gap)
            y0 = y + (17 - h)
            x1 = x0 + bar_w
            y1 = y + 17
            draw.rectangle((x0, y0, x1, y1), outline=color, width=1)
            if i < level:
                draw.rectangle((x0 + 1, y0 + 1, x1 - 1, y1 - 1), fill=color)

    def __draw_crosshair(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        r: int,
        color,
        active: bool,
    ):
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=1)
        draw.line((cx - r - 4, cy, cx + r + 4, cy), fill=color, width=1)
        draw.line((cx, cy - r - 4, cx, cy + r + 4), fill=color, width=1)
        if active:
            draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=color)

    def __norm_range(self, value, low, high):
        try:
            v = float(value)
            if high <= low:
                return None
            return max(0.0, min(1.0, (v - low) / (high - low)))
        except Exception:
            return None

    @override
    def draw(self, image: Image.Image, partial=False) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)

        width, height = image.size
        accent = self.__app_config.accent
        background = self.__app_config.background
        font_header = self.__app_config.font_header
        font_body = self.__app_config.font_standard

        draw.rectangle((0, 0, width, height), fill=background)

        # Outer frame
        draw.rectangle((1, 1, width - 2, height - 2), outline=accent, width=1)
        draw.rectangle((5, 5, width - 6, height - 6), outline=accent, width=1)

        # Header
        draw.line((8, 26, width - 8, 26), fill=accent, width=1)
        draw.text((10, 8), "PIP-BOY // STATUS HUB", fill=accent, font=font_header)

        now_str = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p")
        now_w = self.__text_width(font_body, now_str)
        draw.text((width - now_w - 10, 9), now_str, fill=accent, font=font_body)

        # Panel layout
        left_x0, left_y0, left_x1, left_y1 = 10, 34, 150, 176
        right_x0, right_y0, right_x1, right_y1 = 160, 34, width - 10, 176
        msg_x0, msg_y0, msg_x1, msg_y1 = 10, 182, width - 10, height - 10

        self.__draw_box(draw, left_x0, left_y0, left_x1, left_y1, accent)
        self.__draw_box(draw, right_x0, right_y0, right_x1, right_y1, accent)
        self.__draw_box(draw, msg_x0, msg_y0, msg_x1, msg_y1, accent)

        # Provider data
        connection_status = self.__safe_call(self.__network_status_provider.get_connection_status)
        battery_soc = self.__safe_call(self.__battery_status_provider.get_state_of_charge)
        battery_device_status = self.__safe_call(self.__battery_status_provider.get_device_status)

        gps_status = self.__safe_call(self.__location_provider.get_device_status)
        location = self.__safe_call(self.__location_provider.get_location)

        env_status = self.__safe_call(self.__environment_data_provider.get_device_status)
        env_data = self.__safe_call(self.__environment_data_provider.get_environment_data)

        lat = getattr(location, "latitude", None) if location is not None else None
        lon = getattr(location, "longitude", None) if location is not None else None

        temp = getattr(env_data, "temperature", None) if env_data is not None else None
        humidity = getattr(env_data, "humidity", None) if env_data is not None else None
        pressure = getattr(env_data, "pressure", None) if env_data is not None else None

        ip_addr = None
        ssid = None

        for attr_name in ("get_ip_address", "get_ip", "ip_address"):
            attr = getattr(self.__network_status_provider, attr_name, None)
            if callable(attr):
                ip_addr = self.__safe_call(attr)
                if ip_addr:
                    break

        for attr_name in ("get_ssid", "get_wifi_name", "ssid"):
            attr = getattr(self.__network_status_provider, attr_name, None)
            if callable(attr):
                ssid = self.__safe_call(attr)
                if ssid:
                    break

        status_line = "ALL SYSTEMS NOMINAL"
        if env_status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
            status_line = "ENV SENSOR NOT READY"
        elif gps_status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
            status_line = "GPS NOT READY"
        elif connection_status == ConnectionStatus.DISCONNECTED:
            status_line = "NETWORK DISCONNECTED"
        elif battery_soc is not None and battery_soc <= 0.20:
            status_line = "LOW BATTERY WARNING"

        # Left panel: SYS / NET
        self.__draw_label(draw, 16, 40, "SYS", accent)

        self.__draw_text(draw, 16, 60, "BAT", accent)
        self.__draw_meter(draw, 44, 60, 90, 12, battery_soc, accent, segments=8)
        bat_text = "--" if battery_soc is None else f"{battery_soc:.0%}"
        draw.text((100, 76), bat_text, fill=accent, font=font_body)

        self.__draw_text(draw, 16, 94, "PWR", accent)
        pwr_text = "--" if battery_device_status is None else str(battery_device_status).split(".")[-1]
        self.__draw_text(draw, 54, 94, pwr_text, accent)

        self.__draw_text(draw, 16, 112, "NET", accent)
        net_text = "--" if connection_status is None else str(connection_status).split(".")[-1]
        self.__draw_text(draw, 54, 112, net_text, accent)

        signal_level = 0
        if connection_status == ConnectionStatus.CONNECTED:
            signal_level = 4
        self.__draw_signal_bars(draw, 112, 110, accent, signal_level)

        self.__draw_text(draw, 16, 132, f"IP  {ip_addr if ip_addr else '--'}", accent)
        self.__draw_text(draw, 16, 150, f"AP  {ssid if ssid else '--'}", accent)

        # Right panel: GPS + ENV
        self.__draw_label(draw, 166, 40, "GPS", accent)

        gps_active = gps_status == DeviceStatus.OPERATIONAL and isinstance(lat, (int, float)) and isinstance(lon, (int, float))
        self.__draw_crosshair(draw, right_x1 - 28, 58, 12, accent, gps_active)

        gps_text = "--" if gps_status is None else str(gps_status).split(".")[-1]
        self.__draw_text(draw, 166, 60, f"STS {gps_text}", accent)
        self.__draw_text(draw, 166, 78, f"LAT {lat:.5f}" if isinstance(lat, (int, float)) else "LAT --", accent)
        self.__draw_text(draw, 166, 96, f"LON {lon:.5f}" if isinstance(lon, (int, float)) else "LON --", accent)

        self.__draw_label(draw, 166, 114, "ENV", accent)

        temp_norm = self.__norm_range(temp, 30, 100)
        hum_norm = self.__norm_range(humidity, 0, 100)
        prs_norm = self.__norm_range(pressure, 950, 1050)

        self.__draw_text(draw, 166, 132, "TMP", accent)
        self.__draw_meter(draw, 198, 132, 60, 10, temp_norm, accent, segments=6)
        self.__draw_text(draw, 264, 130, f"{temp:.1f}" if isinstance(temp, (int, float)) else "--", accent)

        self.__draw_text(draw, 166, 146, "HUM", accent)
        self.__draw_meter(draw, 198, 146, 60, 10, hum_norm, accent, segments=6)
        self.__draw_text(draw, 264, 144, f"{humidity:.0f}%" if isinstance(humidity, (int, float)) else "--", accent)

        self.__draw_text(draw, 166, 160, "PRS", accent)
        self.__draw_meter(draw, 198, 160, 60, 10, prs_norm, accent, segments=6)
        self.__draw_text(draw, 264, 158, f"{pressure:.0f}" if isinstance(pressure, (int, float)) else "--", accent)

        # Message strip
        self.__draw_label(draw, 16, 188, "CONSOLE", accent)
        self.__draw_text(draw, 16, 208, f"MSG  {status_line}", accent)
        self.__draw_text(draw, 16, 228, f"ENV  {('--' if env_status is None else str(env_status).split('.')[-1])}", accent)
        self.__draw_text(draw, 112, 228, f"GPS  {('--' if gps_status is None else str(gps_status).split('.')[-1])}", accent)
        self.__draw_text(draw, 220, 228, f"NET  {('--' if connection_status is None else str(connection_status).split('.')[-1])}", accent)

        return image, 0, 0