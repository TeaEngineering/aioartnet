import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional, Sequence

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style

from . import DMX_UNIVERSE_SIZE, ArtNetClient


class FadeState(Enum):
    OFF = auto()
    FADE_IN = auto()
    HOLD = auto()
    FADE_OUT = auto()


@dataclass
class ChannelIntensity:
    channel: int
    intensity: int

    def __repr__(self) -> str:
        # compact, 1-based to match the rest of the console UI, e.g. "5@255"
        return f"{self.channel + 1}@{self.intensity}"


@dataclass
class Cue:
    name: str
    fade_in: float
    hold: float
    fade_out: float
    channels: list[ChannelIntensity] = field(default_factory=list)


@dataclass
class Submaster:
    intensity: float
    name: str
    channels: list[ChannelIntensity] = field(default_factory=list)


@dataclass
class ActiveCue:
    cue: Cue
    since: float
    state: FadeState = FadeState.FADE_IN

    def get_update_intensity(self, time: float) -> float:
        cue = self.cue
        uptime = time - self.since
        if uptime < cue.fade_in:
            self.state = FadeState.FADE_IN
            return uptime / cue.fade_in
        elif uptime < cue.fade_in + cue.hold:
            self.state = FadeState.HOLD
            return 1.0
        elif uptime < cue.fade_in + cue.hold + cue.fade_out:
            self.state = FadeState.FADE_OUT
            return 1.0 - ((uptime - cue.fade_in - cue.hold) / cue.fade_out)
        else:
            self.state = FadeState.OFF
            return 0.0


# per-channel provenance, tracked alongside the mixed output so the UI can
# colour each channel by what is driving it
SRC_UNDRIVEN = 0  # nothing active touches this channel
SRC_STORED = 1  # held by an active cue or submaster
SRC_LIVE = 2  # overridden by a live edit
SRC_DEFAULT = 3  # held at a fixture profile default (base layer)
SRC_FX = 4  # driven by an effect unit


def apply_ci(
    data: bytearray,
    edits: Sequence[ChannelIntensity],
    scale: float,
    htp: bool = False,
) -> None:
    for e in edits:
        val = max(0, min(255, int(e.intensity * scale)))
        # highest-takes-precedence keeps the brighter of the existing/new level
        data[e.channel] = max(data[e.channel], val) if htp else val


# ---- effect engine -------------------------------------------------------
# colourmaps are defined by control points (QLC+ style); each consecutive pair
# is expanded to 300 linearly-interpolated samples, so a 2-point map is 300
# samples and a 4-point map is 900.
COLOUR_POINTS: dict[str, list[int]] = {
    "rainbow": [0xFF0000, 0x00FF00, 0x0000FF],
    "fire": [0xFFFF00, 0xFF0000, 0x000040, 0xFF0000],
    "abstract": [0x5571FF, 0x00FFFF, 0xFF00FF, 0xFFFF00],
    "ocean": [0x003AB9, 0x02EAFF],
}
_LUT_STEPS = 300


def build_lut(points: list[int]) -> list[tuple[int, int, int]]:
    def rgb(c: int) -> tuple[int, int, int]:
        return ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)

    lut: list[tuple[int, int, int]] = []
    for a, b in zip(points, points[1:]):
        ra, ga, ba = rgb(a)
        rb, gb, bb = rgb(b)
        for s in range(_LUT_STEPS):
            f = s / _LUT_STEPS
            lut.append(
                (
                    round(ra + (rb - ra) * f),
                    round(ga + (gb - ga) * f),
                    round(ba + (bb - ba) * f),
                )
            )
    return lut


COLOURMAPS: dict[str, list[tuple[int, int, int]]] = {
    name: build_lut(pts) for name, pts in COLOUR_POINTS.items()
}

# parameter specs: name -> ("cont", default) | ("enum", default, choices)
# continuous params live as a 0.0-1.0 fraction; explicit values are 0-100 percent
# and CC values are v/127, so the two are interchangeable.
RGB_PARAMS: dict[str, tuple] = {
    "intensity": ("cont", 0.0),
    "speed": ("cont", 0.2),
    "spread": ("cont", 0.3),
    "style": ("enum", "rainbow", tuple(COLOURMAPS)),
}
PT_PARAMS: dict[str, tuple] = {
    "intensity": ("cont", 0.0),
    "speed": ("cont", 0.2),
    "size": ("cont", 0.25),
    "spread": ("cont", 0.3),
    "mode": ("enum", "circle", ("static", "circle", "wave", "sway")),
}
EFFECT_PARAMS: dict[str, dict[str, tuple]] = {"rgb": RGB_PARAMS, "pt": PT_PARAMS}

SPEED_MAX_HZ = 1.0  # animation rate at speed = 100%
PT_AMP_MAX = 127  # pan/tilt delta in DMX steps at size = 100%


@dataclass
class EffectLane:
    index: int
    channels: dict[str, int]  # role ("red"/"pan"/...) -> absolute DMX channel
    home: tuple[int, int] = (128, 128)  # PT only (pan, tilt)


class Effect:
    """A time-based effect that writes its group's channels each engine tick."""

    unit = ""
    spec: dict[str, tuple] = {}

    def __init__(self) -> None:
        self.group: Optional[str] = None
        self.lanes: list[EffectLane] = []
        self.params: dict[str, Any] = {
            name: s[1] for name, s in self.spec.items()
        }

    def is_active(self) -> bool:
        return float(self.params["intensity"]) > 0.0

    def tick(self, live: bytearray, source: bytearray, t: float) -> None:
        raise NotImplementedError


class RgbEffect(Effect):
    unit = "rgb"
    spec = RGB_PARAMS

    def tick(self, live: bytearray, source: bytearray, t: float) -> None:
        n = len(self.lanes)
        if n == 0:
            return
        lut = COLOURMAPS[self.params["style"]]
        total = len(lut)
        speed = float(self.params["speed"]) * SPEED_MAX_HZ
        spread = float(self.params["spread"])
        bri = float(self.params["intensity"])
        for lane in self.lanes:
            phase = (speed * t + (lane.index / n) * spread) % 1.0
            r, g, b = lut[int(phase * total) % total]
            for role, val in (("red", r), ("green", g), ("blue", b)):
                ch = lane.channels.get(role)
                if ch is None:
                    continue
                live[ch] = max(0, min(255, round(val * bri)))
                source[ch] = SRC_FX


class PtEffect(Effect):
    unit = "pt"
    spec = PT_PARAMS

    _SHAPES = {
        "static": lambda th: (0.0, 0.0),
        "circle": lambda th: (math.cos(th), math.sin(th)),
        "wave": lambda th: (math.sin(th), 0.0),
        "sway": lambda th: (0.0, math.sin(th)),
    }

    def tick(self, live: bytearray, source: bytearray, t: float) -> None:
        n = len(self.lanes)
        if n == 0:
            return
        speed = float(self.params["speed"]) * SPEED_MAX_HZ
        spread = float(self.params["spread"])
        amp = float(self.params["size"]) * PT_AMP_MAX * float(self.params["intensity"])
        shape = self._SHAPES[self.params["mode"]]
        for lane in self.lanes:
            theta = 2 * math.pi * (speed * t + (lane.index / n) * spread)
            dpan, dtilt = shape(theta)
            for role, home, delta in (
                ("pan", lane.home[0], dpan),
                ("tilt", lane.home[1], dtilt),
            ):
                ch = lane.channels.get(role)
                if ch is None:
                    continue
                live[ch] = max(0, min(255, home + round(amp * delta)))
                source[ch] = SRC_FX


EFFECT_CLASSES: dict[str, type[Effect]] = {"rgb": RgbEffect, "pt": PtEffect}


def format_effect_param(unit: str, param: str, value: Any) -> str:
    # continuous -> 0-100 percent, enum -> name (inverse of _set_fx_param)
    if EFFECT_PARAMS[unit][param][0] == "enum":
        return str(value)
    return str(round(float(value) * 100))


class Engine:
    def __init__(
        self, handler: Callable[[bytes], None], universe_size: int = DMX_UNIVERSE_SIZE
    ):
        self.universe_size = universe_size
        self.last_poll: float = 0
        self.cues: list[Cue] = []
        # an index into cues[] of our current playback position, or None if stopped
        self.active_cue: int | None = None
        self.subs: list[Submaster] = []
        # base layer: fixture profile defaults, output beneath cues/subs/edits
        self.base = bytearray(universe_size)
        self.live = bytearray(universe_size)
        # per-channel SRC_* provenance for the most recent poll
        self.last_source = bytearray(universe_size)
        self.handler = handler
        self.loop = False
        self.edits: list[ChannelIntensity] = []
        self.live_edit = False
        self.tickhz = 10
        self._active_cues: list[ActiveCue] = []
        # effect units (e.g. "rgb", "pt"), ticked each poll beneath live edits
        self.effects: dict[str, Effect] = {}

    async def poll(self, time: float) -> None:
        # advance time
        self.last_poll = max(self.last_poll, time)

        # start from the base layer (fixture profile defaults)
        live = bytearray(self.base)
        # every cue is either off, fade-in, hold, or fade-out
        for ac in self._active_cues:
            intensity = ac.get_update_intensity(self.last_poll)
            apply_ci(live, ac.cue.channels, scale=intensity)

        for ac in self._active_cues:
            if ac.state == FadeState.OFF:
                print(f"finished {ac}")

        self._active_cues[:] = [
            x for x in self._active_cues if x.state != FadeState.OFF
        ]

        # mix in any non-zero submasters (highest-takes-precedence)
        for sub in self.subs:
            if sub.intensity > 0:
                apply_ci(live, sub.channels, scale=sub.intensity, htp=True)

        # classify provenance so far: base defaults lowest, then cues/subs
        # "stored". Effects and live edits stamp themselves below.
        source = bytearray(
            SRC_DEFAULT if b else SRC_UNDRIVEN for b in self.base
        )
        for ac in self._active_cues:
            for e in ac.cue.channels:
                source[e.channel] = SRC_STORED
        for sub in self.subs:
            if sub.intensity > 0:
                for e in sub.channels:
                    source[e.channel] = SRC_STORED

        # effects sit above cues/subs; tick() writes live and stamps SRC_FX
        for eff in self.effects.values():
            if eff.is_active():
                eff.tick(live, source, self.last_poll)

        # live edits punch over everything, including effects
        if self.live_edit:
            apply_ci(live, self.edits, 1.0)
            for e in self.edits:
                source[e.channel] = SRC_LIVE

        self.last_source = source
        self.live[:] = live  # keep the last output (used by `fx pt home` capture)

        # print(f'calling handler {self.handler} with {self.live}')
        self.handler(live)

    async def go_relative(self, change: int) -> None:
        if len(self.cues) == 0:
            self.active_cue = None
            return
        # special handling for resuming/starting first cue
        if self.active_cue is None:
            next_cue = 0 if change == 1 else len(self.cues)
        else:
            next_cue = self.active_cue + (1 if change == 1 else -1)
        # if current queue is held, nudge it to fade-out
        next_cue = next_cue % len(self.cues)
        self.activate(self.cues[next_cue])
        self.active_cue = next_cue

    def activate(self, cue: Cue) -> None:
        # activates a cue, with the start time of our last poll
        self._active_cues.append(ac := ActiveCue(since=self.last_poll, cue=cue))
        print(f"activated {ac}")

    async def stop(self) -> None:
        self.active_cue = None

    async def go_absolute(self, cue_num: int) -> None:
        # cc = self.cues[self.active_cue]
        print(cue_num)

    async def clear_edits(self) -> None:
        self.edits = []
        await self.poll(0)

    def add_edit(self, edit: ChannelIntensity) -> None:
        ci = self.edits
        for i in range(len(ci)):
            if ci[i].channel == edit.channel:
                ci[i] = edit
                break
        else:
            ci.append(edit)

    async def start_ticking(self) -> asyncio.Task[None]:
        self._tick_task = asyncio.create_task(self._tick())
        return self._tick_task

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            await self.poll(time.time())


# MIDI status bytes (high nibble of the status byte). Defined locally so this
# module imports without python-rtmidi present; rtmidi is only needed to open a
# port (see open_midi_input).
NOTE_OFF = 0x80
NOTE_ON = 0x90
POLY_AFTERTOUCH = 0xA0
CONTROLLER_CHANGE = 0xB0
PROGRAM_CHANGE = 0xC0
CHANNEL_AFTERTOUCH = 0xD0


class MidiCC:
    """Polls a MIDI input, tracking held notes and the latest CC values.

    Lifted from the ola_pilot desk and adapted to aioartnet: it keeps
    ``notes_on`` (the instantaneously held keys) and ``cc_last`` (the most
    recent value seen for every CC channel), and dispatches CC changes to any
    bound listeners. ``rtmidi``'s ``get_message()`` is non-blocking, so polling
    drains the queue each tick.
    """

    def __init__(self, midi_in: Any, cc_defaults: Optional[dict[int, int]] = None):
        self.midi_in = midi_in
        self.notes_on: dict[int, int] = {}
        # pre-seed CC channels so they appear in the view before any message
        self.cc_last: dict[int, int] = dict(cc_defaults) if cc_defaults else {}
        self.cc_pending: dict[int, int] = {}
        self.cc_listeners: dict[int, list[Callable[[int], None]]] = defaultdict(list)
        # note listeners get True on press, False on release
        self.note_listeners: dict[int, list[Callable[[bool], None]]] = defaultdict(
            list
        )

    def _fire_note(self, note: int, pressed: bool) -> None:
        for listener in self.note_listeners.get(note, []):
            listener(pressed)

    def poll(self) -> None:
        # drain every queued message (get_message() returns None when empty)
        while True:
            msg = self.midi_in.get_message()
            if not msg:
                break
            message, _timedelta = msg
            status = message[0] & 0xF0
            if status == CONTROLLER_CHANGE:
                self.cc_last[message[1]] = message[2]
                self.cc_pending[message[1]] = message[2]
            elif status == NOTE_ON:
                note, vel = message[1], message[2]
                if vel == 0:  # note-on velocity 0 is conventionally a note-off
                    if self.notes_on.pop(note, None) is not None:
                        self._fire_note(note, False)
                elif note not in self.notes_on:  # only fire on the press edge
                    self.notes_on[note] = vel
                    self._fire_note(note, True)
                else:
                    self.notes_on[note] = vel
            elif status == NOTE_OFF:
                if self.notes_on.pop(message[1], None) is not None:
                    self._fire_note(message[1], False)
            elif status == POLY_AFTERTOUCH:
                self.notes_on[message[1]] = message[2]
            # CHANNEL_AFTERTOUCH / PROGRAM_CHANGE / clock / etc. are ignored

        # dispatch CC changes seen this tick to any bound listeners
        for k, v in self.cc_pending.items():
            for listener in self.cc_listeners[k]:
                listener(v)
        self.cc_pending.clear()

    def bind_cc(self, channel: int, listener: Callable[[int], None]) -> None:
        self.cc_listeners[channel].append(listener)
        self.cc_last.setdefault(channel, 0)

    def bind_note(self, note: int, listener: Callable[[bool], None]) -> None:
        self.note_listeners[note].append(listener)

    async def run(self, interval: float = 0.01) -> None:
        while True:
            self.poll()
            await asyncio.sleep(interval)

    def __repr__(self) -> str:
        return f"CC: {self.cc_last}\nNotes: {self.notes_on}"


def open_midi_input(
    port: "int | str | None" = None,
    cc_defaults: Optional[dict[int, int]] = None,
) -> "MidiCC":
    """Open a MIDI input and wrap it in a MidiCC.

    ``port`` may be an integer device index or a substring of the port name.
    """
    from rtmidi.midiutil import open_midiinput  # type: ignore[import-untyped]

    midi_in, _port_name = open_midiinput(port=port)
    return MidiCC(midi_in, cc_defaults=cc_defaults)


CONFIG_PATH = os.path.expanduser("~/.aioartnet/console")


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    """Load the JSON console config, or return {} if it is absent/unreadable."""
    try:
        with open(path) as f:
            config: dict[str, Any] = json.load(f)
            return config
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"warning: ignoring bad config {path}: {e}")
        return {}


def setup_midi_from_config(
    config: dict[str, Any], cli_port: "int | str | None"
) -> Optional[MidiCC]:
    """Decide whether/how to enable MIDI from the config and CLI override.

    ``cli_port`` is the value of ``--midi-in`` (None if the flag was absent, ""
    for a bare flag, or a port substring/index). A present CLI flag forces MIDI
    on; otherwise the config's ``midi.enabled`` decides.
    """
    midi_cfg = config.get("midi", {})
    cc_defaults = {int(k): int(v) for k, v in midi_cfg.get("cc_defaults", {}).items()}

    if cli_port is not None:
        # explicit --midi-in wins; fall back to the configured device for `-m`
        port = cli_port if cli_port != "" else midi_cfg.get("device")
    elif midi_cfg.get("enabled"):
        port = midi_cfg.get("device")
    else:
        return None

    return open_midi_input(port, cc_defaults=cc_defaults)


def parse_intensity(level: str) -> int:
    if level.upper() in ("F", "FL", "FULL"):
        return 255
    elif level.upper() in ("Z", "ZERO"):
        return 0
    elif level.upper() in ("H", "HALF"):
        return 128
    elif level.lower().startswith("0x"):
        return min(255, int(level[2:], base=16))
    else:
        return min(255, int((int(level) / 100.0) * 256))


def parse_user_index(value: str, items: Sequence[Any], extend: bool = False) -> int:
    # python indexes from 0 to len(items)-1
    # we allow 'user' indexing from 1 to len(items) for overwrites, and
    # from 1 to len(items)+1 if we are doing a write operation to extend the
    # collection
    cn = int(value) - 1
    if cn < 0:
        raise ValueError("numbering starts from 1")
    if cn > len(items) + {True: 1, False: 0}[extend]:
        raise ValueError(f"highest existing item is {len(items)}")
    return cn


def parse_duration(value: str) -> int:
    return int(value)


NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "amber": (255, 191, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "yellow": (255, 255, 0),
    "black": (0, 0, 0),
    "off": (0, 0, 0),
}


def parse_color(token: str) -> tuple[int, int, int]:
    """Parse `#rrggbb` or a named colour into a 0-255 RGB triple."""
    if token.startswith("#") and len(token) == 7:
        h = token[1:]
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    if token in NAMED_COLORS:
        return NAMED_COLORS[token]
    raise ValueError(f"unknown colour {token!r}")


@dataclass
class FixtureProfile:
    """A fixture personality: the attribute at each channel offset, plus any
    enumerated (macro) values for raw channels like gobo/strobe."""

    name: str
    channels: list[str]  # attr name per offset ("-"/"" for unaddressed channels)
    enums: dict[str, dict[str, int]]  # attr -> {macro name: raw DMX byte}
    defaults: dict[str, int]  # attr -> base value held beneath cues/subs/edits

    @property
    def footprint(self) -> int:
        return len(self.channels)

    def offset(self, attr: str) -> Optional[int]:
        try:
            return self.channels.index(attr)
        except ValueError:
            return None


def parse_profile(name: str, entries: list[Any]) -> FixtureProfile:
    """Build a FixtureProfile from its config list. Each entry is one of:
    - a bare attr name:                  "red"
    - a macro channel (value is a map):  {"gobo": {"open": 0, "stars": 50}}
    - a defaulted channel (value int):   {"global_dimmer": 255}
    """
    channels: list[str] = []
    enums: dict[str, dict[str, int]] = {}
    defaults: dict[str, int] = {}
    for entry in entries:
        if isinstance(entry, str):
            channels.append(entry)
        elif isinstance(entry, dict) and len(entry) == 1:
            attr, spec = next(iter(entry.items()))
            channels.append(attr)
            if isinstance(spec, dict):
                enums[attr] = {k: int(v) for k, v in spec.items()}
            elif isinstance(spec, int) and not isinstance(spec, bool):
                defaults[attr] = min(255, max(0, spec))
            else:
                raise ValueError(f"bad channel spec in profile {name}: {entry!r}")
        else:
            raise ValueError(f"bad channel entry in profile {name}: {entry!r}")
    return FixtureProfile(
        name=name, channels=channels, enums=enums, defaults=defaults
    )


def load_profiles(config: dict[str, Any]) -> dict[str, FixtureProfile]:
    return {
        name: parse_profile(name, entries)
        for name, entries in config.get("fixtures", {}).items()
    }


@dataclass
class PatchedFixture:
    """An instance of a profile patched at a base DMX channel (0-based)."""

    kind: str  # profile name
    label: str  # e.g. "wash"
    number: int  # 1-based
    base: int  # 0-based channel of offset 0
    profile: FixtureProfile


class DmxGrid:
    """Renders a live DMX universe as a grid of channel levels.

    Each cell's text comes from the channel value (hex or percent) and its
    colour from the channel's provenance: light grey when undriven, black when
    held by a cue/submaster, and red when overridden by a live edit.

    The grid geometry is static (512 cells never move), so the value texts and
    the source colours are precomputed and a frame is only rebuilt when the
    value or source bytes actually change (dirty-skip). That keeps each refresh
    down to a list rebuild + prompt_toolkit's terminal diff, with no per-cell
    string formatting or colour parsing.
    """

    COLS = 32

    _SRC_STYLE = {
        SRC_UNDRIVEN: "fg:#b0b0b0",  # light grey
        SRC_STORED: "fg:black",
        SRC_LIVE: "fg:red",
        SRC_DEFAULT: "fg:#2a6fdb",  # blue: held at a fixture profile default
        SRC_FX: "fg:#d08020",  # amber: driven by an effect unit
    }

    def __init__(
        self,
        source: Callable[[], bytes],
        source_map: Optional[Callable[[], bytes]] = None,
        label: str = "",
    ) -> None:
        self.source = source
        self.source_map = source_map
        self.label = label
        self.mode = "hex"  # "hex" or "pct"
        self.trim = False  # clip the grid to the highest patched channel
        # returns the channel count to show when trimmed (0 = nothing patched)
        self.trim_source: Optional[Callable[[], int]] = None
        self._last_data: bytes = b""
        self._last_src: bytes = b""
        self._cache_mode: Optional[str] = None
        self._cache_vis = -1
        self._cache: StyleAndTextTuples = []
        self._text: dict[str, list[str]] = {
            "hex": [self._cell_text(v, "hex") for v in range(256)],
            "pct": [self._cell_text(v, "pct") for v in range(256)],
        }

    @staticmethod
    def _cell_text(value: int, mode: str) -> str:
        if mode == "hex":
            text = f"{value:02X}"
        else:
            pct = round(value / 255 * 100)
            if value == 0:
                text = ".."
            elif pct >= 100:
                text = "FL"
            else:
                text = f"{pct:02d}"
        return text + " "

    @property
    def visible_channels(self) -> int:
        """Channels to render: all of them, or rounded up to a full row of the
        highest patched channel when trimmed (at least one row)."""
        if not self.trim or self.trim_source is None:
            return DMX_UNIVERSE_SIZE
        extent = self.trim_source()
        rows = max(1, (extent + self.COLS - 1) // self.COLS)
        return min(DMX_UNIVERSE_SIZE, rows * self.COLS)

    @property
    def height(self) -> int:
        rows = (self.visible_channels + self.COLS - 1) // self.COLS
        return rows + 2  # title line + column header

    def render(self) -> StyleAndTextTuples:
        data = self.source()
        src = self.source_map() if self.source_map is not None else b""
        vis = self.visible_channels
        if (
            bytes(data) == self._last_data
            and bytes(src) == self._last_src
            and self._cache_mode == self.mode
            and self._cache_vis == vis
        ):
            return self._cache
        self._last_data = bytes(data)
        self._last_src = bytes(src)
        self._cache_mode = self.mode
        self._cache_vis = vis
        text = self._text[self.mode]

        tag = f"{self.mode}, trim" if self.trim else self.mode
        frags: StyleAndTextTuples = [
            ("bold", f" DMX {self.label}  [{tag}]\n"),
            ("class:dim", "     "),
        ]
        for c in range(self.COLS):
            frags.append(("class:dim", f"{c + 1:>2} "))
        for ch in range(vis):
            if ch % self.COLS == 0:
                frags.append(("class:dim", f"\n{ch + 1:>4} "))
            code = src[ch] if ch < len(src) else SRC_UNDRIVEN
            frags.append((self._SRC_STYLE[code], text[data[ch]]))

        self._cache = frags
        return frags


class MidiView:
    """Renders the live MIDI state: held keys and last-seen CC values.

    Held notes (``notes_on``) are instantaneous; CC values (``cc_last``)
    accumulate every channel that has been seen since startup.
    """

    HEIGHT = 4  # title + keys line + cc line(s)

    def __init__(self, midi: Optional[MidiCC]) -> None:
        self.midi = midi

    def render(self) -> StyleAndTextTuples:
        frags: StyleAndTextTuples = [("bold", " MIDI\n")]
        if self.midi is None:
            frags.append(("class:dim", " not enabled (start with --midi-in)"))
            return frags

        frags.append(("class:dim", " keys: "))
        if self.midi.notes_on:
            for note, vel in sorted(self.midi.notes_on.items()):
                frags.append(("fg:red", f"{note}:{vel} "))
        else:
            frags.append(("class:dim", "-"))

        frags.append(("", "\n"))
        frags.append(("class:dim", " cc:   "))
        if self.midi.cc_last:
            for cc, val in sorted(self.midi.cc_last.items()):
                frags.append(("fg:black", f"{cc}={val:<3} "))
        else:
            frags.append(("class:dim", "-"))
        return frags


class FxView:
    """Renders the live state of each effect unit: group and current params."""

    HEIGHT = 3  # title + one line per unit (rgb, pt)

    def __init__(self, engine: "Engine") -> None:
        self.engine = engine

    def render(self) -> StyleAndTextTuples:
        frags: StyleAndTextTuples = [("bold", " FX\n")]
        effects = self.engine.effects
        if not effects:
            frags.append(("class:dim", " (no effects configured)"))
            return frags
        for unit, eff in effects.items():
            active = eff.is_active()
            frags.append(("class:dim", f" {unit:<3} "))
            frags.append(("class:dim", f"group={eff.group} "))
            frags.append(
                ("fg:#d08020", "[active] ") if active else ("class:dim", "[off]   ")
            )
            for param, value in eff.params.items():
                frags.append(("", f"{param}={format_effect_param(unit, param, value)} "))
            frags.append(("", "\n"))
        return frags


HELP_TEXT = """\
Available commands:
  live on|off                       enable/disable live edits in the output
  loop on|off                       enable/disable looping playback
  chan|ch N at|@ LEVEL              set channel N to LEVEL
  chan|ch A thru B at LEVEL         set channels A..B to LEVEL
  sub|submaster N at|@ LEVEL        set submaster N to LEVEL
  record cue|sub N [fade D]         record current edits as cue/sub N
            [fade_in D] [fade_out D] [hold D]
  midi bind cc C sub N              bind MIDI CC C to submaster N
  midi bind cc C                    unbind MIDI CC C
  midi bind note K flash sub N      pad K flashes submaster N while held
  midi bind note K                  unbind pad K
  patch PROFILE LBL N [thru M] @ A  patch fixture(s) of PROFILE at address A
  group NAME = SELECTOR             name a group of fixtures
  fix                               list patched fixtures
  fix SELECTOR ATTR VAL [...]       set fixture attribute(s)
  fix SELECTOR color #RRGGBB|NAME   set fixture colour (red/green/blue)
  fix SELECTOR at LEVEL             set fixture dimmer
            SELECTOR = LBL N | LBL N thru M | group name
  fx                                list effect units
  fx rgb|pt group GRP               point an effect at a fixture group
  fx UNIT PARAM VALUE               set effect param (0-100, or style/mode name)
            rgb: intensity speed spread style(rainbow|ocean|fire|abstract)
            pt:  intensity speed size spread mode(static|circle|wave|sway)
  fx rgb|pt off                     intensity to 0
  fx pt home [PAN TILT]             capture (or set) the group's home positions
  midi bind cc C fx UNIT PARAM      drive an effect param from a CC knob
  midi bind note K fx UNIT PARAM V  pad K sets an effect param (e.g. style)
  edits|edit|dirty                  show the current uncommitted edits
  go                                advance to the next cue
  back                              return to the previous cue
  go N                              jump to cue N
  clear                             clear the current edits
  list                              list all cues, submasters and bindings
  save [path]                       save the show to path (or the --file)
  tickhz N                          set the engine tick rate (Hz)
  view hex|pct [trim|all]           grid value format; trim clips to patched
LEVEL may be a percentage (0-100), a hex value (0xFF), or F/FULL, H/HALF, Z/ZERO."""


# interprets simple commands and formats the responses
class Interpreter:
    def __init__(
        self,
        engine: Engine,
        grid: Optional[DmxGrid] = None,
        midi: Optional[MidiCC] = None,
        profiles: Optional[dict[str, FixtureProfile]] = None,
    ):
        self.engine = engine
        self.grid = grid
        self.midi = midi
        # cc -> binding target: ("sub", idx) or ("fx", unit, param)
        self.bindings: dict[int, tuple] = {}
        # cc's that already have a live listener attached (avoid stacking)
        self._wired_cc: set[int] = set()
        # note -> target: ("flash", idx) or ("fx", unit, param, value)
        self.note_bindings: dict[int, tuple] = {}
        self._wired_notes: set[int] = set()
        # intensity saved while a flash is held, restored on release
        self._flash_saved: dict[int, float] = {}
        # path of the show file (from --file); target for a bare `save`
        self.showfile_path: Optional[str] = None
        # fixture profiles (from config), patched instances, and groups
        self.profiles = profiles or {}
        self.fixtures: dict[tuple[str, int], PatchedFixture] = {}
        self._labels: set[str] = set()  # patched fixture labels, e.g. {"wash"}
        self.groups: dict[str, list[tuple[str, int]]] = {}
        # per-fixture pan/tilt home positions for the PT effect
        self.homes: dict[tuple[str, int], tuple[int, int]] = {}

    def _fixture(self, label: str, number: int) -> PatchedFixture:
        try:
            return self.fixtures[(label, number)]
        except KeyError:
            raise ValueError(f"no fixture {label} {number}")

    def _take_selector(
        self, tokens: list[str]
    ) -> tuple[list[PatchedFixture], list[str]]:
        """Consume a selector from the front of `tokens`: fixture refs
        (`wash 1`, `wash 1 thru 6`) and group names, in any combination.
        Returns the resolved fixtures and the unconsumed remainder."""
        out: list[PatchedFixture] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in self.groups:
                out.extend(self._fixture(lbl, n) for lbl, n in self.groups[tok])
                i += 1
            elif tok in self._labels:
                n1 = int(tokens[i + 1])
                if i + 2 < len(tokens) and tokens[i + 2] == "thru":
                    n2 = int(tokens[i + 3])
                    out.extend(self._fixture(tok, n) for n in range(n1, n2 + 1))
                    i += 4
                else:
                    out.append(self._fixture(tok, n1))
                    i += 2
            else:
                break  # start of the attribute list
        return out, tokens[i:]

    def _set_attr(self, fx: PatchedFixture, attr: str, token: str) -> None:
        offset = fx.profile.offset(attr)
        if offset is None:
            return  # this fixture has no such attribute (e.g. mixed group)
        if attr in fx.profile.enums:
            macros = fx.profile.enums[attr]
            if token in macros:
                byte = macros[token]
            else:
                try:
                    byte = min(255, int(token, 0))  # raw channel: literal byte
                except ValueError:
                    raise ValueError(f"{attr}: unknown macro {token!r}")
        else:
            byte = parse_intensity(token)  # intensity-scaled channel
        self.engine.add_edit(
            ChannelIntensity(channel=fx.base + offset, intensity=byte)
        )

    def _set_channel(self, fx: PatchedFixture, attr: str, byte: int) -> None:
        offset = fx.profile.offset(attr)
        if offset is None:
            return
        self.engine.add_edit(
            ChannelIntensity(channel=fx.base + offset, intensity=byte)
        )

    def _apply_fix(self, fixtures: list[PatchedFixture], tokens: list[str]) -> None:
        i = 0
        while i < len(tokens):
            attr = tokens[i]
            if attr in ("at", "@"):  # `at` is shorthand for the dimmer channel
                for fx in fixtures:
                    self._set_attr(fx, "dimmer", tokens[i + 1])
                i += 2
            elif attr == "color":
                r, g, b = parse_color(tokens[i + 1])
                for fx in fixtures:
                    self._set_channel(fx, "red", r)
                    self._set_channel(fx, "green", g)
                    self._set_channel(fx, "blue", b)
                i += 2
            else:
                for fx in fixtures:
                    self._set_attr(fx, attr, tokens[i + 1])
                i += 2

    # ---- effect units -----------------------------------------------------
    def _ensure_effect(self, unit: str) -> Effect:
        if unit not in EFFECT_CLASSES:
            raise ValueError(f"unknown effect unit {unit!r}")
        eff = self.engine.effects.get(unit)
        if eff is None:
            eff = EFFECT_CLASSES[unit]()
            self.engine.effects[unit] = eff
        return eff

    def _patched_extent(self) -> int:
        # highest patched channel (1-based count), for trimming the DMX grid
        return max(
            (fx.base + fx.profile.footprint for fx in self.fixtures.values()),
            default=0,
        )

    def _set_view(self, opts: list[str]) -> None:
        if self.grid is None:
            return
        for opt in opts:
            if opt == "hex":
                self.grid.mode = "hex"
            elif opt in ("pct", "percent", "fl"):
                self.grid.mode = "pct"
            elif opt == "trim":
                self.grid.trim = True
            elif opt == "all":
                self.grid.trim = False
            else:
                raise ValueError(f"unknown view option {opt!r}")

    def _default_home(self, fx: PatchedFixture) -> tuple[int, int]:
        defaults = fx.profile.defaults
        return (defaults.get("pan", 128), defaults.get("tilt", 128))

    def _resolve_lanes(self, unit: str, group: str) -> list[EffectLane]:
        roles = ("red", "green", "blue") if unit == "rgb" else ("pan", "tilt")
        lanes: list[EffectLane] = []
        for label, number in self.groups.get(group, []):
            fx = self._fixture(label, number)
            channels = {
                role: fx.base + off
                for role in roles
                if (off := fx.profile.offset(role)) is not None
            }
            if not channels:
                continue  # this fixture has none of the needed roles
            home = self.homes.get((label, number)) or self._default_home(fx)
            lanes.append(EffectLane(len(lanes), channels, home))
        return lanes

    def _rebind_effects(self) -> None:
        for unit, eff in self.engine.effects.items():
            if eff.group is not None:
                eff.lanes = self._resolve_lanes(unit, eff.group)

    def _set_fx_param(self, unit: str, param: str, value: str) -> None:
        eff = self._ensure_effect(unit)
        spec = EFFECT_PARAMS[unit].get(param)
        if spec is None:
            raise ValueError(f"{unit}: unknown param {param!r}")
        if spec[0] == "enum":
            if value not in spec[2]:
                raise ValueError(f"{unit} {param}: expected one of {spec[2]}")
            eff.params[param] = value
        else:  # continuous: explicit 0-100 percent -> 0.0-1.0
            eff.params[param] = max(0.0, min(1.0, float(value) / 100.0))

    def _validate_fx_param(
        self, unit: str, param: str, allow_enum: bool = True
    ) -> None:
        if unit not in EFFECT_CLASSES:
            raise ValueError(f"unknown effect unit {unit!r}")
        spec = EFFECT_PARAMS[unit].get(param)
        if spec is None:
            raise ValueError(f"{unit}: unknown param {param!r}")
        if not allow_enum and spec[0] == "enum":
            raise ValueError(f"{unit} {param} is enumerated; bind it to a note")
        self._ensure_effect(unit)  # so snap-on-bind has something to drive

    @staticmethod
    def _fmt_param(unit: str, param: str, value: Any) -> str:
        return format_effect_param(unit, param, value)

    def _pt_group(self) -> list[tuple[str, int]]:
        eff = self.engine.effects.get("pt")
        if eff is None or eff.group is None:
            raise ValueError("assign a group first: fx pt group <grp>")
        return self.groups.get(eff.group, [])

    def _capture_pt_home(self) -> None:
        # snapshot each group fixture's current pan/tilt output as its home
        for label, number in self._pt_group():
            fx = self._fixture(label, number)
            po = fx.profile.offset("pan")
            to = fx.profile.offset("tilt")
            pan = self.engine.live[fx.base + po] if po is not None else 128
            tilt = self.engine.live[fx.base + to] if to is not None else 128
            self.homes[(label, number)] = (pan, tilt)
        self._rebind_effects()

    def _set_pt_home_all(self, pan: int, tilt: int) -> None:
        for ref in self._pt_group():
            self.homes[ref] = (pan, tilt)
        self._rebind_effects()

    def _list_effects(self) -> None:
        if not self.engine.effects:
            print("No effects")
            return
        for unit, eff in self.engine.effects.items():
            params = " ".join(
                f"{k}={self._fmt_param(unit, k, v)}" for k, v in eff.params.items()
            )
            state = "active" if eff.is_active() else "off"
            print(f"fx {unit} group={eff.group} [{state}] {params}")

    def _patch(self, profile: str, label: str, rest: list[str]) -> None:
        """patch <profile> <label> <n> [thru <m>] @ <base> [step <stride>]"""
        prof = self.profiles.get(profile)
        if prof is None:
            raise ValueError(f"unknown fixture profile {profile!r}")
        i = 0
        n1 = int(rest[i])
        n2 = n1
        i += 1
        if i < len(rest) and rest[i] == "thru":
            n2 = int(rest[i + 1])
            i += 2
        if i >= len(rest) or rest[i] not in ("@", "at"):
            raise ValueError("patch needs '@ <base address>'")
        base = int(rest[i + 1]) - 1  # 1-based DMX address -> 0-based channel
        i += 2
        stride = prof.footprint
        if i < len(rest) and rest[i] == "step":
            stride = int(rest[i + 1])
            i += 2
        for k, number in enumerate(range(n1, n2 + 1)):
            fx = PatchedFixture(
                kind=profile,
                label=label,
                number=number,
                base=base + k * stride,
                profile=prof,
            )
            self.fixtures[(label, number)] = fx
            # seed the engine base layer with this fixture's channel defaults
            for attr, value in prof.defaults.items():
                offset = prof.offset(attr)
                if offset is not None:
                    self.engine.base[fx.base + offset] = value
        self._labels.add(label)
        self._rebind_effects()  # new fixtures may join an effect's group

    def _apply_cc(self, cc: int, value: int) -> None:
        # called synchronously from MidiCC.poll(); reads bindings dynamically so
        # rebinding/unbinding a cc takes effect without re-registering listeners
        tgt = self.bindings.get(cc)
        if tgt is None:
            return
        if tgt[0] == "sub":
            idx = tgt[1]
            if 0 <= idx < len(self.engine.subs):
                self.engine.subs[idx].intensity = value / 127.0
        elif tgt[0] == "fx":
            _, unit, param = tgt
            eff = self.engine.effects.get(unit)
            if eff is not None and param in eff.params:
                eff.params[param] = value / 127.0  # continuous params only

    def _cc_listener(self, cc: int) -> Callable[[int], None]:
        return lambda value: self._apply_cc(cc, value)

    def _wire_cc(self, cc: int) -> None:
        # one listener per cc (dispatches through _apply_cc); snap to current knob
        if self.midi is None:
            return
        if cc not in self._wired_cc:
            self.midi.bind_cc(cc, self._cc_listener(cc))
            self._wired_cc.add(cc)
        if cc in self.midi.cc_last:
            self._apply_cc(cc, self.midi.cc_last[cc])

    def _wire_note(self, note: int) -> None:
        if self.midi is not None and note not in self._wired_notes:
            self.midi.bind_note(note, self._note_listener(note))
            self._wired_notes.add(note)

    def _note_event(self, note: int, pressed: bool) -> None:
        # called from MidiCC.poll(); reads note_bindings dynamically so
        # re/unbinding takes effect without re-registering listeners
        tgt = self.note_bindings.get(note)
        if tgt is None:
            return
        if tgt[0] == "flash":
            idx = tgt[1]
            if not (0 <= idx < len(self.engine.subs)):
                return
            if pressed:
                self._flash_saved[note] = self.engine.subs[idx].intensity
                self.engine.subs[idx].intensity = 1.0
            elif note in self._flash_saved:
                self.engine.subs[idx].intensity = self._flash_saved.pop(note)
        elif tgt[0] == "fx" and pressed:
            # latch: set the effect param to the bound value and leave it
            _, unit, param, value = tgt
            try:
                self._set_fx_param(unit, param, value)
            except (ValueError, KeyError):
                pass

    def _note_listener(self, note: int) -> Callable[[bool], None]:
        return lambda pressed: self._note_event(note, pressed)

    @staticmethod
    def _compact_refs(refs: list[tuple[str, int]]) -> str:
        # collapse consecutive same-label numbers into `wash 1 thru 6`
        parts: list[str] = []
        i = 0
        while i < len(refs):
            label, n = refs[i]
            j = i
            while j + 1 < len(refs) and refs[j + 1] == (label, refs[j][1] + 1):
                j += 1
            if j > i:
                parts.append(f"{label} {n} thru {refs[j][1]}")
            else:
                parts.append(f"{label} {n}")
            i = j + 1
        return " ".join(parts)

    def serialise(self) -> list[str]:
        """Render the persistent show state as replayable console commands."""
        lines = ["# aioartnet console show file"]
        # fixtures and groups first, so later selectors resolve
        for fx in self.fixtures.values():
            lines.append(f"patch {fx.kind} {fx.label} {fx.number} @ {fx.base + 1}")
        for name, refs in self.groups.items():
            lines.append(f"group {name} = {self._compact_refs(refs)}")
        # effects after groups (selectors must resolve); then per-fixture homes
        for unit, eff in self.engine.effects.items():
            if eff.group is not None:
                lines.append(f"fx {unit} group {eff.group}")
            for p, v in eff.params.items():
                if v != EFFECT_PARAMS[unit][p][1]:  # non-default only
                    lines.append(f"fx {unit} {p} {self._fmt_param(unit, p, v)}")
        for (label, number), (pan, tilt) in self.homes.items():
            lines.append(f"fx pt home {label} {number} {pan} {tilt}")
        # submasters next, so later `sub N at`/`midi bind` lines resolve
        for i, sub in enumerate(self.engine.subs):
            for ci in sub.channels:
                lines.append(f"chan {ci.channel + 1} at 0x{ci.intensity:02X}")
            lines.append(f"record sub {i + 1}")
            if sub.intensity > 0:
                lines.append(f"sub {i + 1} at 0x{round(sub.intensity * 255):02X}")
        for i, cue in enumerate(self.engine.cues):
            for ci in cue.channels:
                lines.append(f"chan {ci.channel + 1} at 0x{ci.intensity:02X}")
            lines.append(
                f"record cue {i + 1} fade_in {int(cue.fade_in)} "
                f"fade_out {int(cue.fade_out)} hold {int(cue.hold)}"
            )
        for cc, tgt in sorted(self.bindings.items(), key=lambda kv: kv[0]):
            if tgt[0] == "sub":
                lines.append(f"midi bind cc {cc} sub {tgt[1] + 1}")
            else:
                lines.append(f"midi bind cc {cc} fx {tgt[1]} {tgt[2]}")
        for note, tgt in sorted(self.note_bindings.items(), key=lambda kv: kv[0]):
            if tgt[0] == "flash":
                lines.append(f"midi bind note {note} flash sub {tgt[1] + 1}")
            else:
                lines.append(
                    f"midi bind note {note} fx {tgt[1]} {tgt[2]} {tgt[3]}"
                )
        # display preference (only when non-default, to keep the file clean)
        if self.grid is not None and (self.grid.trim or self.grid.mode != "hex"):
            lines.append(f"view {self.grid.mode} {'trim' if self.grid.trim else 'all'}")
        return lines

    async def on_cmd(self, cmd: str) -> str:
        match cmd.lower().split():
            case ["live", ("on" | "off") as mode]:
                self.engine.live_edit = mode == "on"
            case ["loop", ("on" | "off") as mode]:
                self.engine.loop = mode == "on"
            case ["chan" | "ch", chan, ("at" | "@"), level]:
                intensity = parse_intensity(level)
                self.engine.add_edit(
                    ChannelIntensity(channel=int(chan) - 1, intensity=intensity)
                )
            case ["chan" | "ch", chan_from, "thru", chan_to, "at", level]:
                intensity = parse_intensity(level)
                for i in range(int(chan_from), int(chan_to)):
                    self.engine.add_edit(
                        ChannelIntensity(channel=i - 1, intensity=intensity)
                    )
            case ["sub" | "submaster", chan, ("at" | "@"), level]:
                intensity = parse_intensity(level)
                cn = parse_user_index(chan, self.engine.subs, extend=False)
                # submaster intensity is a 0.0-1.0 fader scaling recorded levels
                self.engine.subs[cn].intensity = intensity / 255.0
            case ["midi", "bind", "cc", ccnum, "sub", subnum]:
                cc = int(ccnum)
                idx = parse_user_index(subnum, self.engine.subs, extend=False)
                self.bindings[cc] = ("sub", idx)
                self._wire_cc(cc)
            case ["midi", "bind", "cc", ccnum, "fx", unit, param]:
                cc = int(ccnum)
                self._validate_fx_param(unit, param, allow_enum=False)
                self.bindings[cc] = ("fx", unit, param)
                self._wire_cc(cc)
            case ["midi", "bind", "cc", ccnum]:
                # target omitted -> unbind (listener stays but no-ops)
                self.bindings.pop(int(ccnum), None)
            case ["midi", "bind", "note", notenum, "flash", "sub", subnum]:
                note = int(notenum)
                idx = parse_user_index(subnum, self.engine.subs, extend=False)
                self.note_bindings[note] = ("flash", idx)
                self._wire_note(note)
            case ["midi", "bind", "note", notenum, "fx", unit, param, value]:
                note = int(notenum)
                self._validate_fx_param(unit, param)
                self.note_bindings[note] = ("fx", unit, param, value)
                self._wire_note(note)
            case ["midi", "bind", "note", notenum]:
                # action omitted -> unbind (listener stays but no-ops)
                self.note_bindings.pop(int(notenum), None)
            case ["patch", profile, label, *rest]:
                self._patch(profile, label, rest)
            case ["group", name, *sel]:
                if sel and sel[0] == "=":
                    sel = sel[1:]
                fixtures, remainder = self._take_selector(sel)
                if remainder:
                    raise ValueError(f"unknown fixtures in group: {remainder}")
                self.groups[name] = [(fx.label, fx.number) for fx in fixtures]
                self._rebind_effects()  # group membership may have changed
            case ["fix" | "fixture"]:
                if self.fixtures:
                    for fx in self.fixtures.values():
                        print(f"{fx.label} {fx.number} ({fx.kind}) @ {fx.base + 1}")
                else:
                    print("No fixtures patched")
            case ["fix" | "fixture", *rest]:
                fixtures, attrs = self._take_selector(rest)
                if not fixtures:
                    raise ValueError("no fixtures selected")
                if not attrs:
                    raise ValueError("nothing to set")
                self._apply_fix(fixtures, attrs)
            case ["fx"]:
                self._list_effects()
            case ["fx", unit, "group", grp]:
                if grp not in self.groups:
                    raise ValueError(f"unknown group {grp!r}")
                self._ensure_effect(unit).group = grp
                self._rebind_effects()
            case ["fx", unit, "off"]:
                self._set_fx_param(unit, "intensity", "0")
            case ["fx", "pt", "home"]:
                self._capture_pt_home()
            case ["fx", "pt", "home", pan, tilt]:
                self._set_pt_home_all(int(pan), int(tilt))
            case ["fx", "pt", "home", label, num, pan, tilt]:
                self.homes[(label, int(num))] = (int(pan), int(tilt))
                self._rebind_effects()
            case ["fx", unit, param, value]:
                self._set_fx_param(unit, param, value)
            case ["record", ("cue" | "sub") as target, cue_num, *args]:
                # record cue 1 time 3
                fade_in = 0
                fade_out = 0
                hold = 1
                while args:
                    match args:
                        case ["fade", duration, *more]:
                            fade_in = parse_duration(duration)
                            fade_out = fade_in
                        case ["fade_in", duration, *more]:
                            fade_in = parse_duration(duration)
                        case ["fade_out", duration, *more]:
                            fade_out = parse_duration(duration)
                        case ["hold", duration, *more]:
                            hold = parse_duration(duration)
                        case _:
                            raise ValueError(f"unknown record args {args}")
                    args = more

                if target == "cue":
                    cn = parse_user_index(cue_num, self.engine.cues, extend=True)
                    cue = Cue(
                        name="",
                        fade_in=fade_in,
                        hold=hold,
                        fade_out=fade_out,
                        channels=list(self.engine.edits),
                    )
                    print(f"inserting {cue} as cue {cn}")
                    if cn == len(self.engine.cues):
                        self.engine.cues.append(cue)
                    else:
                        self.engine.cues[cn] = cue
                    await self.engine.clear_edits()
                else:
                    cn = parse_user_index(cue_num, self.engine.subs, extend=True)
                    # recorded submasters start with their fader down (0.0);
                    # bring them up with `sub N at LEVEL`
                    sub = Submaster(
                        intensity=0.0,
                        name="",
                        channels=list(self.engine.edits),
                    )
                    print(f"inserting {sub} as sub {cn}")
                    if cn == len(self.engine.subs):
                        self.engine.subs.append(sub)
                    else:
                        self.engine.subs[cn] = sub
                    await self.engine.clear_edits()
            # update cue 1
            # update
            case ["edits" | "edit" | "dirty"]:
                print(self.engine.edits)
            case ["go"]:
                await self.engine.go_relative(1)
            case ["back"]:
                await self.engine.go_relative(-1)
            case ["go", cue_num]:
                cn = int(cue_num) - 1
                await self.engine.go_absolute(cn)
            case ["cue", cue_num, "go"]:
                raise ValueError("NYI")
            case ["clear"]:
                await self.engine.clear_edits()
            case ["list"]:
                if self.engine.cues:
                    for idx, cue in enumerate(self.engine.cues):
                        print(f"cue {idx + 1:03} {cue}")
                else:
                    print("No cues")
                if self.engine.subs:
                    for idx, sub in enumerate(self.engine.subs):
                        print(f"sub {idx + 1:03} {sub}")
                else:
                    print("No submasters")
                for fx in self.fixtures.values():
                    print(
                        f"fixture {fx.label} {fx.number} "
                        f"({fx.kind}) @ {fx.base + 1}"
                    )
                for name, refs in self.groups.items():
                    print(f"group {name} = {self._compact_refs(refs)}")
                self._list_effects()
                for cc, tgt in sorted(self.bindings.items(), key=lambda kv: kv[0]):
                    dst = (
                        f"sub {tgt[1] + 1}"
                        if tgt[0] == "sub"
                        else f"fx {tgt[1]} {tgt[2]}"
                    )
                    print(f"bind cc {cc} -> {dst}")
                for note, tgt in sorted(
                    self.note_bindings.items(), key=lambda kv: kv[0]
                ):
                    dst = (
                        f"flash sub {tgt[1] + 1}"
                        if tgt[0] == "flash"
                        else f"fx {tgt[1]} {tgt[2]} {tgt[3]}"
                    )
                    print(f"bind note {note} -> {dst}")
            case ["save"] | ["save", _]:
                # match lowercased the line, so recover the path from the
                # original cmd to keep case-sensitive paths intact
                parts = cmd.split()
                path = parts[1] if len(parts) > 1 else self.showfile_path
                if not path:
                    raise ValueError(
                        "no show file: use 'save <path>' or start with --file"
                    )
                with open(os.path.expanduser(path), "w") as f:
                    f.write("\n".join(self.serialise()) + "\n")
                if len(parts) > 1:
                    self.showfile_path = path
                print(f"saved to {path}")
            case ["tickhz", hz]:
                self.engine.tickhz = int(hz)
            case ["view", *opts]:
                self._set_view(opts)
            case ["help" | "h" | "?"]:
                print(HELP_TEXT)
            case _:
                raise ValueError(f"Unknown command: {cmd}")

        await self.engine.poll(0)

        return ""

    async def load_commands(self, filename: str) -> None:
        with open(os.path.expanduser(filename)) as file:
            for lineno, raw in enumerate(file, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    await self.on_cmd(line)
                except Exception:
                    print(f"{filename}:{lineno}: {traceback.format_exc(limit=-2)}")


class _LogWriter:
    """File-like sink that turns print() output into log-pane lines."""

    def __init__(self, append: Callable[[str], None]) -> None:
        self._append = append
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._append(line)
        return len(s)

    def flush(self) -> None:
        pass


async def main(
    client: ArtNetClient,
    engine: Engine,
    interpreter: Interpreter,
    dmx_source: Callable[[], bytes],
    label: str = "",
    midi: Optional[MidiCC] = None,
) -> None:
    grid = DmxGrid(dmx_source, lambda: bytes(engine.last_source), label)
    grid.trim_source = interpreter._patched_extent
    interpreter.grid = grid
    midi_view = MidiView(midi)
    fx_view = FxView(engine)

    history = FileHistory(os.path.expanduser("~/.aioartnet-console-history"))

    # scrollback pane for command output and Art-Net events
    log_buffer = Buffer(read_only=True)
    LOG_MAX_LINES = 1000

    def append_log(line: str) -> None:
        lines = log_buffer.text.split("\n") if log_buffer.text else []
        lines.append(line)
        if len(lines) > LOG_MAX_LINES:
            lines = lines[-LOG_MAX_LINES:]
        text = "\n".join(lines)
        log_buffer.set_document(
            Document(text, cursor_position=len(text)), bypass_readonly=True
        )
        if app.is_running:
            app.invalidate()

    async def run_cmd(text: str) -> None:
        text = text.strip()
        if not text:
            return
        append_log(f"> {text}")
        try:
            output = await interpreter.on_cmd(text)
            if output:
                append_log(output)
        except Exception:
            append_log(traceback.format_exc(limit=-2))

    def accept(buff: Buffer) -> bool:
        asyncio.get_running_loop().create_task(run_cmd(buff.text))
        return False  # clear the input line

    input_buffer = Buffer(history=history, multiline=False, accept_handler=accept)

    root = HSplit(
        [
            Window(
                content=FormattedTextControl(grid.render),
                # dynamic: shrinks when the grid is trimmed to patched channels
                height=lambda: grid.height,
                style="class:grid",
            ),
            Window(height=1, char="─", style="class:sep"),
            Window(
                content=FormattedTextControl(midi_view.render),
                height=MidiView.HEIGHT,
                wrap_lines=True,
                style="class:midi",
            ),
            Window(height=1, char="─", style="class:sep"),
            Window(
                content=FormattedTextControl(fx_view.render),
                height=FxView.HEIGHT,
                wrap_lines=True,
                style="class:fx",
            ),
            Window(height=1, char="─", style="class:sep"),
            Window(
                content=BufferControl(buffer=log_buffer, focusable=False),
                # wrap long echoes instead of scrolling the pane horizontally
                wrap_lines=True,
            ),
            Window(height=1, char="─", style="class:sep"),
            VSplit(
                [
                    Window(FormattedTextControl("> "), width=2, style="bold"),
                    Window(content=BufferControl(buffer=input_buffer), height=1),
                ]
            ),
        ]
    )

    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("c-d")
    def _exit(event: Any) -> None:
        event.app.exit()

    style = Style.from_dict({"dim": "#808080", "sep": "#444444"})

    app: Application[None] = Application(
        layout=Layout(root, focused_element=input_buffer),
        key_bindings=kb,
        style=style,
        full_screen=True,
        # auto-refresh at ~20 FPS; render() dirty-skips when DMX is unchanged
        refresh_interval=0.05,
    )

    async def print_events() -> None:
        async for event in client.events():
            print(event)

    await client.connect()
    await engine.start_ticking()

    old_stdout = sys.stdout
    sys.stdout = _LogWriter(append_log)  # type: ignore[assignment]
    tasks = [asyncio.create_task(print_events())]
    if midi is not None:
        tasks.append(asyncio.create_task(midi.run()))
    try:
        if interpreter.showfile_path:
            await interpreter.load_commands(interpreter.showfile_path)
        await app.run_async()
    finally:
        for t in tasks:
            t.cancel()
        sys.stdout = old_stdout


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="aioartnet console",
        description="Replica vintage console",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-i", "--interface")
    parser.add_argument("-n", "--portName")
    parser.add_argument(
        "-u", "--universe", help="Art-Net universe to output", default="0:0:0"
    )
    parser.add_argument("-f", "--file", help="read commands from file")
    parser.add_argument(
        "-m",
        "--midi-in",
        nargs="?",
        const="",
        metavar="PORT",
        help="enable MIDI input; optional substring of the port name to open",
    )
    args = parser.parse_args()

    level = {False: logging.INFO, True: logging.DEBUG}[args.verbose]
    logging.basicConfig(level=level)

    # setup art-net input universe (for our output)
    kwargs = {}
    if args.interface:
        kwargs["interface"] = args.interface
    if args.portName:
        kwargs["portName"] = args.portName
    client = ArtNetClient(**kwargs)
    u1 = client.set_port_config(args.universe, is_input=True)

    # setup console engine/interpreter wired to our universe
    engine = Engine(u1.set_dmx)

    config = load_config()
    midi = setup_midi_from_config(config, args.midi_in)
    profiles = load_profiles(config)

    interpreter = Interpreter(engine, midi=midi, profiles=profiles)
    interpreter.showfile_path = args.file  # may be None

    asyncio.run(
        main(client, engine, interpreter, u1.get_dmx, args.universe, midi=midi)
    )
