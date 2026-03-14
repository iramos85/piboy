import threading
import time

import RPi.GPIO as GPIO


class StatusLed:
    OFF = "off"
    ON = "on"
    SLOW_BLINK = "slow_blink"
    FAST_BLINK = "fast_blink"
    TRIPLE_FLASH = "triple_flash"

    def __init__(self, pin: int = 16):
        self.pin = pin
        self._mode = self.OFF
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self.pin, GPIO.OUT)
        GPIO.output(self.pin, GPIO.LOW)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_mode(self, mode: str):
        with self._lock:
            self._mode = mode

    def off(self):
        self.set_mode(self.OFF)

    def on(self):
        self.set_mode(self.ON)

    def slow_blink(self):
        self.set_mode(self.SLOW_BLINK)

    def fast_blink(self):
        self.set_mode(self.FAST_BLINK)

    def triple_flash(self):
        self.set_mode(self.TRIPLE_FLASH)

    def cleanup(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        GPIO.output(self.pin, GPIO.LOW)
        GPIO.cleanup(self.pin)

    def _run(self):
        while self._running:
            with self._lock:
                mode = self._mode

            if mode == self.OFF:
                GPIO.output(self.pin, GPIO.LOW)
                time.sleep(0.1)

            elif mode == self.ON:
                GPIO.output(self.pin, GPIO.HIGH)
                time.sleep(0.1)

            elif mode == self.SLOW_BLINK:
                GPIO.output(self.pin, GPIO.HIGH)
                time.sleep(0.5)
                GPIO.output(self.pin, GPIO.LOW)
                time.sleep(0.5)

            elif mode == self.FAST_BLINK:
                GPIO.output(self.pin, GPIO.HIGH)
                time.sleep(0.15)
                GPIO.output(self.pin, GPIO.LOW)
                time.sleep(0.15)

            elif mode == self.TRIPLE_FLASH:
                for _ in range(3):
                    GPIO.output(self.pin, GPIO.HIGH)
                    time.sleep(0.1)
                    GPIO.output(self.pin, GPIO.LOW)
                    time.sleep(0.1)
                time.sleep(0.7)

            else:
                GPIO.output(self.pin, GPIO.LOW)
                time.sleep(0.1)