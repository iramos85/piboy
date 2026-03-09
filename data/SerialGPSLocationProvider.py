import io
import logging
import threading
import time
from typing import Union

import pynmea2
import serial
from pynmea2.types.talker import GGA, RMC, GLL

from core.data import DeviceStatus
from core.decorator import override
from data.LocationProvider import Location, LocationException, LocationProvider

logger = logging.getLogger('location_data')


class SerialGPSLocationProvider(LocationProvider):

    def __init__(self, port: str, baud_rate=9600):
        self.__device = serial.Serial(port, baudrate=baud_rate, timeout=0.5)
        self.__io_wrapper = io.TextIOWrapper(io.BufferedReader(self.__device), encoding='ascii', errors='ignore')
        self.__location: Union[Location, None] = None
        self.__device_status = DeviceStatus.UNAVAILABLE
        self.__thread = threading.Thread(target=self.__update_location, args=(), daemon=True)
        self.__thread.start()

    def __set_location(self, latitude: float, longitude: float):
        self.__device_status = DeviceStatus.OPERATIONAL
        self.__location = Location(latitude, longitude)

    def __clear_location(self):
        self.__device_status = DeviceStatus.NO_DATA
        self.__location = None

    def __update_location(self):
        while True:
            try:
                data = self.__io_wrapper.readline().strip()
                if len(data) == 0:
                    self.__device_status = DeviceStatus.UNAVAILABLE
                    continue

                logger.debug(data)

                # Parse all valid NMEA sentences
                message = pynmea2.parse(data)

                # GLL: Geographic Position - Latitude/Longitude
                if isinstance(message, GLL):
                    if message.lat and message.lon:
                        self.__set_location(message.latitude, message.longitude)
                    else:
                        self.__clear_location()

                # GGA: Fix data
                elif isinstance(message, GGA):
                    if message.lat and message.lon and str(message.gps_qual) != '0':
                        self.__set_location(message.latitude, message.longitude)
                    else:
                        self.__clear_location()

                # RMC: Recommended minimum navigation info
                elif isinstance(message, RMC):
                    if message.lat and message.lon and message.status == 'A':
                        self.__set_location(message.latitude, message.longitude)
                    else:
                        self.__clear_location()

            except serial.SerialException as e:
                logger.warning(e)
                self.__device_status = DeviceStatus.UNAVAILABLE
                time.sleep(5)
            except pynmea2.ParseError as e:
                logger.warning(e)
            except UnicodeDecodeError as e:
                logger.warning(e)

    @override
    def get_location(self) -> Location:
        if self.__location is None:
            raise LocationException('GPS module has currently no signal')
        return self.__location

    @override
    def get_device_status(self) -> DeviceStatus:
        return self.__device_status