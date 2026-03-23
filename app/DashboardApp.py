import json
import logging
import threading
import time
import urllib.parse
import urllib.request

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

BUILD_LABEL = "BUILD V2.4"
GEOCODE_TTL_S = 300
GEOCODE_MIN_MOVE_DEG = 0.002


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

        self.__ticker_offset = 0
        self.__ticker_text = "BOOTING STATUS HUB"

        self.__geo_lock = threading.Lock()
        self.__geo_lookup_in_progress = False
        self.__geo_last_lookup_ts = 0.0
        self.__geo_last_coords: tuple[float, float] | None = None
        self.__geo_place_label: str | None = None

    @property
    @override
    def title(self) -> str:
        return "HOME"

    def tick(self):
        self.__ticker_offset += 1

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

    def __build_status_line(
        self,
        connection_status,
        battery_soc,
        gps_status,
        env_status,
    ) -> str:
        parts = []

        if env_status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
            parts.append("ENV SENSOR NOT READY")
        else:
            parts.append("ENV OK")

        if gps_status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
            parts.append("GPS NOT READY")
        else:
            parts.append("GPS OK")

        if connection_status == ConnectionStatus.DISCONNECTED:
            parts.append("NETWORK DISCONNECTED")
        else:
            parts.append("NETWORK CONNECTED")

        if battery_soc is not None and battery_soc <= 0.20:
            parts.append("LOW BATTERY WARNING")
        elif battery_soc is not None:
            parts.append(f"BATTERY {battery_soc:.0%}")

        return "  //  ".join(parts)

    def __draw_ticker(self, draw: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int, color):
        font = self.__app_config.font_standard
        self.__draw_label(draw, x0 + 6, y0 + 4, "CONSOLE", color)

        text_y = y0 + 24
        available_width = (x1 - x0) - 12

        full_text = f"MSG  {self.__ticker_text}   "
        if not full_text.strip():
            return

        char_offset = self.__ticker_offset % max(1, len(full_text))
        visible_text = full_text[char_offset:] + full_text[:char_offset]

        while self.__text_width(font, visible_text) < available_width + 40:
            visible_text += full_text

        self.__draw_text(draw, x0 + 6, text_y, visible_text, color)

    def __needs_geocode_refresh(self, lat: float, lon: float) -> bool:
        with self.__geo_lock:
            now = time.time()

            if self.__geo_lookup_in_progress:
                return False

            if self.__geo_last_coords is None:
                return True

            last_lat, last_lon = self.__geo_last_coords
            moved = abs(lat - last_lat) > GEOCODE_MIN_MOVE_DEG or abs(lon - last_lon) > GEOCODE_MIN_MOVE_DEG
            expired = (now - self.__geo_last_lookup_ts) > GEOCODE_TTL_S

            return moved or expired

    def __format_place_label(self, payload: dict) -> str | None:
        address = payload.get("address", {})

        suburb = (
            address.get("suburb")
            or address.get("neighbourhood")
            or address.get("quarter")
            or address.get("city_district")
        )
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        )
        state = address.get("state")

        if suburb and city and state:
            return f"{suburb}, {city}, {state}"
        if city and state:
            return f"{city}, {state}"
        if suburb and state:
            return f"{suburb}, {state}"
        if state:
            return state

        display_name = payload.get("display_name")
        if isinstance(display_name, str) and display_name.strip():
            return display_name.split(",")[0].strip()

        return None

    def __reverse_geocode_worker(self, lat: float, lon: float):
        try:
            query = urllib.parse.urlencode(
                {
                    "lat": f"{lat:.6f}",
                    "lon": f"{lon:.6f}",
                    "format": "jsonv2",
                    "zoom": "12",
                    "addressdetails": "1",
                }
            )
            url = f"https://nominatim.openstreetmap.org/reverse?{query}"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "PiBoyDashboard/2.4 (local project use)",
                    "Accept": "application/json",
                },
            )

            with urllib.request.urlopen(req, timeout=3.0) as response:
                payload = json.loads(response.read().decode("utf-8"))

            place_label = self.__format_place_label(payload)
            if place_label:
                place_label = place_label.upper()

            with self.__geo_lock:
                self.__geo_last_coords = (lat, lon)
                self.__geo_last_lookup_ts = time.time()
                self.__geo_place_label = place_label
        except Exception as ex:
            logger.debug("Reverse geocode failed: %s", ex)
            with self.__geo_lock:
                self.__geo_last_coords = (lat, lon)
                self.__geo_last_lookup_ts = time.time()
        finally:
            with self.__geo_lock:
                self.__geo_lookup_in_progress = False

    def __request_geocode_if_needed(self, connection_status, lat, lon):
        if connection_status != ConnectionStatus.CONNECTED:
            return

        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return

        if not self.__needs_geocode_refresh(lat, lon):
            return

        with self.__geo_lock:
            if self.__geo_lookup_in_progress:
                return
            self.__geo_lookup_in_progress = True

        threading.Thread(
            target=self.__reverse_geocode_worker,
            args=(float(lat), float(lon)),
            daemon=True,
        ).start()

    def __get_cached_place_label(self) -> str | None:
        with self.__geo_lock:
            return self.__geo_place_label

    @override
    def draw(self, image: Image.Image, partial=False) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)

        width, height = image.size
        accent = self.__app_config.accent
        background = self.__app_config.background
        font_body = self.__app_config.font_standard

        draw.rectangle((0, 0, width, height), fill=background)

        draw.rectangle((1, 1, width - 2, height - 2), outline=accent, width=1)
        draw.rectangle((5, 5, width - 6, height - 6), outline=accent, width=1)

        draw.line((8, 26, width - 8, 26), fill=accent, width=1)
        draw.text((10, 8), "PIP-BOY // STATUS HUB", fill=accent, font=self.__app_config.font_header)

        build_w = self.__text_width(font_body, BUILD_LABEL)
        draw.text((width - build_w - 10, 9), BUILD_LABEL, fill=accent, font=font_body)

        left_x0, left_y0, left_x1, left_y1 = 10, 34, 162, 176
        right_x0, right_y0, right_x1, right_y1 = 172, 34, width - 10, 176
        msg_x0, msg_y0, msg_x1, msg_y1 = 10, 182, width - 10, height - 10

        self.__draw_box(draw, left_x0, left_y0, left_x1, left_y1, accent)
        self.__draw_box(draw, right_x0, right_y0, right_x1, right_y1, accent)
        self.__draw_box(draw, msg_x0, msg_y0, msg_x1, msg_y1, accent)

        connection_status = self.__safe_call(self.__network_status_provider.get_connection_status)
        battery_soc = self.__safe_call(self.__battery_status_provider.get_state_of_charge)
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

        self.__request_geocode_if_needed(connection_status, lat, lon)
        place_label = self.__get_cached_place_label()

        self.__ticker_text = self.__build_status_line(
            connection_status=connection_status,
            battery_soc=battery_soc,
            gps_status=gps_status,
            env_status=env_status,
        )

        # LEFT PANEL
        self.__draw_label(draw, 16, 40, "SYS", accent)

        self.__draw_text(draw, 16, 60, "BAT", accent)
        self.__draw_meter(draw, 44, 60, 96, 12, battery_soc, accent, segments=8)
        bat_text = "--" if battery_soc is None else f"{battery_soc:.0%}"
        draw.text((106, 76), bat_text, fill=accent, font=font_body)

        self.__draw_text(draw, 16, 102, "NET", accent)
        net_text = "--" if connection_status is None else str(connection_status).split(".")[-1]
        self.__draw_text(draw, 54, 102, net_text, accent)

        signal_level = 4 if connection_status == ConnectionStatus.CONNECTED else 0
        self.__draw_signal_bars(draw, 136, 100, accent, signal_level)

        self.__draw_text(draw, 16, 126, f"IP  {ip_addr if ip_addr else '--'}", accent)
        self.__draw_text(draw, 16, 146, f"AP  {ssid if ssid else '--'}", accent)

        # RIGHT PANEL
        self.__draw_label(draw, 178, 40, "GPS", accent)

        gps_active = (
            gps_status == DeviceStatus.OPERATIONAL
            and isinstance(lat, (int, float))
            and isinstance(lon, (int, float))
        )
        self.__draw_crosshair(draw, right_x1 - 28, 58, 12, accent, gps_active)

        if place_label and gps_active:
            self.__draw_text(draw, 178, 62, place_label[:20], accent)
            if len(place_label) > 20:
                self.__draw_text(draw, 178, 80, place_label[20:40], accent)
            else:
                self.__draw_text(draw, 178, 80, "LOCATION LOCK", accent)
            self.__draw_text(draw, 178, 98, "COORDS CACHED", accent)
        else:
            gps_text = "--" if gps_status is None else str(gps_status).split(".")[-1]
            self.__draw_text(draw, 178, 60, f"STS {gps_text}", accent)
            self.__draw_text(draw, 178, 78, f"LAT {lat:.5f}" if isinstance(lat, (int, float)) else "LAT --", accent)
            self.__draw_text(draw, 178, 96, f"LON {lon:.5f}" if isinstance(lon, (int, float)) else "LON --", accent)

        self.__draw_label(draw, 178, 114, "ENV", accent)

        temp_norm = self.__norm_range(temp, 30, 100)
        hum_norm = self.__norm_range(humidity, 0, 100)
        prs_norm = self.__norm_range(pressure, 950, 1050)

        self.__draw_text(draw, 178, 132, "TMP", accent)
        self.__draw_meter(draw, 210, 132, 60, 10, temp_norm, accent, segments=6)
        self.__draw_text(draw, 276, 130, f"{temp:.1f}" if isinstance(temp, (int, float)) else "--", accent)

        self.__draw_text(draw, 178, 146, "HUM", accent)
        self.__draw_meter(draw, 210, 146, 60, 10, hum_norm, accent, segments=6)
        self.__draw_text(draw, 276, 144, f"{humidity:.0f}%" if isinstance(humidity, (int, float)) else "--", accent)

        self.__draw_text(draw, 178, 160, "PRS", accent)
        self.__draw_meter(draw, 210, 160, 60, 10, prs_norm, accent, segments=6)
        self.__draw_text(draw, 276, 158, f"{pressure:.0f}" if isinstance(pressure, (int, float)) else "--", accent)

        self.__draw_ticker(draw, msg_x0, msg_y0, msg_x1, msg_y1, accent)

        return image, 0, 0