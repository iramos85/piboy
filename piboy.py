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
from app.ClockApp import ClockApp
from app.DebugApp import DebugApp
from app.EnvironmentApp import EnvironmentApp
from app.FileManagerApp import FileManagerApp
from app.MapApp import MapApp
from app.RadioApp import RadioApp
from app.UpdateApp import UpdateApp
from core import resources
from core.data import ConnectionStatus, DeviceStatus
from data.BatteryStatusProvider import BatteryStatusProvider
from data.EnvironmentDataProvider import EnvironmentDataProvider
from data.LocationProvider import LocationProvider
from data.NetworkStatusProvider import NetworkStatusProvider
from data.OSMTileProvider import OSMTileProvider
from data.TileProvider import TileProvider
from environment import AppConfig, Environment
from interaction.Display import Display
from interaction.Input import Input
from interaction.UnifiedInteraction import UnifiedInteraction

fileConfig(fname="config.ini")
logger = logging.getLogger(__name__)

# Force displayed/footer clock to Central Time
LOCAL_TZ = ZoneInfo("America/Chicago")

# UI sound settings
TAB_SWITCH_SFX = "media/ui/tab_switch.wav"

# -----------------------------------------
# Rotary encoder pins (Adafruit #377)
# Verified working in your test script:
#   A -> GPIO17
#   B -> GPIO27
#   SW -> GPIO22
#   Common -> GND
# -----------------------------------------
ENC_A_PIN = 17
ENC_B_PIN = 27
ENC_SW_PIN = 22

# Encoder behavior tuning
ENC_POLL_S = 0.002          # 2ms polling; stable and responsive
ENC_STEP_RATE_LIMIT_S = 0.030  # suppress bounce bursts
BTN_DEBOUNCE_S = 0.060
BTN_LONGPRESS_S = 0.70


def play_tab_switch_sfx():
    """
    Play a short UI sound without blocking the app.
    Tries MAX98357A first, then falls back to default ALSA output.
    """
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
        self.__image_buffer = self.__init_buffer()
        self.__apps: list[App] = []
        self.__active_app = 0

    def __init_buffer(self) -> Image.Image:
        return Image.new("RGB", self.__environment.app_config.resolution, self.__environment.app_config.background)

    def __tick(self):
        self.__bit ^= 1

    def clear_buffer(self) -> Image.Image:
        self.__image_buffer = self.__init_buffer()
        return self.__image_buffer

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
        """Directly select an app by index (used by the rotary selector switch)."""
        if index < 0 or index >= len(self.__apps):
            return
        if index == self.__active_app:
            return

        self.active_app.on_app_leave()
        self.__active_app = index
        self.active_app.on_app_enter()
        play_tab_switch_sfx()
        self.update_display(display, partial=False)

    def watch_function(self, display: Display):
        while True:
            now = datetime.now(LOCAL_TZ)
            time.sleep(1.0 - now.microsecond / 1_000_000.0)

            image, x0, y0 = draw_footer(self.image_buffer, self)
            display.show(image, x0, y0)
            self.__tick()

    def update_display(self, display: Display, partial=False):
        image = self.clear_buffer()
        app_bbox = (
            self.__environment.app_config.app_side_offset,
            self.__environment.app_config.app_top_offset,
            self.__environment.app_config.width - self.__environment.app_config.app_side_offset,
            self.__environment.app_config.height - self.__environment.app_config.app_bottom_offset,
        )
        x_offset, y_offset = app_bbox[0:2]
        if partial:
            for patch, x0, y0 in self.active_app.draw(image.crop(app_bbox), partial):
                display.show(patch, x0 + x_offset, y0 + y_offset)
        else:
            for patch, x0, y0 in draw_base(image, self):
                display.show(patch, x0, y0)
            for patch, x0, y0 in self.active_app.draw(image.crop(app_bbox), partial):
                image.paste(patch, (x0 + x_offset, y0 + y_offset))
            display.show(image.crop(app_bbox), x_offset, y_offset)

    # “Button” handlers (still used by rotary mapping)
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

    # Kept for compatibility if anything still calls these
    def on_rotary_increase(self, display: Display):
        self.active_app.on_app_leave()
        self.next_app()
        self.active_app.on_app_enter()
        play_tab_switch_sfx()
        self.update_display(display, partial=False)

    def on_rotary_decrease(self, display: Display):
        self.active_app.on_app_leave()
        self.previous_app()
        self.active_app.on_app_enter()
        play_tab_switch_sfx()
        self.update_display(display, partial=False)


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
        return lambda partial: state.update_display(display, partial)

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
        """
        IMPORTANT: We intentionally do NOT use GPIOInput anymore.
        All navigation is driven by the rotary encoder thread.
        """
        if e.is_raspberry_pi:
            class _DummyInput(Input):
                def close(self):
                    return

            return _DummyInput()
        else:
            if self.__unified_instance is None:
                self.__unified_instance = self.__create_tk_interaction(state, e.app_config)
            return self.__unified_instance


def start_mode_selector_thread(app_state: AppState, display: Display):
    """
    Poll a multi-position selector wired as:
      - common -> GND
      - each selected throw -> one GPIO pin (input pull-up)
    Active position reads LOW.

    Pin mapping (BCM) to app index:
      Position 1 -> INV (0)
      Position 2 -> SYS (1)
      Position 3 -> ENV (2)
      Position 4 -> RAD (3)
      Position 5 -> MAP (6)
    """
    try:
        import RPi.GPIO as GPIO
    except Exception as ex:
        logger.warning("Mode selector thread not started (RPi.GPIO unavailable): %s", ex)
        return

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    mode_pins = {
        5: 0,   # INV
        6: 1,   # SYS
        12: 2,  # ENV
        13: 3,  # RAD
        20: 6,  # MAP (moved from GPIO19 to GPIO20)
    }

    for pin in mode_pins:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def read_active_index():
        low_pins = [pin for pin in mode_pins if GPIO.input(pin) == GPIO.LOW]
        if len(low_pins) == 1:
            return mode_pins[low_pins[0]]
        return None

    def worker():
        last_index = None
        candidate = None
        stable_count = 0

        while True:
            idx = read_active_index()

            if idx == candidate:
                stable_count += 1
            else:
                candidate = idx
                stable_count = 1

            if stable_count >= 3 and idx is not None and idx != last_index:
                try:
                    app_state.set_active_app_index(idx, display)
                    last_index = idx
                except Exception:
                    logger.exception("Failed to set app index from mode selector")

            time.sleep(0.02)

    threading.Thread(target=worker, daemon=True).start()
    logger.info("Started mode selector thread")


def start_rotary_encoder_thread(app_state: AppState, display: Display):
    """
    Polling rotary encoder driver (no GPIO edge interrupts).
    This avoids 'Failed to add edge detection' entirely.

    Mappings:
      CW  -> on_key_down
      CCW -> on_key_up
      short press -> on_key_a
      long press  -> on_key_b
    """
    try:
        import RPi.GPIO as GPIO
    except Exception as ex:
        logger.warning("Rotary thread not started (RPi.GPIO unavailable): %s", ex)
        return

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    for pin in (ENC_A_PIN, ENC_B_PIN, ENC_SW_PIN):
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # Quadrature decode via state transitions
    last_state = (GPIO.input(ENC_A_PIN) << 1) | GPIO.input(ENC_B_PIN)
    last_step_t = 0.0

    # Button state
    btn_last = GPIO.input(ENC_SW_PIN)
    btn_last_change_t = time.monotonic()
    btn_press_t = None  # type: float | None
    long_fired = False

    # Transition table: maps (prev<<2 | curr) to direction
    # +1 = CW, -1 = CCW, 0 = invalid/no move
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
        nonlocal last_state, last_step_t, btn_last, btn_last_change_t, btn_press_t, long_fired

        while True:
            # --- encoder ---
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
                        if direction > 0:
                            app_state.on_key_down(display)
                        else:
                            app_state.on_key_up(display)
                    except Exception:
                        logger.exception("Rotary turn handler failed")

                last_state = state

            # --- button ---
            btn = GPIO.input(ENC_SW_PIN)
            now = time.monotonic()

            if btn != btn_last:
                btn_last = btn
                btn_last_change_t = now

                # pressed (pulled up -> LOW)
                if btn == 0:
                    btn_press_t = now
                    long_fired = False
                else:
                    # released
                    if btn_press_t is not None:
                        held = now - btn_press_t
                        btn_press_t = None
                        if held >= BTN_DEBOUNCE_S and not long_fired:
                            # short press
                            try:
                                app_state.on_key_a(display)
                            except Exception:
                                logger.exception("Rotary short-press handler failed")

            # long press detection while held
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

    app_state.add_app(injector.get(FileManagerApp)) \
        .add_app(injector.get(UpdateApp)) \
        .add_app(injector.get(EnvironmentApp)) \
        .add_app(injector.get(RadioApp)) \
        .add_app(injector.get(DebugApp)) \
        .add_app(injector.get(ClockApp)) \
        .add_app(injector.get(MapApp))

    if injector.get(Environment).is_raspberry_pi:
        from core.udev_service import UDevService

        udev_service = UDevService()
        udev_service.start()

        # Start inputs
        start_rotary_encoder_thread(app_state, DISPLAY)
        start_mode_selector_thread(app_state, DISPLAY)

    DISPLAY.show(app_state.image_buffer, 0, 0)
    app_state.update_display(DISPLAY)
    app_state.active_app.on_app_enter()

    try:
        app_state.watch_function(DISPLAY)
    except KeyboardInterrupt:
        pass
    finally:
        DISPLAY.close()
        INPUT.close()