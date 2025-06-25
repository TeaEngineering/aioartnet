import argparse
import asyncio
import logging
import os
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Sequence

from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession

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


def apply_ci(data: bytearray, edits: Sequence[ChannelIntensity], scale: float) -> None:
    for e in edits:
        data[e.channel] = max(0, min(255, int(e.intensity * scale)))


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

        # mix in any non-zero submasters

        # add live edits
        if self.live_edit:
            apply_ci(live, self.edits, 1.0)

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


# interprets simple commands and formats the responses
class Interpreter:
    def __init__(self, engine: Engine):
        self.engine = engine

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
                parse_user_index(chan, self.engine.subs, extend=False)
                # self.engine.subs[]
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
                    pass
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
            case _:
                raise ValueError(f"Unknown command: {cmd}")

        await self.engine.poll(0)

        return ""

    async def load_commands(self, filename: str) -> None:
        with open(filename, "r") as file:
            for line in file.read():
                await self.on_cmd(line.strip())


async def main(client: ArtNetClient, engine: Engine, interpreter: Interpreter) -> None:
    history = FileHistory(os.path.expanduser("~/.aioartnet-console-history"))
    session: PromptSession[str] = PromptSession("> ", history=history)

    await client.connect()
    await engine.start_ticking()

    # Run echo loop. Read text from stdin, and reply it back.
    while True:
        try:
            inp = await session.prompt_async("> ")
            if inp == "":
                continue
            output = await interpreter.on_cmd(inp)
            if output is None or output == "":
                continue
            print(output)

        except KeyboardInterrupt:
            return
        except EOFError:
            break
        except Exception:
            traceback.print_exc(limit=-2)
    return None


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

    # TODO: needs await as cmds are async?
    # if args.file:
    #    interpreter.load_commands(args.file)

    with patch_stdout():
        asyncio.run(main(client, engine, interpreter))
