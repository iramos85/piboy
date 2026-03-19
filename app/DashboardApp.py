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

    def __draw_section(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        title: str,
        lines: list[str],
    ) -> int:
        accent = self.__app_config.accent
        font_header = self.__app_config.font_header
        font_body = self.__app_config.font_standard

        draw.text((x, y), title, fill=accent, font=font_header)
        y += 18

        for line in lines:
            draw.text((x, y), line, fill=accent, font=font_body)
            y += 16

        return y

    @override
    def draw(self, image: Image.Image, partial=False) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)

        width, height = image.size
        accent = self.__app_config.accent
        background = self.__app_config.background
        font_header = self.__app_config.font_header
        font_body = self.__app_config.font_standard

        draw.rectangle((0, 0, width, height), fill=background)

        # Frame lines
        draw.rectangle((0, 0, width - 1, height - 1), outline=accent)
        draw.line((width // 2, 8, width // 2, height - 28), fill=accent)
        draw.line((8, height - 28, width - 8, height - 28), fill=accent)

        # Header strip
        now_str = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p")
        draw.text((10, 6), "PIP-BOY STATUS HUB", fill=accent, font=font_header)

        time_bbox = font_body.getbbox(now_str)
        time_width = time_bbox[2] - time_bbox[0]
        draw.text((width - time_width - 10, 8), now_str, fill=accent, font=font_body)

        # ---- System / Network ----
        connection_status = self.__safe_call(self.__network_status_provider.get_connection_status)
        battery_soc = self.__safe_call(self.__battery_status_provider.get_state_of_charge)
        battery_device_status = self.__safe_call(self.__battery_status_provider.get_device_status)

        sys_lines = []

        if battery_soc is None:
            sys_lines.append("BAT: --")
        else:
            sys_lines.append(f"BAT: {battery_soc:.0%}")

        if battery_device_status is None:
            sys_lines.append("PWR: --")
        else:
            sys_lines.append(f"PWR: {str(battery_device_status).split('.')[-1]}")

        if connection_status is None:
            sys_lines.append("NET: --")
        else:
            sys_lines.append(f"NET: {str(connection_status).split('.')[-1]}")

        # best-effort network details
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

        sys_lines.append(f"IP : {ip_addr if ip_addr else '--'}")
        sys_lines.append(f"AP : {ssid if ssid else '--'}")

        # ---- GPS ----
        gps_lines = []
        gps_status = self.__safe_call(self.__location_provider.get_device_status)
        location = self.__safe_call(self.__location_provider.get_location)

        if gps_status is None:
            gps_lines.append("STS: --")
        else:
            gps_lines.append(f"STS: {str(gps_status).split('.')[-1]}")

        lat = None
        lon = None

        if location is not None:
            lat = getattr(location, "latitude", None)
            lon = getattr(location, "longitude", None)

        gps_lines.append(f"LAT: {lat:.5f}" if isinstance(lat, (int, float)) else "LAT: --")
        gps_lines.append(f"LON: {lon:.5f}" if isinstance(lon, (int, float)) else "LON: --")

        # ---- Environment ----
        env_lines = []
        env_status = self.__safe_call(self.__environment_data_provider.get_device_status)
        env_data = self.__safe_call(self.__environment_data_provider.get_environment_data)

        if env_status is None:
            env_lines.append("STS: --")
        else:
            env_lines.append(f"STS: {str(env_status).split('.')[-1]}")

        temp = getattr(env_data, "temperature", None) if env_data is not None else None
        humidity = getattr(env_data, "humidity", None) if env_data is not None else None
        pressure = getattr(env_data, "pressure", None) if env_data is not None else None

        env_lines.append(f"TMP: {temp:.1f}" if isinstance(temp, (int, float)) else "TMP: --")
        env_lines.append(f"HUM: {humidity:.0f}%" if isinstance(humidity, (int, float)) else "HUM: --")
        env_lines.append(f"PRS: {pressure:.0f}" if isinstance(pressure, (int, float)) else "PRS: --")

        # ---- Console / summary line ----
        status_line = "ALL SYSTEMS NOMINAL"

        if env_status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
            status_line = "ENV SENSOR NOT READY"
        elif gps_status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
            status_line = "GPS NOT READY"
        elif connection_status == ConnectionStatus.DISCONNECTED:
            status_line = "NETWORK DISCONNECTED"
        elif battery_soc is not None and battery_soc <= 0.20:
            status_line = "LOW BATTERY WARNING"

        # Draw sections
        self.__draw_section(draw, 10, 28, "SYS", sys_lines)
        self.__draw_section(draw, width // 2 + 10, 28, "GPS", gps_lines)
        self.__draw_section(draw, 10, 118, "ENV", env_lines)

        draw.text((10, height - 22), f"MSG: {status_line}", fill=accent, font=font_body)

        return image, 0, 0