import json
import logging
import os
import random
import re
import socket
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from subprocess import DEVNULL, PIPE, CompletedProcess, Popen, run
from typing import Any, Callable, Generator, Optional

from injector import inject
from PIL import Image, ImageDraw

from app.App import SelfUpdatingApp
from core import resources
from core.decorator import override
from environment import AppConfig

logger = logging.getLogger('app')


class RadioApp(SelfUpdatingApp):
    __CONTROL_PADDING = 4
    __CONTROL_BOTTOM_OFFSET = 20
    __LINE_HEIGHT = 20
    __META_INFO_HEIGHT = 20
    __VOLUME_STEP = 10

    @dataclass
    class Track:
        path: str
        title: str
        artist: str
        album: str

        @property
        def display_name(self) -> str:
            if self.artist and self.title:
                return f'{self.artist} - {self.title}'
            return self.title or os.path.basename(self.path)

    class ControlGroup:
        def __init__(self):
            self.__controls: list['RadioApp.Control'] = []

        def listen(self, control: 'RadioApp.Control'):
            self.__controls.append(control)

        def clear_selection(self, control: Optional['RadioApp.Control']):
            for c in [c for c in self.__controls if c is not control]:
                c.reset()

    class Control(ABC):
        class SelectionState:
            NONE: 'RadioApp.Control.SelectionState'
            FOCUSED: 'RadioApp.Control.SelectionState'

            def __init__(
                self,
                color: tuple[int, int, int],
                background_color: tuple[int, int, int],
                is_focused: bool,
                is_selected: bool
            ):
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
            draw.rectangle(
                left_top + (left + width - 1, top + height - 1),
                fill=self._selection_state.background_color
            )
            draw.bitmap(left_top, self._icon_bitmap, fill=self._selection_state.color)

    class SwitchControl(Control):
        def __init__(
            self,
            icon_bitmap: Image.Image,
            switched_icon_bitmap: Image.Image,
            on_select: Callable[[], bool],
            on_switched_select: Callable[[], bool],
            control_group: Optional['RadioApp.ControlGroup'] = None
        ):
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
            draw.rectangle(
                left_top + (left + width - 1, top + height - 1),
                fill=self._selection_state.background_color
            )
            draw.bitmap(
                left_top,
                self._switched_icon_bitmap if self._is_switched else self._icon_bitmap,
                fill=self._selection_state.color
            )

    class InstantControl(Control):
        def __init__(
            self,
            icon_bitmap: Image.Image,
            on_select: Callable[[], None],
            control_group: Optional['RadioApp.ControlGroup'] = None
        ):
            super().__init__(icon_bitmap, control_group)
            self._on_select = on_select

        def on_select(self):
            super()._handle_control_group()
            self._on_select()

    class AudioPlayer:
        """
        mpv-backed audio player with simple IPC for pause/unpause/quit.
        """

        def __init__(self, callback_next: Callable[[], None]):
            self.__callback_next = callback_next
            self.__process: Optional[Popen] = None
            self.__watch_thread: Optional[threading.Thread] = None
            self.__is_continuing = False
            self.__current_file: Optional[str] = None
            self.__paused = False
            self.__stop_requested = False
            self.__ipc_path = '/tmp/piboy-mpv.sock'

        def __remove_ipc_socket(self):
            try:
                if os.path.exists(self.__ipc_path):
                    os.remove(self.__ipc_path)
            except Exception:
                pass

        def __send_ipc(self, command: dict[str, Any]) -> bool:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(0.5)
                    client.connect(self.__ipc_path)
                    client.sendall((json.dumps(command) + '\n').encode('utf-8'))
                    return True
            except Exception:
                return False

        def __start_watch_thread(self):
            def worker():
                proc = self.__process
                if proc is None:
                    return

                rc = proc.wait()
                if self.__process is not proc:
                    return

                self.__process = None
                self.__paused = False
                self.__remove_ipc_socket()

                if not self.__stop_requested and rc == 0 and self.__is_continuing:
                    t = threading.Thread(target=self.__delayed_call_next, daemon=True)
                    t.start()

            self.__watch_thread = threading.Thread(target=worker, daemon=True)
            self.__watch_thread.start()

        def __delayed_call_next(self, delay: float = 0.2):
            time.sleep(delay)
            self.__callback_next()

        def load_file(self, file_path: str):
            self.__current_file = file_path

        def start_stream(self) -> bool:
            if self.__current_file is None:
                return False

            if self.is_active:
                if self.__paused:
                    ok = self.__send_ipc({"command": ["set_property", "pause", False]})
                    if ok:
                        self.__paused = False
                    return ok
                self.__is_continuing = True
                return True

            self.__remove_ipc_socket()
            self.__stop_requested = False

            cmd = [
                'mpv',
                '--no-video',
                '--really-quiet',
                '--no-terminal',
                f'--input-ipc-server={self.__ipc_path}',
                self.__current_file
            ]

            try:
                self.__process = Popen(cmd, stdout=DEVNULL, stderr=DEVNULL)
                self.__is_continuing = True
                self.__paused = False
                self.__start_watch_thread()
                return True
            except Exception as ex:
                logger.error('Error starting mpv: %s', ex)
                self.__process = None
                self.__paused = False
                self.__remove_ipc_socket()
                return False

        def pause_stream(self) -> bool:
            if not self.is_active:
                return False

            ok = self.__send_ipc({"command": ["cycle", "pause"]})
            if ok:
                self.__paused = not self.__paused
            return ok

        def stop_stream(self) -> bool:
            if self.__process:
                self.__stop_requested = True
                try:
                    self.__send_ipc({"command": ["quit"]})
                    self.__process.wait(timeout=1.5)
                except Exception:
                    try:
                        self.__process.terminate()
                    except Exception:
                        pass
                self.__process = None
                self.__paused = False
                self.__is_continuing = False
                self.__remove_ipc_socket()
                return True

            self.__paused = False
            self.__is_continuing = False
            self.__remove_ipc_socket()
            return False

        @property
        def has_stream(self) -> bool:
            return self.__current_file is not None

        @property
        def is_active(self) -> bool:
            return self.__process is not None and self.__process.poll() is None

        @property
        def is_continuing(self) -> bool:
            return self.__is_continuing

        @property
        def is_paused(self) -> bool:
            return self.__paused

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

        self.__media_root = Path('media')
        self.__music_directory = self.__media_root / 'Music'
        self.__playlists_directory = self.__media_root / 'Playlists'
        self.__default_playlist = self.__playlists_directory / 'all-shuffled.m3u'
        self.__supported_extensions = {'.mp3', '.wav', '.flac', '.ogg', '.m4a'}

        self.__tracks: list[RadioApp.Track] = []
        self.__selected_index = 0
        self.__top_index = 0
        self.__playlist: list[int] = []
        self.__playing_index = 0
        self.__is_random = False
        self.__source_name = 'Empty'
        self.__status_text = 'Ready'

        self.__reload_library()

        self.__volume: Optional[int] = None
        try:
            self.__volume = self.__get_volume()
        except (FileNotFoundError, ValueError):
            self.__volume = None

        self.__player = self.AudioPlayer(self.__call_next)

        self.Control.SelectionState.NONE = self.Control.SelectionState(
            self.__color_dark, self.__background, False, False
        )
        self.Control.SelectionState.FOCUSED = self.Control.SelectionState(
            self.__color, self.__background, True, False
        )

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

    @staticmethod
    def __sanitize_title(file_name: str) -> str:
        title = os.path.splitext(file_name)[0]
        title = re.sub(r'^\d+\s*[-._ ]\s*', '', title)
        return title.strip()

    def __track_from_path(self, file_path: Path) -> Optional['RadioApp.Track']:
        try:
            resolved = file_path.resolve()
        except Exception:
            resolved = file_path

        if not resolved.is_file():
            return None
        if resolved.suffix.lower() not in self.__supported_extensions:
            return None

        artist = 'Unknown Artist'
        album = 'Unknown Album'

        try:
            rel = resolved.relative_to(self.__music_directory.resolve())
            parts = rel.parts
            if len(parts) >= 3:
                artist = parts[0]
                album = parts[1]
        except Exception:
            parent = resolved.parent
            album = parent.name
            artist = parent.parent.name if parent.parent else 'Unknown Artist'

        title = self.__sanitize_title(resolved.name)
        return self.Track(str(resolved), title, artist, album)

    def __load_tracks_from_music(self) -> list['RadioApp.Track']:
        tracks: list[RadioApp.Track] = []
        if not self.__music_directory.is_dir():
            return tracks

        for path in sorted(self.__music_directory.rglob('*')):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self.__supported_extensions:
                continue

            track = self.__track_from_path(path)
            if track is not None:
                tracks.append(track)

        return tracks

    def __load_tracks_from_playlist(self, playlist_path: Path) -> list['RadioApp.Track']:
        tracks: list[RadioApp.Track] = []
        if not playlist_path.is_file():
            return tracks

        try:
            lines = playlist_path.read_text(encoding='utf-8').splitlines()
        except UnicodeDecodeError:
            lines = playlist_path.read_text(encoding='utf-8-sig').splitlines()
        except Exception as ex:
            logger.error('Error reading playlist %s: %s', playlist_path, ex)
            return tracks

        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue

            candidate = Path(line)
            if not candidate.is_absolute():
                candidate = (playlist_path.parent / candidate).resolve()

            track = self.__track_from_path(candidate)
            if track is not None:
                tracks.append(track)

        return tracks

    def __reload_library(self):
        loaded_tracks: list[RadioApp.Track] = []

        if self.__default_playlist.is_file():
            loaded_tracks = self.__load_tracks_from_playlist(self.__default_playlist)
            if loaded_tracks:
                self.__source_name = self.__default_playlist.stem
                self.__status_text = 'Playlist loaded'
            else:
                self.__status_text = 'Playlist empty'
        if not loaded_tracks:
            loaded_tracks = self.__load_tracks_from_music()
            self.__source_name = 'All Music'
            self.__status_text = 'Library loaded' if loaded_tracks else 'Empty'

        self.__tracks = loaded_tracks
        self.__playlist = list(range(len(self.__tracks)))
        self.__selected_index = 0
        self.__top_index = 0
        self.__playing_index = 0
        self.__is_random = False

    def __current_track(self) -> Optional['RadioApp.Track']:
        if not self.__tracks or not self.__playlist:
            return None
        if self.__playing_index < 0 or self.__playing_index >= len(self.__playlist):
            return None
        idx = self.__playlist[self.__playing_index]
        if idx < 0 or idx >= len(self.__tracks):
            return None
        return self.__tracks[idx]

    def __selected_track(self) -> Optional['RadioApp.Track']:
        if not self.__tracks:
            return None
        if self.__selected_index < 0 or self.__selected_index >= len(self.__tracks):
            return None
        return self.__tracks[self.__selected_index]

    def play_action(self) -> bool:
        if len(self.__playlist) == 0:
            self.__status_text = 'No tracks'
            return False

        selected_track = self.__selected_track()
        if selected_track is None:
            self.__status_text = 'No track selected'
            return False

        try:
            playlist_pos = self.__playlist.index(self.__selected_index)
        except ValueError:
            playlist_pos = 0

        if self.__selected_index != self.__playlist[self.__playing_index]:
            self.__playing_index = playlist_pos
            if self.__player.has_stream:
                self.__player.stop_stream()

        track = self.__tracks[self.__playlist[self.__playing_index]]
        self.__player.load_file(track.path)

        started = self.__player.start_stream()
        self.__status_text = 'Playing' if started else 'mpv missing?'
        return started

    def pause_action(self) -> bool:
        ok = self.__player.pause_stream()
        self.__status_text = 'Paused' if ok else 'Pause failed'
        return ok

    def stop_action(self):
        self.__player.stop_stream()
        self.__status_text = 'Stopped'

    def prev_action(self):
        if len(self.__playlist) == 0:
            return

        self.__playing_index = (self.__playing_index - 1) % len(self.__playlist)
        self.__selected_index = self.__playlist[self.__playing_index]

        if self.__player.has_stream:
            self.stop_action()
            self.play_action()

    def skip_action(self):
        if len(self.__playlist) == 0:
            return

        self.__playing_index = (self.__playing_index + 1) % len(self.__playlist)
        self.__selected_index = self.__playlist[self.__playing_index]

        if self.__player.has_stream:
            self.stop_action()
            self.play_action()

    def random_action(self) -> bool:
        if len(self.__tracks) == 0:
            return True

        current_selected = self.__selected_index
        self.__is_random = True
        self.__playlist = list(range(len(self.__tracks)))
        random.shuffle(self.__playlist)

        try:
            self.__playing_index = self.__playlist.index(current_selected)
        except ValueError:
            self.__playing_index = 0

        self.__status_text = 'Shuffle on'
        return True

    def order_action(self) -> bool:
        self.__is_random = False
        self.__playlist = list(range(len(self.__tracks)))
        if len(self.__tracks) > 0:
            self.__playing_index = min(self.__selected_index, len(self.__tracks) - 1)
        else:
            self.__playing_index = 0
        self.__status_text = 'Shuffle off'
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
            self.__status_text = f'Volume {self.__volume}%'
        except ValueError:
            self.__status_text = 'Volume error'

    def increase_volume_action(self):
        try:
            current_value = self.__get_volume()
            if current_value % self.__VOLUME_STEP == 0:
                self.__set_volume(min(current_value + self.__VOLUME_STEP, 100))
            else:
                aligned_value = (current_value + self.__VOLUME_STEP) // self.__VOLUME_STEP * self.__VOLUME_STEP
                self.__set_volume(min(aligned_value + self.__VOLUME_STEP, 100))
            self.__volume = self.__get_volume()
            self.__status_text = f'Volume {self.__volume}%'
        except ValueError:
            self.__status_text = 'Volume error'

    def __call_next(self):
        if self.__player.is_continuing and len(self.__playlist) > 0:
            self.__playing_index = (self.__playing_index + 1) % len(self.__playlist)
            self.__selected_index = self.__playlist[self.__playing_index]
            track = self.__current_track()
            if track is None:
                return
            self.__player.stop_stream()
            self.__player.load_file(track.path)
            self.__player.start_stream()
            self.__status_text = 'Playing'

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

    def __fit_text(self, text: str, max_width: int) -> str:
        fitted = text
        while self.__font.getbbox(fitted)[2] > max_width and len(fitted) > 0:
            fitted = fitted[:-1]
        return fitted

    @override
    def draw(self, image: Image.Image, partial=False) -> Generator[tuple[Image.Image, int, int], Any, None]:
        draw = ImageDraw.Draw(image)
        width, height = self.__app_size

        controls_total_width = (
            sum([c.size[0] for c in self.__controls]) +
            self.__CONTROL_PADDING * (len(self.__controls) - 1)
        )
        max_control_height = max([c.size[1] for c in self.__controls])
        cursor: tuple[int, int] = (
            width // 2 - controls_total_width // 2,
            height - max_control_height - self.__CONTROL_BOTTOM_OFFSET
        )
        for control in self.__controls:
            c_width, c_height = control.size
            control.draw(draw, (cursor[0], cursor[1] + (max_control_height - c_height) // 2))
            cursor = (cursor[0] + c_width + self.__CONTROL_PADDING, cursor[1])
        vertical_limit = cursor[1]

        vol_display = f'{self.__volume}%' if self.__volume is not None else 'N/A'
        text = f'Volume: {vol_display}'
        _, _, t_width, t_height = self.__font.getbbox(text)
        draw.text(
            (width // 2 - t_width // 2, vertical_limit - self.__META_INFO_HEIGHT // 2 - t_height // 2),
            text,
            self.__color,
            font=self.__font
        )
        vertical_limit -= self.__META_INFO_HEIGHT

        status = self.__status_text
        if self.__player.is_paused:
            status = 'Paused'
        elif self.__player.is_active:
            status = 'Playing'
        text = self.__fit_text(f'{self.__source_name} | {status}', width)
        _, _, t_width, t_height = self.__font.getbbox(text)
        draw.text(
            (width // 2 - t_width // 2, vertical_limit - self.__META_INFO_HEIGHT // 2 - t_height // 2),
            text,
            self.__color,
            font=self.__font
        )
        vertical_limit -= self.__META_INFO_HEIGHT

        playing_track = self.__current_track()
        if playing_track is not None and self.__player.has_stream:
            text = self.__fit_text(playing_track.display_name, width)
        else:
            text = 'Empty'
        _, _, t_width, t_height = self.__font.getbbox(text)
        draw.text(
            (width // 2 - t_width // 2, vertical_limit - self.__META_INFO_HEIGHT // 2 - t_height // 2),
            text,
            self.__color,
            font=self.__font
        )
        vertical_limit -= self.__META_INFO_HEIGHT

        left_top = (0, 0)
        left, top = left_top
        right_bottom = (width, vertical_limit)
        right, bottom = right_bottom
        max_entries = max(1, (bottom - top) // self.__LINE_HEIGHT)

        if len(self.__tracks) > max_entries:
            if self.__selected_index < self.__top_index:
                self.__top_index = self.__selected_index
            elif self.__selected_index not in range(self.__top_index, self.__top_index + max_entries):
                self.__top_index = self.__selected_index - max_entries + 1
        else:
            self.__top_index = 0

        cursor = left_top
        for index, track in enumerate(self.__tracks[self.__top_index:]):
            index += self.__top_index
            if self.__selected_index == index:
                draw.rectangle(cursor + (right, cursor[1] + self.__LINE_HEIGHT), self.__color_dark)
            if index == max_entries + self.__top_index:
                draw.text(cursor, '...', self.__color, font=self.__font)
                break

            prefix = '> ' if (
                self.__playlist and
                index == self.__playlist[self.__playing_index] and
                self.__player.has_stream
            ) else ''
            text = self.__fit_text(prefix + track.display_name, right - left)
            draw.text(cursor, text, self.__color, font=self.__font)
            cursor = (cursor[0], cursor[1] + self.__LINE_HEIGHT)

        if partial:
            right_bottom = width, height
            yield image.crop(left_top + right_bottom), *left_top
        else:
            yield image, 0, 0

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
        self.__selected_index = min(self.__selected_index + 1, len(self.__tracks) - 1)

    @override
    def on_key_a(self):
        self.__controls[self.__selected_control_index].on_select()

    @override
    def on_app_enter(self):
        super().on_app_enter()
        self.__reload_library()
        self.__controls[self.__selected_control_index].on_focus()

        if len(self.__tracks) > 0:
            try:
                if not self.__player.is_active:
                    self.play_action()
            except Exception as ex:
                logger.warning("RAD autoplay failed: %s", ex)

    @override
    def on_app_leave(self):
        try:
            self.stop_action()
        except Exception:
            pass
        super().on_app_leave()