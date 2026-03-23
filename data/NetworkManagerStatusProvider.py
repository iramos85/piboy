import logging
import subprocess
import threading
import time

from core.data import ConnectionStatus
from core.decorator import override
from data.NetworkStatusProvider import NetworkStatusProvider

logger = logging.getLogger("network_data")


class NetworkManagerStatusProvider(NetworkStatusProvider):
    """Data provider for network status using NetworkManager / nmcli."""

    __status = ConnectionStatus.DISCONNECTED
    __ssid = None
    __ip_address = None

    def __init__(self):
        self.__thread = threading.Thread(target=self.__update_status, args=(), daemon=True)
        self.__thread.start()

    def __run_nmcli(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["nmcli", *args],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode != 0:
                logger.debug("nmcli command failed: %s // %s", args, result.stderr.strip())
                return ""
            return result.stdout.strip()
        except Exception as ex:
            logger.debug("nmcli command exception for %s: %s", args, ex)
            return ""

    def __update_status(self):
        while True:
            try:
                connectivity = self.__run_nmcli(["networking", "connectivity"])
                self.__status = (
                    ConnectionStatus.CONNECTED
                    if "full" in connectivity.lower()
                    else ConnectionStatus.DISCONNECTED
                )

                active_wifi = self.__run_nmcli(["-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
                ssid = None
                for line in active_wifi.splitlines():
                    if line.startswith("yes:"):
                        ssid = line.split(":", 1)[1].strip() or None
                        break
                self.__ssid = ssid

                ip_addr = self.__run_nmcli(["-t", "-f", "IP4.ADDRESS", "dev", "show", "wlan0"])
                if ip_addr:
                    # Example: IP4.ADDRESS[1]:192.168.1.20/24
                    first_line = ip_addr.splitlines()[0]
                    if ":" in first_line:
                        first_line = first_line.split(":", 1)[1].strip()
                    if "/" in first_line:
                        first_line = first_line.split("/", 1)[0].strip()
                    self.__ip_address = first_line or None
                else:
                    self.__ip_address = None

                logger.debug(
                    "Network status=%s ssid=%s ip=%s",
                    self.__status,
                    self.__ssid,
                    self.__ip_address,
                )
            except Exception as ex:
                logger.exception("NetworkManagerStatusProvider update failed: %s", ex)
                self.__status = ConnectionStatus.DISCONNECTED
                self.__ssid = None
                self.__ip_address = None

            time.sleep(10)

    @override
    def get_connection_status(self) -> ConnectionStatus:
        return self.__status

    def get_ssid(self) -> str | None:
        return self.__ssid

    def get_ip_address(self) -> str | None:
        return self.__ip_address