import logging
import shutil
import socket
import subprocess
from typing import Callable, Optional

from injector import inject
from PIL import Image, ImageDraw

from app.App import SelfUpdatingApp
from core.decorator import override
from core.data import ConnectionStatus, DeviceStatus
from data.BatteryStatusProvider import BatteryStatusProvider
from data.EnvironmentDataProvider import EnvironmentDataProvider
from data.LocationProvider import LocationProvider
from data.NetworkStatusProvider import NetworkStatusProvider
from environment import AppConfig

logger = logging.getLogger("app")


class StatusApp(SelfUpdatingApp):
    __LINE_HEIGHT = 18
    __LEFT = 6
    __TOP = 6

    @inject
    def __init__(
        self,
        app_config: AppConfig,
        draw_callback: Callable[[bool], None],
        network_status_provider: NetworkStatusProvider,
        battery_status_provider: BatteryStatusProvider,
        environment_data_provider: EnvironmentDataProvider,
        location_provider: LocationProvider,
    ):
        super().__init__(draw_callback)

        self.__app_config = app_config
        self.__network_status_provider = network_status_provider
        self.__battery_status_provider = battery_status_provider
        self.__environment_data_provider = environment_data_provider
        self.__location_provider = location_provider

    @property
    @override
    def title(self) -> str:
        return "STAT"

    @property
    @override
    def refresh_time(self) -> float:
        return 3.0

    @staticmethod
    def __run_command(command: list[str]) -> str:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return ""
        except Exception as ex:
            logger.debug("Command failed %s: %s", command, ex)
            return ""

    def __get_hostname(self) -> str:
        try:
            return socket.gethostname()
        except Exception:
            return "unknown"

    def __get_ip_address(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "offline"

    def __get_ssid(self) -> str:
        ssid = self.__run_command(["iwgetid", "-r"])
        return ssid if ssid else "disconnected"

    def __get_wifi_signal_percent(self) -> str:
        output = self.__run_command(["sh", "-c", "iwconfig 2>/dev/null"])
        if not output:
            return "--"

        for line in output.splitlines():
            if "Link Quality=" in line:
                try:
                    part = line.split("Link Quality=")[1].split(" ")[0]
                    if "/" in part:
                        current, maximum = part.split("/", 1)
                        pct = round((int(current) / int(maximum)) * 100)
                        return f"{pct}%"
                except Exception:
                    logger.debug("Failed parsing Link Quality from iwconfig output")
                    return "--"

            if "Signal level=" in line:
                try:
                    level_part = line.split("Signal level=")[1].split(" ")[0]
                    if "dBm" in level_part:
                        dbm = int(level_part.replace("dBm", ""))
                        pct = max(0, min(100, 2 * (dbm + 100)))
                        return f"{pct}%"
                except Exception:
                    logger.debug("Failed parsing Signal level from iwconfig output")
                    return "--"

        return "--"

    def __get_connection_text(self) -> str:
        try:
            status = self.__network_status_provider.get_connection_status()
            return "ONLINE" if status == ConnectionStatus.CONNECTED else "OFFLINE"
        except Exception:
            logger.exception("Failed reading network connection status")
            return "UNKNOWN"

    def __get_cpu_temp_c(self) -> str:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r", encoding="utf-8") as f:
                temp_c = int(f.read().strip()) / 1000.0
            return f"{temp_c:.1f}C"
        except Exception:
            return "--"

    def __get_ram_percent(self) -> str:
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                lines = f.readlines()

            mem_total = 0
            mem_available = 0

            for line in lines:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_available = int(line.split()[1])

            if mem_total <= 0:
                return "--"

            used_pct = round((1 - (mem_available / mem_total)) * 100)
            return f"{used_pct}%"
        except Exception:
            logger.exception("Failed reading RAM usage")
            return "--"

    def __get_disk_free(self) -> str:
        try:
            _, _, free = shutil.disk_usage("/")
            return f"{free / (1024 ** 3):.1f}G"
        except Exception:
            return "--"

    def __get_uptime(self) -> str:
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                seconds = float(f.read().split()[0])

            days = int(seconds // 86400)
            hours = int((seconds % 86400) // 3600)
            minutes = int((seconds % 3600) // 60)

            if days > 0:
                return f"{days}d {hours}h {minutes}m"
            return f"{hours}h {minutes}m"
        except Exception:
            return "--"

    def __get_battery_text(self) -> str:
        try:
            soc = self.__battery_status_provider.get_state_of_charge()
            return f"{soc:.0%}"
        except Exception:
            logger.exception("Failed reading battery state of charge")
            return "--"

    def __get_battery_status_text(self) -> str:
        try:
            soc = self.__battery_status_provider.get_state_of_charge()
            if soc <= 0.20:
                return "LOW"
            return "OK"
        except Exception:
            return "ERR"

    def __get_env_text(self) -> str:
        try:
            status = self.__environment_data_provider.get_device_status()
            if status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
                return "ERR"

            data = self.__environment_data_provider.get_environment_data()
            if data is None:
                return "ERR"

            temp_f = (data.temperature * 9 / 5) + 32
            return f"{temp_f:.1f}F {data.humidity:.0f}% {data.pressure:.0f}hPa"
        except Exception:
            logger.exception("Failed reading environment data")
            return "ERR"

    def __get_gps_text(self) -> str:
        try:
            status = self.__location_provider.get_device_status()
            if status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
                return "NO LOCK"

            location = self.__location_provider.get_location()
            if location is None:
                return "NO LOCK"

            return f"{location.latitude:.2f},{location.longitude:.2f}"
        except Exception:
            logger.exception("Failed reading GPS data")
            return "NO LOCK"

    def __get_warning_text(self) -> Optional[str]:
        try:
            warnings = []

            try:
                if self.__network_status_provider.get_connection_status() != ConnectionStatus.CONNECTED:
                    warnings.append("NO WIFI")
            except Exception:
                warnings.append("NET ERR")

            try:
                if self.__battery_status_provider.get_state_of_charge() <= 0.20:
                    warnings.append("LOW BATT")
            except Exception:
                warnings.append("BATT ERR")

            try:
                temp_text = self.__get_cpu_temp_c()
                if temp_text.endswith("C"):
                    temp_value = float(temp_text[:-1])
                    if temp_value >= 75.0:
                        warnings.append("HOT CPU")
            except Exception:
                pass

            try:
                gps_status = self.__location_provider.get_device_status()
                if gps_status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
                    warnings.append("NO GPS")
            except Exception:
                warnings.append("GPS ERR")

            if not warnings:
                return "ALL NOMINAL"

            return " | ".join(warnings[:2])
        except Exception:
            return "STATUS ERR"

    @override
    def on_key_a(self):
        logger.info("StatusApp refresh requested")

    @override
    def on_key_b(self):
        logger.info("StatusApp long-press action reserved")

    @override
    def draw(self, image: Image.Image, partial=False) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)
        font = self.__app_config.font_standard
        small_font = self.__app_config.font_header

        x = self.__LEFT
        y = self.__TOP

        hostname = self.__get_hostname()
        ssid = self.__get_ssid()
        ip = self.__get_ip_address()
        signal = self.__get_wifi_signal_percent()
        connection = self.__get_connection_text()

        battery = self.__get_battery_text()
        battery_state = self.__get_battery_status_text()
        cpu_temp = self.__get_cpu_temp_c()
        ram = self.__get_ram_percent()
        disk = self.__get_disk_free()
        uptime = self.__get_uptime()

        env = self.__get_env_text()
        gps = self.__get_gps_text()
        warn = self.__get_warning_text()

        lines = [
            "STATUS",
            f"HOST {hostname}",
            f"NET  {ssid}",
            f"IP   {ip}",
            f"SIG  {signal} {connection}",
            "",
            f"PWR  {battery} {battery_state}",
            f"CPU  {cpu_temp}",
            f"RAM  {ram}",
            f"DSK  {disk} FREE",
            f"UP   {uptime}",
            "",
            f"ENV  {env}",
            f"GPS  {gps}",
            "",
            f"WARN {warn}",
        ]

        for i, line in enumerate(lines):
            use_font = small_font if i == 0 else font
            draw.text((x, y), line, self.__app_config.accent, font=use_font)
            y += self.__LINE_HEIGHT if i != 0 else 22

        return image, 0, 0