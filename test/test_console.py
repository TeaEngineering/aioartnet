from unittest.mock import Mock

import pytest

from aioartnet.console import Engine, Interpreter


@pytest.mark.asyncio
async def test_console() -> None:
    set_dmx = Mock()
    engine = Engine(set_dmx, universe_size=20)
    interpreter = Interpreter(engine)

    exp = bytearray(20)
    await interpreter.on_cmd("live on")
    set_dmx.assert_called_with(exp)
    assert engine.cues == []

    await interpreter.on_cmd("chan 1 at 50")
    exp[0] = 128
    set_dmx.assert_called_with(exp)

    await interpreter.on_cmd("chan 10 thru 20 at 20")
    exp[9:19] = bytes([51] * 10)
    set_dmx.assert_called_with(exp)

    # saving clears the live edits
    await interpreter.on_cmd("RECORD CUE 1")
    assert len(engine.cues) == 1
    zeros = bytearray(20)
    set_dmx.assert_called_with(zeros)

    await interpreter.on_cmd("RECORD CUE 2 HOLD 4")
    await interpreter.on_cmd("RECORD CUE 3 HOLD 2 FADE 1")
    assert len(engine.cues) == 3

    # await interpreter.on_cmd("CUE 3 LABEL \"Scene 1 blackout\"")
    assert engine.active_cue is None
    await interpreter.on_cmd("GO")
    assert engine.active_cue == 0

    set_dmx.assert_called_with(exp)

    # await interpreter.on_cmd("STATE")
    # await interpreter.on_cmd("LIST")


@pytest.mark.asyncio
async def test_console_submaster() -> None:
    set_dmx = Mock()
    engine = Engine(set_dmx, universe_size=20)
    interpreter = Interpreter(engine)

    # record a submaster from live edits; recording leaves its fader at 0
    await interpreter.on_cmd("chan 1 at f")
    await interpreter.on_cmd("chan 2 at 50")
    await interpreter.on_cmd("record sub 1")
    assert len(engine.subs) == 1
    assert engine.subs[0].intensity == 0.0
    set_dmx.assert_called_with(bytearray(20))

    # bringing the fader to full outputs the recorded levels
    await interpreter.on_cmd("sub 1 at f")
    exp = bytearray(20)
    exp[0] = 255
    exp[1] = 128
    set_dmx.assert_called_with(exp)

    # half fader scales the recorded levels
    await interpreter.on_cmd("sub 1 @ 50")
    exp = bytearray(20)
    exp[0] = 128
    exp[1] = 64
    set_dmx.assert_called_with(exp)

    # taking the fader to zero removes its contribution
    await interpreter.on_cmd("sub 1 at z")
    set_dmx.assert_called_with(bytearray(20))


@pytest.mark.asyncio
async def test_console_submaster_htp() -> None:
    set_dmx = Mock()
    engine = Engine(set_dmx, universe_size=20)
    interpreter = Interpreter(engine)

    # two submasters overlapping on channel 1: brighter wins (HTP)
    await interpreter.on_cmd("chan 1 at 30")
    await interpreter.on_cmd("record sub 1")
    await interpreter.on_cmd("chan 1 at 80")
    await interpreter.on_cmd("record sub 2")

    await interpreter.on_cmd("sub 1 at f")
    await interpreter.on_cmd("sub 2 at f")
    exp = bytearray(20)
    exp[0] = 204  # int(0.8 * 256) clamped, the brighter of the two subs
    set_dmx.assert_called_with(exp)


@pytest.mark.asyncio
async def test_console_channel_provenance() -> None:
    from aioartnet.console import SRC_LIVE, SRC_STORED, SRC_UNDRIVEN

    set_dmx = Mock()
    engine = Engine(set_dmx, universe_size=20)
    interpreter = Interpreter(engine)

    # ch1 held by an active cue -> stored
    await interpreter.on_cmd("chan 1 at f")
    await interpreter.on_cmd("record cue 1")
    await interpreter.on_cmd("go")
    # ch5 overridden by a live edit -> live
    await interpreter.on_cmd("live on")
    await interpreter.on_cmd("chan 5 at half")

    src = engine.last_source
    assert src[0] == SRC_STORED
    assert src[4] == SRC_LIVE
    assert src[8] == SRC_UNDRIVEN

    # turning live edits off drops the live provenance back to undriven
    await interpreter.on_cmd("live off")
    assert engine.last_source[4] == SRC_UNDRIVEN


def test_midi_cc_tracking() -> None:
    from aioartnet.console import CONTROLLER_CHANGE, NOTE_OFF, NOTE_ON, MidiCC

    class FakeMidiIn:
        def __init__(self, msgs: list[list[int]]) -> None:
            self.msgs = list(msgs)

        def get_message(self) -> object:
            return (self.msgs.pop(0), 0.0) if self.msgs else None

    fake = FakeMidiIn(
        [
            [CONTROLLER_CHANGE, 70, 100],
            [CONTROLLER_CHANGE, 74, 20],
            [NOTE_ON, 60, 99],
            [NOTE_ON, 64, 80],
        ]
    )
    seen: list[int] = []
    midi = MidiCC(fake)
    midi.bind_cc(70, seen.append)
    midi.poll()

    # held keys are tracked, last CC values recorded, listener fired
    assert midi.notes_on == {60: 99, 64: 80}
    assert midi.cc_last == {70: 100, 74: 20}
    assert seen == [100]

    # note-off and note-on-with-velocity-0 both release the key
    fake.msgs = [[NOTE_OFF, 60, 0], [NOTE_ON, 64, 0]]
    midi.poll()
    assert midi.notes_on == {}
    # cc_last persists every channel ever seen
    assert midi.cc_last == {70: 100, 74: 20}


@pytest.mark.asyncio
async def test_console_loop() -> None:
    set_dmx = Mock()
    engine = Engine(set_dmx, universe_size=20)
    interpreter = Interpreter(engine)
    cmds = "ch 1 at f,record cue 1,ch 2 at f,record cue 2,ch 3 at f,record cue 3,go,go,go,go"
    for cmd in cmds.split(","):
        await interpreter.on_cmd(cmd)
    assert engine.active_cue == 0
