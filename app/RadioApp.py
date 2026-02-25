import os
import random
import re
import threading
import time
import wave
from abc import ABC, abstractmethod
from subprocess import PIPE, run
from typing import Any, Callable, Generator, Optional

import pyaudio
from injector import inject
from PIL import Image, ImageDraw

from app.App import SelfUpdatingApp
from core import resources
from core.decorator import override
from environment import AppConfig


class RadioApp(SelfUpdatingApp):
    __CONTROL_PADDING = 4
    __CONTROL_BOTTOM_OFFSET = 20
    __LINE_HEIGHT = 20
    __META_INFO_HEIGHT = 20
    __VOLUME_STEP = 10

    class ControlGroup:
        def __init__(self):
            self.__controls: list['RadioApp.Control'] = []

        def listen(self, control: 'RadioApp.Control'):
            self.__controls.append(control)

        def clear_selection(self, control: Optional['RadioApp.Control']):
            for control in [c for c in self.__controls if c is not control]:
                control.reset()

    class Control(ABC):
        class SelectionState:
            NONE: 'RadioApp.Control.SelectionState'
            FOCUSED: 'RadioApp.Control.SelectionState'

            def __init__(self, color: tuple[int, int, int], background_color: tuple[int, int, int],
                         is_focused: bool, is_selected: bool):
                self.__color = color
                self.__background_color = background_color
                self.__is_focused = is_focused
                self.__is_selected = is_selected

            @classmethod
            def from_state(cls, is_focused: bool, is_selected: bool) -> 'RadioApp.Control.SelectionState':
                values = [cls.NONE, cls.FOCUSED]
                return [state for state in values
                        if state.is_focused == is_focused and state.is_selected == is_selected][0]

            @property
            def is_focused(self) -> bool:
                return self.__is_focused

            @property
            def is_selected(self) -> bool:
                return self.__is_selected

            @property
            def color(self) -> tuple[int, int, int]:
                return self.__color

            @property
            def background_color(self) -> tuple[int, int, int]:
                return self.__background_color

        def __init__(self, icon_bitmap: Image.Image, control_group: Optional['RadioApp.ControlGroup'] = None):
            self._icon_bitmap = icon_bitmap
            self._selection_state = self.SelectionState.NONE
            self._control_group = control_group
            if self._control_group:
                self._control_group.listen(self)

        @property
        def size(self) -> tuple[int, int]:
            return self._icon_bitmap.size

        @property
        def is_focused(self) -> bool:
            return self._selection_state.is_focused

        @property
        def is_selected(self) -> bool:
            return self._selection_state.is_selected

        def _handle_control_group(self):
            if not self.is_selected and self._control_group:
                self._control_group.clear_selection(self)

        @abstractmethod
        def on_select(self):
            raise NotImplementedError

        def on_focus(self):
            self._selection_state = self.SelectionState.FOCUSED

        def on_blur(self):
            self._selection_state = self.SelectionState.NONE

        def reset(self):
            self._selection_state = self.SelectionState.NONE

        def draw(self, draw: ImageDraw.ImageDraw, left_top: tuple[int, int]):
            width, height = self._icon_bitmap.size
            left, top = left_top
            draw.rectangle(left_top + (left + width - 1, top + height - 1),
                           fill=self._selection_state.background_color)
            draw.bitmap(left_top, self._icon_bitmap, fill=self._selection_state.color)

    class SwitchControl(Control):
        def __init__(self, icon_bitmap: Image.Image, switched_icon_bitmap: Image.Image,
                     on_select: Callable[[], bool],
                     on_switched_select: Callable[[], bool],
                     control_group: Optional['RadioApp.ControlGroup'] = None):
            super().__init__(icon_bitmap, control_group)
            self._on_select = on_select
            self._on_switched_select = on_switched_select
            self._switched_icon_bitmap = switched_icon_bitmap
            self._is_switched = False

        def on_select(self):
            super()._handle_control_group()
            if self._is_switched:
                if self._on_select():
                    self._is_switched = not self._is_switched
            else:
                if self._on_switched_select():
                    self._is_switched = not self._is_switched

        def on_deselect(self):
            self._is_switched = False

        def reset(self):
            self.on_blur()
            self.on_deselect()

        def draw(self, draw: ImageDraw.ImageDraw, left_top: tuple[int, int]):
            width, height = self._icon_bitmap.size
            left, top = left_top
            draw.rectangle(left_top + (left + width - 1, top + height - 1),
                           fill=self._selection_state.background_color)
            draw.bitmap(left_top, self._switched_icon_bitmap if self._is_switched else self._icon_bitmap,
                        fill=self._selection_state.color)

    class InstantControl(Control):
        def __init__(self, icon_bitmap: Image.Image, on_select: Callable[[], None],
                     control_group: Optional['RadioApp.ControlGroup'] = None):
            super().__init__(icon_bitmap, control_group)
            self._on_select = on_select

        def on_select(self):
            super()._handle_control_group()
            self._on_select()

    class AudioPlayer:
        def __init__(self, callback_next: Callable[[], None]):
            self.__player = pyaudio.PyAudio()
            self.__total_frames = 0
            self.__played_frames = 0
            self.__wave_read: Optional[wave.Wave_read] = None
            self.__stream: Optional[pyaudio.Stream] = None
            self.__callback_next = callback_next
            self.__is_continuing = False
            self.__output_device_index = self.__find_output_device_index("MAX98357A")

        def __find_output_device_index(self, preferred_name: str) -> Optional[int]:
            preferred_name = preferred_name.lower()
            try:
                for i in range(self.__player.get_device_count()):
                    info = self.__player.get_device_info_by_index(i)
                    name = str(info.get('name', '')).lower()
                    max_out = int(info.get('maxOutputChannels', 0))
                    if max_out > 0 and preferred_name in name:
                        return i
            except Exception:
                pass
            return None

        def __stream_callback(self, _1, frame_count, _2, _3) -> tuple[bytes, int]:
            data = self.__wave_read.readframes(frame_count)
            self.__played_frames += frame_count
            if self.__played_frames >= self.__total_frames:
                thread_call_next = threading.Thread(target=self.__delayed_call_next, args=(), daemon=True)
                thread_call_next.start()
                return bytes(), pyaudio.paComplete
            else:
                return data, pyaudio.paContinue

        def __delayed_call_next(self, delay: int = 1):
            time.sleep(delay)
            self.__callback_next()

        def load_file(self, file_path: str):
            self.__wave_read = wave.open(file_path, 'rb')
            self.__total_frames = self.__wave_read.getnframes()
            self.__played_frames = 0

            open_kwargs = dict(
                format=self.__player.get_format_from_width(self.__wave_read.getsampwidth()),
                channels=self.__wave_read.getnchannels(),
                rate=self.__wave_read.getframerate(),
                output=True,
                stream_callback=self.__stream_callback
            )
            if self.__output_device_index is not None:
                open_kwargs["output_device_index"] = self.__output_device_index

            self.__stream = self.__player.open(**open_kwargs)

        def start_stream(self) -> bool:
            if self.__stream:
                self.__stream.start_stream()
                self.__is_continuing = True
                return True
            return False

        def pause_stream(self) -> bool:
            if self.__stream:
                self.__stream.stop_stream()
                self.__is_continuing = False
                return True
            return False

        def stop_stream(self) -> bool:
            if self.__stream:
                self.__stream.stop_stream()
                self.__stream.close()
                self.__stream = None
                self.__is_continuing = False
                self.__total_frames = 0
                self.__played_frames = 0
                return True
            return False

        @property
        def has_stream(self) -> bool:
            return self.__stream is not None

        @property
        def is_active(self) -> bool:
            return self.__stream is not None and self.__stream.is_active()

        @property
        def is_continuing(self) -> bool:
            return self.__is_continuing

        @property
        def progress(self) -> Optional[float]:
            if self.__total_frames == 0:
                return None
            if self.__total_frames <= self.__played_frames:
                return 1
            return self.__played_frames / self.__total_frames

    __playback_control_group = ControlGroup()

    @inject
    def __init__(self, draw_callback: Callable[[bool], None], app_config: AppConfig):
        super().__init__(self.__self_update)
        self.__draw_callback = draw_callback
        self.__draw_callback_kwargs = {'partial': True}

        self.__app_size = app_config.app_size
        self.__background = app_config.background
        self.__color = app_config.accent
        self.__color_dark = app_config.accent_dark
        self.__font = app_config.font_standard

        self.__directory = 'media'
        self.__supported_extensions = ['.wav']
        self.__files: list[str] = self.__get_files()

        self.__selected_index = 0
        self.__top_index = 0
        self.__playlist: list[int] = list(range(0, len(self.__files)))
        self.__playing_index = 0
        self.__is_random = False
        self.__volume: Optional[int] = None
        try:
            self.__volume = self.__get_volume()
        except (FileNotFoundError, ValueError):
            self.__volume = None

        self.__player = self.AudioPlayer(self.__call_next)

        self.Control.SelectionState.NONE = self.Control.SelectionState(self.__color_dark, self.__background, False, False)
        self.Control.SelectionState.FOCUSED = self.Control.SelectionState(self.__color, self.__background, True, False)

        control_group = self.ControlGroup()
        self.__controls = [
            self.InstantControl(resources.stop_icon, self.stop_action, control_group),
            self.InstantControl(resources.previous_icon, self.prev_action),
            self.SwitchControl(resources.play_icon, resources.pause_icon, self.pause_action, self.play_action, control_group),
            self.InstantControl(resources.skip_icon, self.skip_action),
            self.SwitchControl(resources.order_icon, resources.random_icon, self.order_action, self.random_action),
            self.InstantControl(resources.volume_decrease_icon, self.decrease_volume_action),
            self.InstantControl(resources.volume_increase_icon, self.increase_volume_action)
        ]
        self.__selected_control_index = 2

    def play_action(self) -> bool:
        if len(self.__playlist) == 0:
            return False
        if self.__selected_index != self.__playlist[self.__playing_index]:
            self.__playing_index = self.__playlist.index(self.__selected_index)
            if self.__player.has_stream:
                self.__player.stop_stream()
        if not self.__player.has_stream:
            self.__player.load_file(os.path.join(self.__directory,
                                                 self.__files[self.__playlist[self.__playing_index]]))
        self.__player.start_stream()
        return True

    def pause_action(self) -> bool:
        return self.__player.pause_stream()

    def stop_action(self):
        self.__player.stop_stream()

    def prev_action(self):
        if len(self.__files) == 0:
            return
        self.__playing_index = (self.__playing_index - 1) % len(self.__files)
        self.__selected_index = self.__playlist[self.__playing_index]

        if self.__player.is_active:
            self.stop_action()
            self.play_action()

    def skip_action(self):
        if len(self.__files) == 0:
            return
        self.__playing_index = (self.__playing_index + 1) % len(self.__files)
        self.__selected_index = self.__playlist[self.__playing_index]

        if self.__player.is_active:
            self.stop_action()
            self.play_action()

    def random_action(self) -> bool:
        self.__is_random = True
        random.shuffle(self.__playlist)
        self.__playing_index = self.__playlist.index(self.__selected_index)
        return True

    def order_action(self) -> bool:
        self.__is_random = False
        self.__playlist = list(range(0, len(self.__files)))
        self.__playing_index = self.__selected_index
        return True

    def decrease_volume_action(self):
        try:
            current_value = self.__get_volume()
            if current_value % self.__VOLUME_STEP == 0:
                self.__set_volume(max(current_value - self.__VOLUME_STEP, 0))
            else:
                aligned_value = current_value // self.__VOLUME_STEP * self.__VOLUME_STEP
                self.__set_volume(max(aligned_value - self.__VOLUME_STEP, 0))
            self.__volume = self.__get_volume()
        except ValueError:
            pass

    def increase_volume_action(self):
        try:
            current_value = self.__get_volume()
            if current_value % self.__VOLUME_STEP == 0:
                self.__set_volume(min(current_value + self.__VOLUME_STEP, 100))
            else:
                aligned_value = (current_value + self.__VOLUME_STEP) // self.__VOLUME_STEP * self.__VOLUME_STEP
                self.__set_volume(min(aligned_value + self.__VOLUME_STEP, 100))
            self.__volume = self.__get_volume()
        except ValueError:
            pass

    def __call_next(self):
        if self.__player.is_continuing and len(self.__files) > 0:
            self.__playing_index = (self.__playing_index + 1) % len(self.__files)
            self.__selected_index = self.__playlist[self.__playing_index]
            self.__player.stop_stream()
            self.__player.load_file(os.path.join(self.__directory, self.__files[self.__playlist[self.__playing_index]]))
            self.__player.start_stream()

    def __self_update(self):
        self.__draw_callback(**self.__draw_callback_kwargs)

    @property
    @override
    def title(self) -> str:
        return 'RAD'

    @property
    @override
    def refresh_time(self) -> float:
        return 1.0

    @override
    def draw(self, image: Image.Image, partial=False) -> Generator[tuple[Image.Image, int, int], Any, None]:
        draw = ImageDraw.Draw(image)
        width, height = self.__app_size

        controls_total_width = sum([c.size[0] for c in self.__controls]) + self.__CONTROL_PADDING * (len(self.__controls) - 1)
        max_control_height = max([c.size[1] for c in self.__controls])
        cursor: tuple[int, int] = (width // 2 - controls_total_width // 2,
                                   height - max_control_height - self.__CONTROL_BOTTOM_OFFSET)
        for control in self.__controls:
            c_width, c_height = control.size
            control.draw(draw, (cursor[0], cursor[1] + (max_control_height - c_height) // 2))
            cursor = (cursor[0] + c_width + self.__CONTROL_PADDING, cursor[1])
        vertical_limit = cursor[1]

        vol_display = f'{self.__volume}%' if self.__volume is not None else 'N/A'
        text = f'Volume: {vol_display}'
        _, _, t_width, t_height = self.__font.getbbox(text)
        draw.text((width // 2 - t_width // 2, vertical_limit - self.__META_INFO_HEIGHT // 2 - t_height // 2),
                  text, self.__color, font=self.__font)
        vertical_limit = vertical_limit - self.__META_INFO_HEIGHT

        if self.__player.has_stream and len(self.__playlist) > 0:
            progress = self.__player.progress if self.__player.progress is not None else 0.0
            text = f'{progress:.1%}: {self.__files[self.__playlist[self.__playing_index]]}'
        else:
            text = 'Empty'
        while self.__font.getbbox(text)[2] > width and len(text) > 0:
            text = text[:-1]
        _, _, t_width, t_height = self.__font.getbbox(text)
        draw.text((width // 2 - t_width // 2, vertical_limit - self.__META_INFO_HEIGHT // 2 - t_height // 2),
                  text, self.__color, font=self.__font)
        vertical_limit = vertical_limit - self.__META_INFO_HEIGHT

        left_top = (0, 0)
        left, top = left_top
        right_bottom = (width, vertical_limit)
        right, bottom = right_bottom
        max_entries = (bottom - top) // self.__LINE_HEIGHT
        if len(self.__files) > max_entries:
            if self.__selected_index < self.__top_index:
                self.__top_index = self.__selected_index
            elif self.__selected_index not in range(self.__top_index, self.__top_index + max_entries):
                self.__top_index = self.__selected_index - max_entries + 1
        else:
            self.__top_index = 0

        cursor = left_top
        for index, file in enumerate(self.__files[self.__top_index:]):
            index += self.__top_index
            if self.__selected_index == index:
                draw.rectangle(cursor + (right, cursor[1] + self.__LINE_HEIGHT), self.__color_dark)
            if index == max_entries + self.__top_index:
                draw.text(cursor, '...', self.__color, font=self.__font)
                break
            text = file
            while self.__font.getbbox(text)[2] > right - left and len(text) > 0:
                text = text[:-1]
            draw.text(cursor, text, self.__color, font=self.__font)
            cursor = (cursor[0], cursor[1] + self.__LINE_HEIGHT)

        if partial:
            right_bottom = width, height
            yield image.crop(left_top + right_bottom), *left_top
        else:
            yield image, 0, 0

    def __get_files(self) -> list[str]:
        if not os.path.isdir(self.__directory):
            return []
        return sorted([f for f in os.listdir(self.__directory) if os.path.splitext(f)[1].lower() in
                       self.__supported_extensions], key=lambda f: f.lower())

    @staticmethod
    def __run_amixer_get() -> str:
        candidates = [
            ['amixer', '-c', 'MAX98357A', '-M', 'sget', 'PCM'],
            ['amixer', '-c', 'MAX98357A', '-M', 'sget', 'Digital'],
            ['amixer', '-c', 'MAX98357A', '-M', 'sget', 'Master'],
            ['amixer', '-M', 'sget', 'PCM'],
            ['amixer', '-M', 'sget', 'Digital'],
            ['amixer', '-M', 'sget', 'Master'],
        ]
        for cmd in candidates:
            result = run(cmd, stdout=PIPE, stderr=PIPE)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode('utf-8')
        raise ValueError('Error getting current volume: no supported amixer control found')

    @staticmethod
    def __get_volume() -> int:
        content = RadioApp.__run_amixer_get()
        match = re.search(r'\[(\d+)%\]', content)
        if match:
            return int(match.group(1))
        raise ValueError('Error getting current volume: No match')

    @staticmethod
    def __set_volume(volume: int):
        if not 0 <= volume <= 100:
            raise ValueError(f'Error setting volume value: Volume must be between 0 and 100, was {volume}')

        candidates = [
            ['amixer', '-c', 'MAX98357A', '-q', '-M', 'sset', 'PCM', f'{volume}%'],
            ['amixer', '-c', 'MAX98357A', '-q', '-M', 'sset', 'Digital', f'{volume}%'],
            ['amixer', '-c', 'MAX98357A', '-q', '-M', 'sset', 'Master', f'{volume}%'],
            ['amixer', '-q', '-M', 'sset', 'PCM', f'{volume}%'],
            ['amixer', '-q', '-M', 'sset', 'Digital', f'{volume}%'],
            ['amixer', '-q', '-M', 'sset', 'Master', f'{volume}%'],
        ]
        for cmd in candidates:
            result = run(cmd, stdout=PIPE, stderr=PIPE)
            if result.returncode == 0:
                return
        raise ValueError('Error setting volume value: no supported amixer control found')

    @override
    def on_key_left(self):
        self.__controls[self.__selected_control_index].on_blur()
        self.__selected_control_index = max(self.__selected_control_index - 1, 0)
        self.__controls[self.__selected_control_index].on_focus()

    @override
    def on_key_right(self):
        self.__controls[self.__selected_control_index].on_blur()
        self.__selected_control_index = min(self.__selected_control_index + 1, len(self.__controls) - 1)
        self.__controls[self.__selected_control_index].on_focus()

    @override
    def on_key_up(self):
        self.__selected_index = max(self.__selected_index - 1, 0)

    @override
    def on_key_down(self):
        self.__selected_index = min(self.__selected_index + 1, len(self.__files) - 1)

    @override
    def on_key_a(self):
        self.__controls[self.__selected_control_index].on_select()

    @override
    def on_app_enter(self):
        super().on_app_enter()
        self.__controls[self.__selected_control_index].on_focus()

        # Auto-play when entering RAD (useful if rotary encoder isn't wired yet)
        if len(self.__files) > 0:
            try:
                if not self.__player.has_stream or not self.__player.is_active:
                    self.play_action()
            except Exception:
                pass

    @override
    def on_app_leave(self):
        # Stop playback when leaving RAD so it doesn't continue across tabs
        try:
            self.stop_action()
        except Exception:
            pass
        super().on_app_leave()