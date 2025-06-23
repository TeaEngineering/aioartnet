import argparse
import asyncio
import logging
import os
import traceback
from dataclasses import dataclass, field
from typing import Callable

from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession

from . import DMX_UNIVERSE_SIZE, ArtNetClient, ArtNetUniverse


@dataclass
class ChannelIntensity:
    channel: int
    intensity: int


@dataclass
class Cue:
    name: str
    fade: float
    hold: float
    channels: list[ChannelIntensity] = field(default_factory=list)


@dataclass
class Submaster:
    pc: float
    name: str
    channels: list[ChannelIntensity] = field(default_factory=list)


class Engine:
    def __init__(self, handler: Callable[[bytes], None]):
        self.cues: list[Cue] = []
        self.active_cue = 0
        self.subs: list[Submaster] = []
        self.last_poll: float = 0
        self.live = bytearray(DMX_UNIVERSE_SIZE)
        self.handler = handler

    async def poll(self, time: float) -> None:
        # print(f'calling handler {self.handler} with {self.live}')
        self.handler(self.live)

    async def go(self, cuenum: int | None = None) -> None:
        cc = self.cues[self.active_cue]
        print(cc)


def parse_intensity(level: str) -> int:
    if level.upper() in ("FL", "FULL"):
        return 255
    if level.upper().startswith("0x"):
        return min(255, int(level[2:], base=16))
    else:
        return min(255, int((int(level) / 100.0) * 256))


class Interpreter:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.edits: list[ChannelIntensity] = []
        self.editing = bytearray(DMX_UNIVERSE_SIZE)
        self.live_edit = False

    async def on_cmd(self, cmd: str) -> str:
        match cmd.lower().split():
            case ["live", ("on" | "off") as mode]:
                self.live_edit = mode == "on"
            case ["chan", chan, "at", level]:
                intensity = parse_intensity(level)
                self.editing[int(chan) - 1] = intensity
                self.edits.append(
                    ChannelIntensity(channel=int(chan) - 1, intensity=intensity)
                )
            case ["chan", chan_from, "thru", chan_to, "at", level]:
                intensity = parse_intensity(level)
                for i in range(int(chan_from), int(chan_to)):
                    self.editing[i - 1] = intensity
                    self.edits.append(
                        ChannelIntensity(channel=i - 1, intensity=intensity)
                    )
            case ["record", "cue", cue_num]:
                cn = int(cue_num) - 1
                if cn < 0:
                    raise ValueError("cue numbering starts from 1")
                if cn > len(self.engine.cues) + 1:
                    raise ValueError(f"highest existing cue is {len(self.engine.cues)}")
                cue = Cue(name="", fade=0, hold=0, channels=self.edits)
                print(f"inserting {cue} at {cn}")
                if cn == len(self.engine.cues):
                    self.engine.cues.append(cue)
                else:
                    self.engine.cues[cn] = cue
                self.edits.clear()
            case _:
                raise ValueError(f"Unknown command: {cmd}")

        if self.live_edit:
            self.engine.live[:] = self.editing[:]
            await self.engine.poll(0)

        return ""


async def main(client: ArtNetClient, u1: ArtNetUniverse) -> None:
    history = FileHistory(os.path.expanduser("~/.aioartnet-console-history"))
    session: PromptSession[str] = PromptSession("> ", history=history)

    engine = Engine(u1.set_dmx)
    interpreter = Interpreter(engine)
    await client.connect()

    # Run echo loop. Read text from stdin, and reply it back.
    while True:
        try:
            inp = await session.prompt_async(">")
            if inp == "":
                continue
            output = await interpreter.on_cmd(inp)
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
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-i", "--interface")
    parser.add_argument("-n", "--portName")
    parser.add_argument("-u", "--universe", default="0:0:0")

    args = parser.parse_args()

    level = {False: logging.INFO, True: logging.DEBUG}[args.verbose]
    logging.basicConfig(level=level)

    kwargs = {}
    if args.interface:
        kwargs["interface"] = args.interface
    if args.portName:
        kwargs["portName"] = args.portName
    client = ArtNetClient(**kwargs)
    u1 = client.set_port_config(args.universe, is_input=True)
    u1.set_dmx(bytes(512))

    with patch_stdout():
        asyncio.run(main(client, u1))
