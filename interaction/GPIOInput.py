import threading
import time
from typing import Callable

import evdev
import RPi.GPIO as GPIO

from core.decorator import override
from interaction.Input import Input


class GPIOInput(Input):

    def __init__(self, key_left: int, key_right: int, key_up: int, key_down: int, key_a: int, key_b: int,
                 rotary_device: str, rotary_switch: int,
                 on_key_left: Callable[[], None], on_key_right: Callable[[], None],
                 on_key_up: Callable[[], None], on_key_down: Callable[[], None],
                 on_key_a: Callable[[], None], on_key_b: Callable[[], None],
                 on_rotary_increase: Callable[[], None], on_rotary_decrease: Callable[[], None],
                 on_rotary_switch: Callable[[], None], debounce: int = 50):
        super().__init__(on_key_left, on_key_right, on_key_up, on_key_down, on_key_a, on_key_b,
                         on_rotary_increase, on_rotary_decrease, on_rotary_switch)

        self._encoder = None
        self._encoder_thread = None
        self._gpio_encoder_thread = None
        self._stop_flag = False

        # GPIO quadrature pins (fallback path when evdev rotary device is unavailable)
        # These are the encoder CLK/DT pins you set in config.yaml
        self._rotary_clk_pin = key_a if False else None  # placeholder to keep linter quiet; overwritten below
        self._rotary_dt_pin = key_b if False else None   # placeholder to keep linter quiet; overwritten below
        self._rotary_sw_pin = rotary_switch
        self._last_clk = None
        self._last_rot_event_ts = 0.0

        # We don't receive clk/dt as explicit params in this class signature, because the original project
        # uses evdev for rotation and only passes the rotary switch pin here.
        # For GPIO fallback, the project stores clk/dt in the same pins as "A/B" only in our customized flow.
        # In your patched piboy.py, keypad pins are disabled (-1), and encoder push is mapped to "A".
        # We still need a reliable way to get CLK/DT, so we'll infer them from environment by hardcoding your config values.
        #
        # Your chosen encoder pins:
        self._rotary_clk_pin = 16
        self._rotary_dt_pin = 26

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        # Try legacy evdev rotary first (works if /dev/input/event0 exists)
        try:
            self._encoder = evdev.InputDevice(rotary_device)
        except Exception as ex:
            self._encoder = None
            print(f"ROTARY DEVICE NOT FOUND ({rotary_device}): {ex}")

        # --- Key/button inputs (only setup valid pins) ---
        self._setup_optional_input(key_left, self.__gpio_left, debounce)
        self._setup_optional_input(key_right, self.__gpio_right, debounce)
        self._setup_optional_input(key_up, self.__gpio_up, debounce)
        self._setup_optional_input(key_down, self.__gpio_down, debounce)
        self._setup_optional_input(key_a, self.__gpio_a, debounce)
        self._setup_optional_input(key_b, self.__gpio_b, debounce)

        # Encoder push switch (required for your setup)
        if self._is_valid_pin(rotary_switch):
            GPIO.setup(rotary_switch, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(rotary_switch, GPIO.RISING, callback=self.__gpio_rotary_switch, bouncetime=debounce)

        # Start encoder event handling:
        # 1) evdev if available
        # 2) otherwise GPIO polling fallback on CLK/DT
        if self._encoder is not None:
            self._encoder_thread = threading.Thread(target=self.__encoder_loop, daemon=True)
            self._encoder_thread.start()
        else:
            self._setup_gpio_encoder_fallback()
            self._gpio_encoder_thread = threading.Thread(target=self.__gpio_encoder_loop, daemon=True)
            self._gpio_encoder_thread.start()

    def _is_valid_pin(self, pin: int) -> bool:
        return isinstance(pin, int) and pin >= 0

    def _setup_optional_input(self, pin: int, callback, debounce: int):
        """Setup a GPIO input only if the pin is valid (>=0)."""
        if not self._is_valid_pin(pin):
            return
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(pin, GPIO.RISING, callback=callback, bouncetime=debounce)

    def _setup_gpio_encoder_fallback(self):
        """Configure CLK/DT pins for direct GPIO polling fallback."""
        if not self._is_valid_pin(self._rotary_clk_pin) or not self._is_valid_pin(self._rotary_dt_pin):
            print("GPIO encoder fallback disabled: invalid CLK/DT pins")
            return

        GPIO.setup(self._rotary_clk_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self._rotary_dt_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self._last_clk = GPIO.input(self._rotary_clk_pin)

    def __encoder_loop(self):
        # ref: https://github.com/raphaelyancey/pyKY040 (cannot use directly; old GPIO lib)
        try:
            for event in self._encoder.read_loop():
                if self._stop_flag:
                    break
                if event.type == 2:
                    if event.value == -1:
                        self.on_rotary_increase()
                    elif event.value == 1:
                        self.on_rotary_decrease()
        except Exception:
            # If evdev dies mid-run, just stop the thread quietly
            pass

    def __gpio_encoder_loop(self):
        """
        Poll quadrature encoder using CLK/DT pins.
        Debounce is handled by a small time gate.
        """
        if not self._is_valid_pin(self._rotary_clk_pin) or not self._is_valid_pin(self._rotary_dt_pin):
            return

        while not self._stop_flag:
            try:
                clk_state = GPIO.input(self._rotary_clk_pin)

                # Detect edge on CLK
                if self._last_clk is not None and clk_state != self._last_clk:
                    now = time.time()

                    # Basic debounce gate (2 ms)
                    if (now - self._last_rot_event_ts) >= 0.002:
                        dt_state = GPIO.input(self._rotary_dt_pin)

                        # Direction rule for PEC11/KY-040 style encoders
                        if dt_state != clk_state:
                            self.on_rotary_increase()
                        else:
                            self.on_rotary_decrease()

                        self._last_rot_event_ts = now

                self._last_clk = clk_state
                time.sleep(0.001)
            except Exception:
                time.sleep(0.01)

    @override
    def close(self):
        self._stop_flag = True
        try:
            GPIO.cleanup()
        except Exception:
            pass

    def __gpio_left(self, _):
        self.on_key_left()

    def __gpio_right(self, _):
        self.on_key_right()

    def __gpio_up(self, _):
        self.on_key_up()

    def __gpio_down(self, _):
        self.on_key_down()

    def __gpio_a(self, _):
        self.on_key_a()

    def __gpio_b(self, _):
        self.on_key_b()

    def __gpio_rotary_switch(self, _):
        self.on_rotary_switch()