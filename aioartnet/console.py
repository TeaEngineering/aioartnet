import argparse
import asyncio
import json
import logging
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
        self.live = bytearray(universe_size)
        # per-channel SRC_* provenance for the most recent poll
        self.last_source = bytearray(universe_size)
        self.handler = handler
        self.loop = False
        self.edits: list[ChannelIntensity] = []
        self.live_edit = False
        self.tickhz = 10
        self._active_cues: list[ActiveCue] = []

    async def poll(self, time: float) -> None:
        # advance time
        self.last_poll = max(self.last_poll, time)

        live = bytearray(self.universe_size)
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

        # add live edits
        if self.live_edit:
            apply_ci(live, self.edits, 1.0)

        # classify the provenance of each channel, mirroring the mix order
        # above: cues/subs are "stored", live edits override as "live"
        source = bytearray(self.universe_size)
        for ac in self._active_cues:
            for e in ac.cue.channels:
                source[e.channel] = SRC_STORED
        for sub in self.subs:
            if sub.intensity > 0:
                for e in sub.channels:
                    source[e.channel] = SRC_STORED
        if self.live_edit:
            for e in self.edits:
                source[e.channel] = SRC_LIVE
        self.last_source = source

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
                # note-on with velocity 0 is conventionally a note-off
                if message[2] == 0:
                    self.notes_on.pop(message[1], None)
                else:
                    self.notes_on[message[1]] = message[2]
            elif status == NOTE_OFF:
                self.notes_on.pop(message[1], None)
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
        self._last_data: bytes = b""
        self._last_src: bytes = b""
        self._cache_mode: Optional[str] = None
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
    def height(self) -> int:
        rows = (DMX_UNIVERSE_SIZE + self.COLS - 1) // self.COLS
        return rows + 2  # title line + column header

    def render(self) -> StyleAndTextTuples:
        data = self.source()
        src = self.source_map() if self.source_map is not None else b""
        if (
            bytes(data) == self._last_data
            and bytes(src) == self._last_src
            and self._cache_mode == self.mode
        ):
            return self._cache
        self._last_data = bytes(data)
        self._last_src = bytes(src)
        self._cache_mode = self.mode
        text = self._text[self.mode]

        frags: StyleAndTextTuples = [
            ("bold", f" DMX {self.label}  [{self.mode}]\n"),
            ("class:dim", "     "),
        ]
        for c in range(self.COLS):
            frags.append(("class:dim", f"{c + 1:>2} "))
        for ch in range(len(data)):
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


HELP_TEXT = """\
Available commands:
  live on|off                       enable/disable live edits in the output
  loop on|off                       enable/disable looping playback
  chan|ch N at|@ LEVEL              set channel N to LEVEL
  chan|ch A thru B at LEVEL         set channels A..B to LEVEL
  sub|submaster N at|@ LEVEL        set submaster N to LEVEL
  record cue|sub N [fade D]         record current edits as cue/sub N
            [fade_in D] [fade_out D] [hold D]
  edits|edit|dirty                  show the current uncommitted edits
  go                                advance to the next cue
  back                              return to the previous cue
  go N                              jump to cue N
  clear                             clear the current edits
  list                              list all cues and submasters
  tickhz N                          set the engine tick rate (Hz)
  view hex|pct                      switch the DMX grid value format
  help|h|?                          show this help

LEVEL may be a percentage (0-100), a hex value (0xFF), or
F/FULL, H/HALF, Z/ZERO."""


# interprets simple commands and formats the responses
class Interpreter:
    def __init__(self, engine: Engine, grid: Optional[DmxGrid] = None):
        self.engine = engine
        self.grid = grid

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
            case ["tickhz", hz]:
                self.engine.tickhz = int(hz)
            case ["view", ("hex" | "pct" | "percent" | "fl") as fmt]:
                if self.grid is not None:
                    self.grid.mode = "hex" if fmt == "hex" else "pct"
            case ["help" | "h" | "?"]:
                print(HELP_TEXT)
            case _:
                raise ValueError(f"Unknown command: {cmd}")

        await self.engine.poll(0)

        return ""

    async def load_commands(self, filename: str) -> None:
        with open(filename, "r") as file:
            for line in file.read():
                await self.on_cmd(line.strip())


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
    interpreter.grid = grid
    midi_view = MidiView(midi)

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
                height=grid.height,
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
                content=BufferControl(buffer=log_buffer, focusable=False),
                wrap_lines=False,
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
    interpreter = Interpreter(engine)

    config = load_config()
    midi = setup_midi_from_config(config, args.midi_in)

    # TODO: needs await as cmds are async?
    # if args.file:
    #    interpreter.load_commands(args.file)

    asyncio.run(
        main(client, engine, interpreter, u1.get_dmx, args.universe, midi=midi)
    )
