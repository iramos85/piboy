import logging

from smbus2 import SMBus
import bme280

from core.data import DeviceStatus
from core.decorator import override
from data.EnvironmentDataProvider import EnvironmentData, EnvironmentDataProvider

logger = logging.getLogger('environment_data')


class BME280EnvironmentDataProvider(EnvironmentDataProvider):
    """Environmental data provider using a BME280 over I2C via smbus2 + RPi.bme280."""

    def __init__(self, port: int, address: int):
        self.__port = int(port)
        self.__address = int(address)
        self.__device_status = DeviceStatus.UNAVAILABLE
        self.__cal = None

        try:
            with SMBus(self.__port) as bus:
                self.__cal = bme280.load_calibration_params(bus, self.__address)
            self.__device_status = DeviceStatus.OPERATIONAL
        except Exception as e:
            self.__cal = None
            self.__device_status = DeviceStatus.UNAVAILABLE
            logger.warning("BME280 init failed: %s", e)

    @override
    def get_environment_data(self) -> EnvironmentData | None:
        if self.__cal is None:
            return None

        try:
            with SMBus(self.__port) as bus:
                data = bme280.sample(bus, self.__address, self.__cal)

            # RPi.bme280 returns humidity in %RH (0..100) — DO NOT divide by 100
            self.__device_status = DeviceStatus.OPERATIONAL
            return EnvironmentData(
                float(data.temperature),
                float(data.pressure),
                float(data.humidity),
            )
        except OSError as e:
            logger.warning(e)
            self.__device_status = DeviceStatus.NO_DATA
            return None

    @override
    def get_device_status(self) -> DeviceStatus:
        return self.__device_status
