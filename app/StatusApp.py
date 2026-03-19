import logging
import shutil
import socket
import subprocess
import threading
import time
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
    __MAX_SCAN_RESULTS = 6

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

        self.__view_mode = "status"   # status | menu | scan | detail | netinfo
        self.__menu_items = [
            "REFRESH",
            "SCAN WIFI",
            "NET DETAILS",
            "DISCONNECT WIFI",
            "RECONNECT WIFI",
            "BACK",
        ]
        self.__menu_index = 0

        self.__scan_results: list[dict] = []
        self.__scan_selected_index = 0
        self.__detail_menu_index = 0
        self.__netinfo_menu_index = 0

        self.__last_action_message = "READY"
        self.__last_action_time = 0.0
        self.__last_scan_time = "NEVER"
        self.__scan_lock = threading.Lock()
        self.__scan_in_progress = False

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
                timeout=6,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            stderr = (result.stderr or "").strip()
            if stderr:
                logger.debug("Command stderr for %s: %s", command, stderr)
            return ""
        except Exception as ex:
            logger.debug("Command failed %s: %s", command, ex)
            return ""

    @staticmethod
    def __run_command_rc(command: list[str]) -> tuple[int, str, str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=8,
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except Exception as ex:
            return 999, "", str(ex)

    def __set_action_message(self, message: str):
        self.__last_action_message = message[:40]
        self.__last_action_time = time.monotonic()
        logger.info("StatusApp action: %s", self.__last_action_message)

    def __get_action_message(self) -> str:
        if time.monotonic() - self.__last_action_time <= 8:
            return self.__last_action_message
        return "READY"

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

    def __get_default_gateway(self) -> str:
        out = self.__run_command(["sh", "-c", "ip route | awk '/default/ {print $3; exit}'"])
        return out if out else "--"

    def __get_interface_name(self) -> str:
        out = self.__run_command(["sh", "-c", "iw dev 2>/dev/null | awk '$1==\"Interface\" {print $2; exit}'"])
        return out if out else "wlan0"

    def __get_ssid(self) -> str:
        ssid = self.__run_command(["iwgetid", "-r"])
        return ssid if ssid else "disconnected"

    def __get_active_connection_name(self) -> str:
        out = self.__run_command(
            ["sh", "-c", "nmcli -t -f NAME,DEVICE connection show --active | awk -F: '$2!=\"lo\" && $2!=\"\" {print $1; exit}'"]
        )
        return out if out else ""

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
            return f"{temp_f:.1f}F {data.humidity:.0f}% {data.pressure:.0f}"
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

    def __current_network(self) -> str:
        return self.__get_ssid()

    def __saved_connections(self) -> set[str]:
        out = self.__run_command(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show"])
        saved = set()
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split(":")
            if parts and parts[0].strip():
                saved.add(parts[0].strip())
        return saved

    def __scan_wifi_worker(self):
        with self.__scan_lock:
            if self.__scan_in_progress:
                return
            self.__scan_in_progress = True

        try:
            self.__set_action_message("SCANNING WIFI...")
            rc, out, err = self.__run_command_rc(
                ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes"]
            )

            if rc != 0:
                logger.warning("Wi-Fi scan failed: %s", err)
                self.__scan_results = []
                self.__view_mode = "scan"
                self.__set_action_message("SCAN FAILED")
                return

            saved = self.__saved_connections()
            current = self.__current_network()

            parsed: list[dict] = []
            seen = set()

            for line in out.splitlines():
                if not line.strip():
                    continue

                parts = line.split(":")
                if len(parts) < 4:
                    continue

                active = parts[0].strip()
                signal_text = parts[-2].strip()
                security = parts[-1].strip() or "OPEN"
                ssid = ":".join(parts[1:-2]).strip()

                if not ssid:
                    ssid = "<hidden>"

                if ssid in seen:
                    continue
                seen.add(ssid)

                try:
                    signal = int(signal_text)
                except Exception:
                    signal = 0

                parsed.append(
                    {
                        "ssid": ssid,
                        "signal": signal,
                        "security": security,
                        "active": active == "yes" or ssid == current,
                        "saved": ssid in saved,
                    }
                )

            parsed.sort(key=lambda x: x["signal"], reverse=True)
            self.__scan_results = parsed[: self.__MAX_SCAN_RESULTS]
            self.__scan_selected_index = 0
            self.__detail_menu_index = 0
            self.__view_mode = "scan"
            self.__last_scan_time = time.strftime("%H:%M:%S")

            if self.__scan_results:
                self.__set_action_message(f"FOUND {len(self.__scan_results)} NETS")
            else:
                self.__set_action_message("NO NETWORKS FOUND")

            try:
                self._SelfUpdatingApp__update_callback(True)
            except Exception:
                pass

        finally:
            with self.__scan_lock:
                self.__scan_in_progress = False

    def __start_wifi_scan(self):
        with self.__scan_lock:
            if self.__scan_in_progress:
                self.__set_action_message("SCAN ALREADY RUNNING")
                return

        threading.Thread(target=self.__scan_wifi_worker, daemon=True).start()

    def __disconnect_wifi(self):
        active_name = self.__get_active_connection_name()
        iface = self.__get_interface_name()

        rc, _, err = 999, "", "No active connection"

        if active_name:
            rc, _, err = self.__run_command_rc(["nmcli", "connection", "down", active_name])
            if rc == 0:
                self.__set_action_message(f"DOWN {active_name[:24]}")
                return

        rc, _, err = self.__run_command_rc(["nmcli", "device", "disconnect", iface])
        if rc == 0:
            self.__set_action_message(f"DOWN {iface}")
        else:
            logger.warning("Disconnect failed: %s", err)
            self.__set_action_message("DISCONNECT FAILED")

    def __reconnect_wifi(self):
        iface = self.__get_interface_name()
        active_name = self.__get_active_connection_name()
        current_ssid = self.__current_network()

        self.__run_command_rc(["nmcli", "radio", "wifi", "on"])

        target = active_name or (current_ssid if current_ssid not in ("disconnected", "offline") else "")

        if target:
            rc, _, err = self.__run_command_rc(["nmcli", "connection", "up", target])
            if rc == 0:
                self.__set_action_message(f"UP {target[:26]}")
                return
            logger.warning("Reconnect by connection failed for %s: %s", target, err)

        rc, _, err = self.__run_command_rc(["nmcli", "device", "connect", iface])
        if rc == 0:
            self.__set_action_message(f"UP {iface}")
        else:
            logger.warning("Reconnect failed: %s", err)
            self.__set_action_message("RECONNECT FAILED")

    def __connect_selected_network(self):
        if not self.__scan_results:
            self.__set_action_message("NO NETWORK SELECTED")
            return

        net = self.__scan_results[self.__scan_selected_index]
        ssid = net["ssid"]

        if not net["saved"]:
            self.__set_action_message("NOT A SAVED NETWORK")
            return

        self.__run_command_rc(["nmcli", "radio", "wifi", "on"])
        rc, _, err = self.__run_command_rc(["nmcli", "connection", "up", ssid])
        if rc == 0:
            self.__set_action_message(f"CONNECTED {ssid[:18]}")
            self.__view_mode = "scan"
        else:
            logger.warning("Connect failed for %s: %s", ssid, err)
            self.__set_action_message("CONNECT FAILED")

    def __detail_menu_items(self) -> list[str]:
        if not self.__scan_results:
            return ["BACK"]

        net = self.__scan_results[self.__scan_selected_index]
        items = []

        if net["active"]:
            items.append("DISCONNECT")
        elif net["saved"]:
            items.append("CONNECT")
        else:
            items.append("UNSAVED")

        items.append("BACK")
        return items

    def __netinfo_menu_items(self) -> list[str]:
        if self.__get_ssid() not in ("disconnected", "offline"):
            return ["DISCONNECT", "BACK"]
        return ["BACK"]

    def __execute_selected_action(self):
        item = self.__menu_items[self.__menu_index]

        if item == "REFRESH":
            self.__set_action_message("REFRESHED")
        elif item == "SCAN WIFI":
            self.__start_wifi_scan()
        elif item == "NET DETAILS":
            self.__netinfo_menu_index = 0
            self.__view_mode = "netinfo"
            self.__set_action_message("NETWORK DETAILS")
        elif item == "DISCONNECT WIFI":
            self.__disconnect_wifi()
        elif item == "RECONNECT WIFI":
            self.__reconnect_wifi()
        elif item == "BACK":
            self.__view_mode = "status"
            self.__set_action_message("BACK TO STATUS")

    def __execute_detail_action(self):
        item = self.__detail_menu_items()[self.__detail_menu_index]

        if item == "CONNECT":
            self.__connect_selected_network()
        elif item == "DISCONNECT":
            self.__disconnect_wifi()
            self.__set_action_message("DISCONNECTED ACTIVE NET")
        elif item == "UNSAVED":
            self.__set_action_message("PASSWORD ENTRY LATER")
        elif item == "BACK":
            self.__view_mode = "scan"
            self.__set_action_message("BACK TO SCAN")

    def __execute_netinfo_action(self):
        item = self.__netinfo_menu_items()[self.__netinfo_menu_index]
        if item == "DISCONNECT":
            self.__disconnect_wifi()
        elif item == "BACK":
            self.__view_mode = "menu"
            self.__set_action_message("BACK TO MENU")

    @override
    def on_app_enter(self):
        super().on_app_enter()
        self.__set_action_message("STATUS READY")

    @override
    def on_app_leave(self):
        super().on_app_leave()
        self.__view_mode = "status"

    @override
    def on_key_up(self):
        if self.__view_mode == "menu":
            self.__menu_index = (self.__menu_index - 1) % len(self.__menu_items)
        elif self.__view_mode == "scan" and self.__scan_results:
            self.__scan_selected_index = (self.__scan_selected_index - 1) % len(self.__scan_results)
        elif self.__view_mode == "detail":
            self.__detail_menu_index = (self.__detail_menu_index - 1) % len(self.__detail_menu_items())
        elif self.__view_mode == "netinfo":
            self.__netinfo_menu_index = (self.__netinfo_menu_index - 1) % len(self.__netinfo_menu_items())

    @override
    def on_key_down(self):
        if self.__view_mode == "menu":
            self.__menu_index = (self.__menu_index + 1) % len(self.__menu_items)
        elif self.__view_mode == "scan" and self.__scan_results:
            self.__scan_selected_index = (self.__scan_selected_index + 1) % len(self.__scan_results)
        elif self.__view_mode == "detail":
            self.__detail_menu_index = (self.__detail_menu_index + 1) % len(self.__detail_menu_items())
        elif self.__view_mode == "netinfo":
            self.__netinfo_menu_index = (self.__netinfo_menu_index + 1) % len(self.__netinfo_menu_items())

    @override
    def on_key_a(self):
        if self.__view_mode == "status":
            self.__view_mode = "menu"
            self.__set_action_message("MENU OPEN")
        elif self.__view_mode == "menu":
            self.__execute_selected_action()
        elif self.__view_mode == "scan":
            if self.__scan_results:
                self.__detail_menu_index = 0
                self.__view_mode = "detail"
                self.__set_action_message("NET DETAILS")
        elif self.__view_mode == "detail":
            self.__execute_detail_action()
        elif self.__view_mode == "netinfo":
            self.__execute_netinfo_action()

    @override
    def on_key_b(self):
        if self.__view_mode == "status":
            self.__view_mode = "menu"
            self.__set_action_message("MENU OPEN")
        elif self.__view_mode == "menu":
            self.__view_mode = "status"
            self.__set_action_message("BACK TO STATUS")
        elif self.__view_mode == "scan":
            self.__view_mode = "menu"
            self.__set_action_message("BACK TO MENU")
        elif self.__view_mode == "detail":
            self.__view_mode = "scan"
            self.__set_action_message("BACK TO SCAN")
        elif self.__view_mode == "netinfo":
            self.__view_mode = "menu"
            self.__set_action_message("BACK TO MENU")

    def __draw_status_view(self, image: Image.Image) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)
        font = self.__app_config.font_standard
        small_font = self.__app_config.font_header

        x = self.__LEFT
        y = self.__TOP

        lines = [
            "STATUS",
            f"HOST {self.__get_hostname()}",
            f"NET  {self.__get_ssid()}",
            f"IP   {self.__get_ip_address()}",
            f"SIG  {self.__get_wifi_signal_percent()} {self.__get_connection_text()}",
            "",
            f"PWR  {self.__get_battery_text()} {self.__get_battery_status_text()}",
            f"CPU  {self.__get_cpu_temp_c()}",
            f"RAM  {self.__get_ram_percent()}",
            f"DSK  {self.__get_disk_free()} FREE",
            f"UP   {self.__get_uptime()}",
            "",
            f"ENV  {self.__get_env_text()}",
            f"GPS  {self.__get_gps_text()}",
            "",
            f"WARN {self.__get_warning_text()}",
            f"MSG  {self.__get_action_message()}",
        ]

        for i, line in enumerate(lines):
            use_font = small_font if i == 0 else font
            draw.text((x, y), line[:34], self.__app_config.accent, font=use_font)
            y += self.__LINE_HEIGHT if i != 0 else 22

        return image, 0, 0

    def __draw_menu_view(self, image: Image.Image) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)
        font = self.__app_config.font_standard
        small_font = self.__app_config.font_header

        x = self.__LEFT
        y = self.__TOP

        draw.text((x, y), "STATUS MENU", self.__app_config.accent, font=small_font)
        y += 24

        for idx, item in enumerate(self.__menu_items):
            prefix = ">" if idx == self.__menu_index else " "
            draw.text((x, y), f"{prefix} {item}", self.__app_config.accent, font=font)
            y += self.__LINE_HEIGHT

        y += 6
        draw.text((x, y), f"MSG {self.__get_action_message()}"[:34], self.__app_config.accent, font=font)
        y += self.__LINE_HEIGHT
        draw.text((x, y), "A=RUN  B=BACK", self.__app_config.accent, font=font)

        return image, 0, 0

    def __draw_scan_view(self, image: Image.Image) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)
        font = self.__app_config.font_standard
        small_font = self.__app_config.font_header

        x = self.__LEFT
        y = self.__TOP

        draw.text((x, y), "WIFI SCAN", self.__app_config.accent, font=small_font)
        y += 24

        if self.__scan_in_progress:
            draw.text((x, y), "SCANNING...", self.__app_config.accent, font=font)
            y += self.__LINE_HEIGHT
        elif not self.__scan_results:
            draw.text((x, y), "NO RESULTS", self.__app_config.accent, font=font)
            y += self.__LINE_HEIGHT
        else:
            for idx, net in enumerate(self.__scan_results):
                prefix = ">" if idx == self.__scan_selected_index else " "
                flags = ""
                if net["active"]:
                    flags += "*"
                if net["saved"]:
                    flags += "S"
                line = f"{prefix} {net['ssid'][:14]:14} {net['signal']:>3}% {flags}"
                draw.text((x, y), line[:34], self.__app_config.accent, font=font)
                y += self.__LINE_HEIGHT

        y += 6
        draw.text((x, y), f"SCAN {self.__last_scan_time}"[:34], self.__app_config.accent, font=font)
        y += self.__LINE_HEIGHT
        draw.text((x, y), "*=LIVE S=SAVED", self.__app_config.accent, font=font)
        y += self.__LINE_HEIGHT
        draw.text((x, y), "A=DETAIL  B=MENU", self.__app_config.accent, font=font)

        return image, 0, 0

    def __draw_detail_view(self, image: Image.Image) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)
        font = self.__app_config.font_standard
        small_font = self.__app_config.font_header

        x = self.__LEFT
        y = self.__TOP

        if not self.__scan_results:
            draw.text((x, y), "NET DETAIL", self.__app_config.accent, font=small_font)
            y += 24
            draw.text((x, y), "NO NETWORK", self.__app_config.accent, font=font)
            return image, 0, 0

        net = self.__scan_results[self.__scan_selected_index]

        draw.text((x, y), "NET DETAIL", self.__app_config.accent, font=small_font)
        y += 24

        lines = [
            f"SSID {net['ssid']}",
            f"SIG  {net['signal']}%",
            f"SEC  {net['security'][:18]}",
            f"LIVE {'YES' if net['active'] else 'NO'}",
            f"SAVE {'YES' if net['saved'] else 'NO'}",
            "",
        ]

        for line in lines:
            draw.text((x, y), line[:34], self.__app_config.accent, font=font)
            y += self.__LINE_HEIGHT

        for idx, item in enumerate(self.__detail_menu_items()):
            prefix = ">" if idx == self.__detail_menu_index else " "
            draw.text((x, y), f"{prefix} {item}", self.__app_config.accent, font=font)
            y += self.__LINE_HEIGHT

        y += 4
        draw.text((x, y), f"MSG {self.__get_action_message()}"[:34], self.__app_config.accent, font=font)

        return image, 0, 0

    def __draw_netinfo_view(self, image: Image.Image) -> tuple[Image.Image, int, int]:
        draw = ImageDraw.Draw(image)
        font = self.__app_config.font_standard
        small_font = self.__app_config.font_header

        x = self.__LEFT
        y = self.__TOP

        draw.text((x, y), "NET INFO", self.__app_config.accent, font=small_font)
        y += 24

        lines = [
            f"SSID {self.__get_ssid()}",
            f"IP   {self.__get_ip_address()}",
            f"GW   {self.__get_default_gateway()}",
            f"IF   {self.__get_interface_name()}",
            f"CONN {self.__get_active_connection_name() or '--'}",
            f"SIG  {self.__get_wifi_signal_percent()}",
            f"NET  {self.__get_connection_text()}",
            "",
        ]

        for line in lines:
            draw.text((x, y), line[:34], self.__app_config.accent, font=font)
            y += self.__LINE_HEIGHT

        for idx, item in enumerate(self.__netinfo_menu_items()):
            prefix = ">" if idx == self.__netinfo_menu_index else " "
            draw.text((x, y), f"{prefix} {item}", self.__app_config.accent, font=font)
            y += self.__LINE_HEIGHT

        y += 4
        draw.text((x, y), f"MSG {self.__get_action_message()}"[:34], self.__app_config.accent, font=font)

        return image, 0, 0

    @override
    def draw(self, image: Image.Image, partial=False) -> tuple[Image.Image, int, int]:
        if self.__view_mode == "menu":
            return self.__draw_menu_view(image)
        if self.__view_mode == "scan":
            return self.__draw_scan_view(image)
        if self.__view_mode == "detail":
            return self.__draw_detail_view(image)
        if self.__view_mode == "netinfo":
            return self.__draw_netinfo_view(image)
        return self.__draw_status_view(image)