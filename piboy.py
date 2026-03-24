import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from logging.config import fileConfig
from typing import Any, Callable, Generator, Self
from zoneinfo import ZoneInfo

from injector import Injector, Module, provider, singleton
from PIL import Image, ImageDraw

import environment
from app.App import App
from app.DashboardApp import DashboardApp
from app.MapApp import MapApp
from app.PowerApp import PowerApp
from app.NullApp import NullApp
from app.RadioApp import RadioApp
from app.StatusApp import StatusApp
from app.UpdateApp import UpdateApp
from app.NotificationApp import NotificationApp
from core import resources
from core.data import ConnectionStatus, DeviceStatus
from data.BatteryStatusProvider import BatteryStatusProvider
from data.EnvironmentDataProvider import EnvironmentDataProvider
from data.LocationProvider import LocationProvider
from data.NetworkStatusProvider import NetworkStatusProvider
from data.NotificationManager import NotificationManager
from data.OSMTileProvider import OSMTileProvider
from data.TileProvider import TileProvider
from environment import AppConfig, Environment
from interaction.Display import Display
from interaction.Input import Input
from interaction.UnifiedInteraction import UnifiedInteraction
from status_led import StatusLed

fileConfig(fname="config.ini")
logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Chicago")
TAB_SWITCH_SFX = "media/ui/tab_switch.wav"

STATUS_LED_PIN = 16
LOW_BATTERY_THRESHOLD = 0.20

ENC_A_PIN = 17
ENC_B_PIN = 27
ENC_SW_PIN = 22

ENC_POLL_S = 0.008
ENC_STEP_RATE_LIMIT_S = 0.140
BTN_DEBOUNCE_S = 0.080
BTN_LONGPRESS_S = 0.70


def play_tab_switch_sfx():
    if not os.path.isfile(TAB_SWITCH_SFX):
        return

    def _worker():
        cmds = [
            ["aplay", "-q", "-D", "plughw:CARD=MAX98357A,DEV=0", TAB_SWITCH_SFX],
            ["aplay", "-q", TAB_SWITCH_SFX],
        ]
        for cmd in cmds:
            try:
                result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode == 0:
                    return
            except Exception:
                continue

    threading.Thread(target=_worker, daemon=True).start()


class AppState:
    __bit = 0

    def __init__(
        self,
        e: Environment,
        network_status_provider: NetworkStatusProvider,
        location_provider: LocationProvider,
        battery_status_provider: BatteryStatusProvider,
        environment_data_provider: EnvironmentDataProvider,
    ):
        self.__environment = e
        self.__network_status_provider = network_status_provider
        self.__location_provider = location_provider
        self.__battery_status_provider = battery_status_provider
        self.__environment_data_provider = environment_data_provider
        self.__notification_manager = NotificationManager(max_entries=100)
        self.__image_buffer = self.__init_buffer()
        self.__apps: list[App] = []
        self.__active_app = 0

        self.__display_lock = threading.Lock()
        self.__state_lock = threading.RLock()
        self.__switching_app = False

        self.__notification_manager.push("SYS", "INFO", "Boot sequence started")
        self.__notification_manager.push("SYS", "INFO", "PiBoy services initialized")
        self.__notification_manager.push("SYS", "INFO", "UI ready")

    def __init_buffer(self) -> Image.Image:
        return Image.new("RGB", self.__environment.app_config.resolution, self.__environment.app_config.background)

    def __tick(self):
        self.__bit ^= 1

    def clear_buffer(self) -> Image.Image:
        self.__image_buffer = self.__init_buffer()
        return self.__image_buffer

    def _show_display(self, display: Display, image: Image.Image, x: int, y: int):
        with self.__display_lock:
            display.show(image, x, y)

    def add_app(self, app: App) -> Self:
        self.__apps.append(app)
        return self

    @property
    def tick(self) -> int:
        return self.__bit

    @property
    def environment(self) -> Environment:
        return self.__environment

    @property
    def network_status_provider(self) -> NetworkStatusProvider:
        return self.__network_status_provider

    @property
    def location_provider(self) -> LocationProvider:
        return self.__location_provider

    @property
    def battery_status_provider(self) -> BatteryStatusProvider:
        return self.__battery_status_provider

    @property
    def environment_data_provider(self) -> EnvironmentDataProvider:
        return self.__environment_data_provider

    @property
    def notification_manager(self) -> NotificationManager:
        return self.__notification_manager

    @property
    def image_buffer(self) -> Image.Image:
        return self.__image_buffer

    @property
    def apps(self) -> list[App]:
        return self.__apps

    @property
    def active_app(self) -> App:
        return self.__apps[self.__active_app]

    @property
    def active_app_index(self) -> int:
        return self.__active_app

    def next_app(self):
        self.__active_app += 1
        if not self.__active_app < len(self.__apps):
            self.__active_app = 0

    def previous_app(self):
        self.__active_app -= 1
        if self.__active_app < 0:
            self.__active_app = len(self.__apps) - 1

    def set_active_app_index(self, index: int, display: Display):
        with self.__state_lock:
            if self.__switching_app:
                logger.info("Ignoring selector change while app switch is already in progress")
                return

            if index < 0 or index >= len(self.__apps):
                logger.warning("Rejected app index %s (out of range)", index)
                return
            if index == self.__active_app:
                return

            previous_index = self.__active_app
            previous_title = self.__apps[previous_index].title
            next_title = self.__apps[index].title

            logger.info(
                "Switching app by selector: %s (%s) -> %s (%s)",
                previous_index,
                previous_title,
                index,
                next_title,
            )

            self.__switching_app = True

            try:
                try:
                    self.active_app.on_app_leave()
                except Exception:
                    logger.exception("on_app_leave failed for %s", previous_title)

                self.__active_app = index

                logger.info("Entering app: %s", self.active_app.title)
                self.active_app.on_app_enter()

                if isinstance(self.active_app, NotificationApp):
                    self.notification_manager.mark_all_read()

                play_tab_switch_sfx()
                logger.info("Updating display for app: %s", self.active_app.title)
                self.update_display(display, partial=False, allow_during_switch=True)
                logger.info("Finished app switch to: %s", self.active_app.title)
            except Exception:
                logger.exception("App switch failed for %s; reverting to %s", next_title, previous_title)
                self.__active_app = previous_index
                try:
                    self.active_app.on_app_enter()
                    self.update_display(display, partial=False, allow_during_switch=True)
                except Exception:
                    logger.exception("Failed to recover previous app: %s", previous_title)
            finally:
                self.__switching_app = False

    def get_status_led_mode(self) -> str:
        try:
            env_status = self.environment_data_provider.get_device_status()
        except Exception:
            logger.exception("Failed reading environment device status for status LED")
            return StatusLed.TRIPLE_FLASH

        if env_status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
            return StatusLed.TRIPLE_FLASH

        try:
            battery_soc = self.battery_status_provider.get_state_of_charge()
        except Exception:
            logger.exception("Failed reading battery state of charge for status LED")
            return StatusLed.TRIPLE_FLASH

        if battery_soc <= LOW_BATTERY_THRESHOLD:
            return StatusLed.FAST_BLINK

        try:
            gps_status = self.location_provider.get_device_status()
        except Exception:
            logger.exception("Failed reading GPS device status for status LED")
            return StatusLed.TRIPLE_FLASH

        if gps_status in (DeviceStatus.NO_DATA, DeviceStatus.UNAVAILABLE):
            return StatusLed.SLOW_BLINK

        return StatusLed.ON

    def watch_function(self, display: Display, status_led: StatusLed | None = None):
        while True:
            now = datetime.now(LOCAL_TZ)
            time.sleep(1.0 - now.microsecond / 1_000_000.0)

            if status_led is not None:
                try:
                    status_led.set_mode(self.get_status_led_mode())
                except Exception:
                    logger.exception("Failed updating status LED mode")

            if self.__switching_app:
                self.__tick()
                continue

            if getattr(self.active_app, "title", "") == "HOME":
                try:
                    if hasattr(self.active_app, "tick"):
                        self.active_app.tick()
                    self.update_display(display, partial=False)
                except Exception:
                    logger.exception("Failed to refresh HOME dashboard")
            else:
                image, x0, y0 = draw_footer(self.image_buffer, self)
                self._show_display(display, image, x0, y0)

            self.__tick()

    def update_display(self, display: Display, partial=False, allow_during_switch=False):
        if self.__switching_app and partial and not allow_during_switch:
            logger.info("Skipping partial update while switching apps: app=%s", self.active_app.title)
            return

        logger.info("update_display start: app=%s partial=%s", self.active_app.title, partial)

        with self.__state_lock:
            image = self.clear_buffer()
            app_bbox = (
                self.__environment.app_config.app_side_offset,
                self.__environment.app_config.app_top_offset,
                self.__environment.app_config.width - self.__environment.app_config.app_side_offset,
                self.__environment.app_config.height - self.__environment.app_config.app_bottom_offset,
            )
            x_offset, y_offset = app_bbox[0:2]

            def normalize_draw_result(result):
                if result is None:
                    return []
                if isinstance(result, tuple) and len(result) == 3:
                    return [result]
                return result

            try:
                with self.__display_lock:
                    if partial:
                        app_result = normalize_draw_result(self.active_app.draw(image.crop(app_bbox), partial))
                        for patch, x0, y0 in app_result:
                            display.show(patch, x0 + x_offset, y0 + y_offset)
                    else:
                        for patch, x0, y0 in draw_base(image, self):
                            display.show(patch, x0, y0)

                        app_result = normalize_draw_result(self.active_app.draw(image.crop(app_bbox), partial))
                        for patch, x0, y0 in app_result:
                            image.paste(patch, (x0 + x_offset, y0 + y_offset))

                        display.show(image.crop(app_bbox), x_offset, y_offset)

                logger.info("update_display complete: app=%s partial=%s", self.active_app.title, partial)
            except Exception:
                logger.exception("update_display failed: app=%s partial=%s", self.active_app.title, partial)
                raise

    def on_key_left(self, display: Display):
        self.active_app.on_key_left()
        self.update_display(display, partial=True)

    def on_key_right(self, display: Display):
        self.active_app.on_key_right()
        self.update_display(display, partial=True)

    def on_key_up(self, display: Display):
        self.active_app.on_key_up()
        self.update_display(display, partial=True)

    def on_key_down(self, display: Display):
        self.active_app.on_key_down()
        self.update_display(display, partial=True)

    def on_key_a(self, display: Display):
        self.active_app.on_key_a()
        self.update_display(display, partial=True)

    def on_key_b(self, display: Display):
        self.active_app.on_key_b()
        self.update_display(display, partial=True)

    def on_rotary_increase(self, display: Display):
        with self.__state_lock:
            if self.__switching_app:
                logger.info("Ignoring rotary increase while app switch is already in progress")
                return

            previous_index = self.__active_app
            previous_title = self.active_app.title
            self.__switching_app = True

            try:
                try:
                    self.active_app.on_app_leave()
                except Exception:
                    logger.exception("on_app_leave failed for %s", previous_title)

                self.next_app()
                logger.info("Rotary increase: %s -> %s", previous_title, self.active_app.title)

                self.active_app.on_app_enter()

                if isinstance(self.active_app, NotificationApp):
                    self.notification_manager.mark_all_read()

                play_tab_switch_sfx()
                self.update_display(display, partial=False, allow_during_switch=True)
                logger.info("Rotary increase complete: now on %s", self.active_app.title)
            except Exception:
                logger.exception("Rotary increase failed on app %s", self.active_app.title)
                self.__active_app = previous_index
                try:
                    self.active_app.on_app_enter()
                    self.update_display(display, partial=False, allow_during_switch=True)
                except Exception:
                    logger.exception("Failed to recover previous app after rotary increase")
            finally:
                self.__switching_app = False

    def on_rotary_decrease(self, display: Display):
        with self.__state_lock:
            if self.__switching_app:
                logger.info("Ignoring rotary decrease while app switch is already in progress")
                return

            previous_index = self.__active_app
            previous_title = self.active_app.title
            self.__switching_app = True

            try:
                try:
                    self.active_app.on_app_leave()
                except Exception:
                    logger.exception("on_app_leave failed for %s", previous_title)

                self.previous_app()
                logger.info("Rotary decrease: %s -> %s", previous_title, self.active_app.title)

                self.active_app.on_app_enter()

                if isinstance(self.active_app, NotificationApp):
                    self.notification_manager.mark_all_read()

                play_tab_switch_sfx()
                self.update_display(display, partial=False, allow_during_switch=True)
                logger.info("Rotary decrease complete: now on %s", self.active_app.title)
            except Exception:
                logger.exception("Rotary decrease failed on app %s", self.active_app.title)
                self.__active_app = previous_index
                try:
                    self.active_app.on_app_enter()
                    self.update_display(display, partial=False, allow_during_switch=True)
                except Exception:
                    logger.exception("Failed to recover previous app after rotary decrease")
            finally:
                self.__switching_app = False


class AppModule(Module):
    __unified_instance: UnifiedInteraction | None = None

    def register_external_tk_interaction(self, tk_instance: UnifiedInteraction):
        self.__unified_instance = tk_instance

    @staticmethod
    def __create_tk_interaction(state: AppState, app_config: AppConfig) -> UnifiedInteraction:
        from interaction.TkInteraction import TkInteraction

        return TkInteraction(
            state.on_key_left,
            state.on_key_right,
            state.on_key_up,
            state.on_key_down,
            state.on_key_a,
            state.on_key_b,
            state.on_rotary_increase,
            state.on_rotary_decrease,
            lambda _: None,
            app_config.resolution,
            app_config.background,
            app_config.accent_dark,
        )

    @singleton
    @provider
    def provide_environment(self) -> Environment:
        environment.configure()
        try:
            return environment.load()
        except FileNotFoundError:
            e = Environment()
            environment.save(e)
            return e

    @singleton
    @provider
    def provide_app_config(self, e: Environment) -> AppConfig:
        return e.app_config

    @singleton
    @provider
    def provide_app_state(
        self,
        e: Environment,
        network_status_provider: NetworkStatusProvider,
        location_provider: LocationProvider,
        battery_status_provider: BatteryStatusProvider,
        environment_data_provider: EnvironmentDataProvider,
    ) -> AppState:
        return AppState(e, network_status_provider, location_provider, battery_status_provider, environment_data_provider)

    @singleton
    @provider
    def provide_environment_data_service(self, e: Environment) -> EnvironmentDataProvider:
        if e.is_raspberry_pi:
            from data.BME280EnvironmentDataProvider import BME280EnvironmentDataProvider
            return BME280EnvironmentDataProvider(e.env_sensor_config.port, e.env_sensor_config.address)
        else:
            from data.FakeEnvironmentDataProvider import FakeEnvironmentDataProvider
            return FakeEnvironmentDataProvider()

    @singleton
    @provider
    def provide_location_service(self, e: Environment) -> LocationProvider:
        if e.is_raspberry_pi:
            from data.SerialGPSLocationProvider import SerialGPSLocationProvider
            return SerialGPSLocationProvider(e.gps_module_config.port, baud_rate=e.gps_module_config.baud_rate)
        else:
            from data.IPLocationProvider import IPLocationProvider
            return IPLocationProvider(apply_inaccuracy=True)

    @singleton
    @provider
    def provide_tile_service(self, e: Environment) -> TileProvider:
        return OSMTileProvider(e.app_config.background, e.app_config.accent, e.app_config.font_standard)

    @singleton
    @provider
    def provide_network_status_service(self, e: Environment) -> NetworkStatusProvider:
        if e.is_raspberry_pi:
            from data.NetworkManagerStatusProvider import NetworkManagerStatusProvider
            return NetworkManagerStatusProvider()
        else:
            from data.FakeNetworkStatusProvider import FakeNetworkStatusProvider
            return FakeNetworkStatusProvider()

    @singleton
    @provider
    def provide_battery_status_service(self, e: Environment) -> BatteryStatusProvider:
        if e.is_raspberry_pi:
            from data.ADS1115BatteryStatusProvider import ADS1115BatteryStatusProvider
            return ADS1115BatteryStatusProvider(e.adc_config.port, e.adc_config.address)
        else:
            from data.FakeBatteryStatusProvider import FakeBatteryStatusProvider
            return FakeBatteryStatusProvider()

    @singleton
    @provider
    def provide_draw_callback(self, state: AppState, display: Display) -> Callable[[bool], None]:
        return lambda partial=False: state.update_display(display, partial)

    @singleton
    @provider
    def provide_display(self, e: Environment, state: AppState) -> Display:
        if e.is_raspberry_pi:
            from interaction.ILI9486Display import ILI9486Display

            spi_device_config = e.display_config.display_device
            return ILI9486Display(
                (spi_device_config.bus, spi_device_config.device),
                e.display_config.dc_pin,
                e.display_config.rst_pin,
                e.display_config.flip_display,
            )
        else:
            if self.__unified_instance is None:
                self.__unified_instance = self.__create_tk_interaction(state, e.app_config)
            return self.__unified_instance

    @singleton
    @provider
    def provide_input(self, e: Environment, state: AppState, display: Display) -> Input:
        if e.is_raspberry_pi:
            class RotaryOnlyInput(Input):
                def close(self) -> None:
                    return

            return RotaryOnlyInput(
                lambda: state.on_key_left(display),
                lambda: state.on_key_right(display),
                lambda: state.on_key_up(display),
                lambda: state.on_key_down(display),
                lambda: state.on_key_a(display),
                lambda: state.on_key_b(display),
                lambda: state.on_rotary_increase(display),
                lambda: state.on_rotary_decrease(display),
                lambda: None,
            )
        else:
            if self.__unified_instance is None:
                self.__unified_instance = self.__create_tk_interaction(state, e.app_config)
            return self.__unified_instance


def start_mode_selector_thread(app_state: AppState, display: Display):
    try:
        import RPi.GPIO as GPIO
    except Exception as ex:
        logger.warning("Mode selector thread not started (RPi.GPIO unavailable): %s", ex)
        return

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    mode_pins = {
        5: 0,    # HOME
        6: 1,    # UPDT
        12: 2,   # NOTIF
        13: 3,   # RAD
        20: 4,   # MAP
        26: 5,   # PWR
        23: 6,   # STAT
    }

    for pin in mode_pins:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def read_active_index():
        low_pins = [pin for pin in mode_pins if GPIO.input(pin) == GPIO.LOW]
        if len(low_pins) == 1:
            return mode_pins[low_pins[0]], low_pins
        return None, low_pins

    def worker():
        last_index = None
        candidate = None
        stable_count = 0
        last_open_logged = False

        while True:
            idx, low_pins = read_active_index()

            if idx == candidate:
                stable_count += 1
            else:
                candidate = idx
                stable_count = 1

            if stable_count >= 3:
                if idx is not None and idx != last_index:
                    try:
                        app_state.set_active_app_index(idx, display)
                        last_index = idx
                        last_open_logged = False
                    except Exception:
                        logger.exception("Failed to set app index from mode selector")
                elif idx is None and low_pins == [] and not last_open_logged:
                    logger.info("Selector in open position (no app selected); ignoring")
                    last_open_logged = True

            time.sleep(0.02)

    threading.Thread(target=worker, daemon=True).start()
    logger.info("Started mode selector thread")


def start_rotary_encoder_thread(app_state: AppState, display: Display):
    try:
        import RPi.GPIO as GPIO
    except Exception as ex:
        logger.warning("Rotary thread not started (RPi.GPIO unavailable): %s", ex)
        return

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    for pin in (ENC_A_PIN, ENC_B_PIN, ENC_SW_PIN):
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    last_state = (GPIO.input(ENC_A_PIN) << 1) | GPIO.input(ENC_B_PIN)
    last_step_t = 0.0

    btn_last = GPIO.input(ENC_SW_PIN)
    btn_press_t = None
    long_fired = False

    trans = {
        0b0001: +1,
        0b0010: -1,
        0b0100: -1,
        0b0111: +1,
        0b1000: +1,
        0b1011: -1,
        0b1101: -1,
        0b1110: +1,
    }

    def worker():
        nonlocal last_state, last_step_t, btn_last, btn_press_t, long_fired

        while True:
            a = GPIO.input(ENC_A_PIN)
            b = GPIO.input(ENC_B_PIN)
            state = (a << 1) | b

            if state != last_state:
                key = (last_state << 2) | state
                direction = trans.get(key, 0)

                now = time.monotonic()
                if direction != 0 and (now - last_step_t) >= ENC_STEP_RATE_LIMIT_S:
                    last_step_t = now
                    try:
                        active_app = app_state.active_app

                        if isinstance(active_app, RadioApp):
                            if getattr(active_app, "is_control_mode", False):
                                if direction > 0:
                                    app_state.on_key_right(display)
                                else:
                                    app_state.on_key_left(display)
                            else:
                                if direction > 0:
                                    app_state.on_key_down(display)
                                else:
                                    app_state.on_key_up(display)
                        else:
                            if direction > 0:
                                app_state.on_key_down(display)
                            else:
                                app_state.on_key_up(display)
                    except Exception:
                        logger.exception("Rotary turn handler failed")

                last_state = state

            btn = GPIO.input(ENC_SW_PIN)
            now = time.monotonic()

            if btn != btn_last:
                btn_last = btn
                if btn == 0:
                    btn_press_t = now
                    long_fired = False
                else:
                    if btn_press_t is not None:
                        held = now - btn_press_t
                        btn_press_t = None
                        if held >= BTN_DEBOUNCE_S and not long_fired:
                            try:
                                app_state.on_key_a(display)
                            except Exception:
                                logger.exception("Rotary short-press handler failed")

            if btn_last == 0 and btn_press_t is not None and not long_fired:
                if (now - btn_press_t) >= BTN_LONGPRESS_S:
                    long_fired = True
                    try:
                        app_state.on_key_b(display)
                    except Exception:
                        logger.exception("Rotary long-press handler failed")

            time.sleep(ENC_POLL_S)

    threading.Thread(target=worker, daemon=True).start()
    logger.info("Started rotary encoder thread (A=%s B=%s SW=%s)", ENC_A_PIN, ENC_B_PIN, ENC_SW_PIN)


def draw_footer(image: Image.Image, state: AppState) -> tuple[Image.Image, int, int]:
    width, height = state.environment.app_config.resolution
    footer_height = 20
    footer_bottom_offset = 3
    icon_padding = 3
    footer_side_offset = state.environment.app_config.app_side_offset
    font = state.environment.app_config.font_header
    draw = ImageDraw.Draw(image)

    start = (footer_side_offset, height - footer_height - footer_bottom_offset)
    end = (width - footer_side_offset - 1, height - footer_bottom_offset - 1)
    cursor_x, cursor_y = start

    connection_status_color = {
        ConnectionStatus.CONNECTED: state.environment.app_config.accent,
        ConnectionStatus.DISCONNECTED: state.environment.app_config.accent if state.tick else state.environment.app_config.background,
    }
    device_status_color = {
        DeviceStatus.OPERATIONAL: state.environment.app_config.accent,
        DeviceStatus.NO_DATA: state.environment.app_config.accent if state.tick else state.environment.app_config.background,
        DeviceStatus.UNAVAILABLE: state.environment.app_config.background,
    }

    draw.rectangle(start + end, fill=state.environment.app_config.accent_dark)

    nw_status_padding = (footer_height - resources.network_icon.height) // 2
    nw_status_color = connection_status_color[state.network_status_provider.get_connection_status()]
    draw.bitmap((cursor_x + icon_padding, cursor_y + nw_status_padding), resources.network_icon, fill=nw_status_color)
    cursor_x += resources.network_icon.width + icon_padding

    gps_status_padding = (footer_height - resources.gps_icon.height) // 2
    gps_status_color = device_status_color[state.location_provider.get_device_status()]
    draw.bitmap((cursor_x + icon_padding, cursor_y + gps_status_padding), resources.gps_icon, fill=gps_status_color)
    cursor_x += resources.gps_icon.width + icon_padding

    state_of_charge_str = f"{state.battery_status_provider.get_state_of_charge():.0%}"
    _, _, text_width, text_height = font.getbbox(state_of_charge_str)
    text_padding = (footer_height - text_height) // 2
    draw.text((cursor_x + icon_padding, cursor_y + text_padding), state_of_charge_str, state.environment.app_config.accent, font=font)
    cursor_x += text_width

    date_str = datetime.now(LOCAL_TZ).strftime("%m-%d-%Y %I:%M:%S %p")
    _, _, text_width, text_height = font.getbbox(date_str)
    text_padding = (footer_height - text_height) // 2
    draw.text((width - footer_side_offset - text_padding - text_width, cursor_y + text_padding), date_str, state.environment.app_config.accent, font=font)

    x0, y0 = start
    end = end[0] + 1, end[1] + 1
    return image.crop(start + end), x0, y0


def draw_header(image: Image.Image, state: AppState) -> tuple[Image.Image, int, int]:
    width, height = state.environment.app_config.resolution
    vertical_line = 5
    header_top_offset = state.environment.app_config.app_top_offset - vertical_line
    header_side_offset = state.environment.app_config.app_side_offset
    app_spacing = 20
    app_padding = 5
    draw = ImageDraw.Draw(image)
    color_background = state.environment.app_config.background
    color_accent = state.environment.app_config.accent

    start = (header_side_offset, header_top_offset + vertical_line)
    end = (header_side_offset, header_top_offset)
    draw.line(start + end, fill=color_accent)
    start = end
    end = (width - header_side_offset - 1, header_top_offset)
    draw.line(start + end, fill=color_accent)
    start = end
    end = (width - header_side_offset - 1, header_top_offset + vertical_line)
    draw.line(start + end, fill=color_accent)

    font = state.environment.app_config.font_header
    max_text_width = width - (2 * header_side_offset)
    app_text_width = sum(int(font.getbbox(app.title)[2]) for app in state.apps) + (len(state.apps) - 1) * app_spacing
    cursor = header_side_offset + (max_text_width - app_text_width) // 2

    for app in state.apps:
        _, _, text_width, text_height = map(int, font.getbbox(app.title))
        draw.text((cursor, header_top_offset - text_height - app_padding), app.title, color_accent, font=font)
        if app is state.active_app:
            start = (cursor - app_padding, header_top_offset - vertical_line)
            end = (cursor - app_padding, header_top_offset)
            draw.line(start + end, fill=color_accent)
            start = end
            end = (cursor + text_width + app_padding, header_top_offset)
            draw.line(start + end, fill=color_background)
            start = end
            end = (cursor + text_width + app_padding, header_top_offset - vertical_line)
            draw.line(start + end, fill=color_accent)
        cursor = cursor + text_width + app_spacing

    partial_start = (header_side_offset, 0)
    partial_end = (width - header_side_offset, header_top_offset + vertical_line)
    x0, y0 = partial_start
    return image.crop(partial_start + partial_end), x0, y0


def draw_base(image: Image.Image, state: AppState) -> Generator[tuple[Image.Image, int, int], Any, None]:
    yield draw_header(image, state)
    yield draw_footer(image, state)


if __name__ == "__main__":
    injector = Injector([AppModule()])
    app_state = injector.get(AppState)

    DISPLAY = injector.get(Display)
    INPUT = injector.get(Input)

    status_led = None

    app_state.add_app(injector.get(DashboardApp)) \
        .add_app(injector.get(UpdateApp)) \
        .add_app(NotificationApp(app_state.notification_manager)) \
        .add_app(injector.get(RadioApp)) \
        .add_app(injector.get(MapApp)) \
        .add_app(injector.get(PowerApp)) \
        .add_app(injector.get(StatusApp))

    if injector.get(Environment).is_raspberry_pi:
        from core.udev_service import UDevService

        status_led = StatusLed(pin=STATUS_LED_PIN)
        status_led.start()
        status_led.slow_blink()

        udev_service = UDevService()
        udev_service.start()

        start_rotary_encoder_thread(app_state, DISPLAY)
        start_mode_selector_thread(app_state, DISPLAY)

    DISPLAY.show(app_state.image_buffer, 0, 0)
    app_state.update_display(DISPLAY)
    app_state.active_app.on_app_enter()

    if isinstance(app_state.active_app, NotificationApp):
        app_state.notification_manager.mark_all_read()

    if status_led is not None:
        try:
            status_led.set_mode(app_state.get_status_led_mode())
        except Exception:
            logger.exception("Failed setting initial status LED mode")
            status_led.triple_flash()

    try:
        app_state.watch_function(DISPLAY, status_led=status_led)
    except KeyboardInterrupt:
        pass
    finally:
        if status_led is not None:
            status_led.off()
            status_led.cleanup()
        try:
            DISPLAY.close()
        except Exception:
            logger.exception("Display close failed during shutdown")
        try:
            INPUT.close()
        except Exception:
            logger.exception("Input close failed during shutdown")