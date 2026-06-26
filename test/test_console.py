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


class _FakeMidiIn:
    def __init__(self, msgs: "list[list[int]] | None" = None) -> None:
        self.msgs = list(msgs or [])

    def feed(self, *msgs: list[int]) -> None:
        self.msgs.extend(msgs)

    def get_message(self) -> object:
        return (self.msgs.pop(0), 0.0) if self.msgs else None


@pytest.mark.asyncio
async def test_midi_bind_applies_cc_to_submaster() -> None:
    from aioartnet.console import CONTROLLER_CHANGE, MidiCC

    engine = Engine(Mock(), universe_size=20)
    fake = _FakeMidiIn()
    midi = MidiCC(fake)
    interpreter = Interpreter(engine, midi=midi)

    await interpreter.on_cmd("chan 1 at f")
    await interpreter.on_cmd("record sub 1")
    await interpreter.on_cmd("midi bind cc 70 sub 1")
    assert interpreter.bindings == {70: 0}

    fake.feed([CONTROLLER_CHANGE, 70, 127])
    midi.poll()
    assert engine.subs[0].intensity == 1.0

    fake.feed([CONTROLLER_CHANGE, 70, 64])
    midi.poll()
    assert engine.subs[0].intensity == 64 / 127.0


@pytest.mark.asyncio
async def test_midi_rebind_does_not_stack_listeners() -> None:
    from aioartnet.console import CONTROLLER_CHANGE, MidiCC

    engine = Engine(Mock(), universe_size=20)
    fake = _FakeMidiIn()
    midi = MidiCC(fake)
    interpreter = Interpreter(engine, midi=midi)

    await interpreter.on_cmd("chan 1 at f")
    await interpreter.on_cmd("record sub 1")
    await interpreter.on_cmd("chan 2 at f")
    await interpreter.on_cmd("record sub 2")

    await interpreter.on_cmd("midi bind cc 70 sub 1")
    await interpreter.on_cmd("midi bind cc 70 sub 2")
    assert len(midi.cc_listeners[70]) == 1
    assert interpreter.bindings == {70: 1}

    fake.feed([CONTROLLER_CHANGE, 70, 127])
    midi.poll()
    assert engine.subs[1].intensity == 1.0
    assert engine.subs[0].intensity == 0.0  # the old target is untouched


@pytest.mark.asyncio
async def test_midi_unbind() -> None:
    from aioartnet.console import CONTROLLER_CHANGE, MidiCC

    engine = Engine(Mock(), universe_size=20)
    fake = _FakeMidiIn()
    midi = MidiCC(fake)
    interpreter = Interpreter(engine, midi=midi)

    await interpreter.on_cmd("chan 1 at f")
    await interpreter.on_cmd("record sub 1")
    await interpreter.on_cmd("midi bind cc 70 sub 1")
    await interpreter.on_cmd("midi bind cc 70")  # unbind (target omitted)
    assert interpreter.bindings == {}

    fake.feed([CONTROLLER_CHANGE, 70, 127])
    midi.poll()
    assert engine.subs[0].intensity == 0.0  # no longer driven
    assert "midi bind cc 70" not in "\n".join(interpreter.serialise())


@pytest.mark.asyncio
async def test_midi_bind_without_device_records_binding() -> None:
    engine = Engine(Mock(), universe_size=20)
    interpreter = Interpreter(engine)  # no midi attached

    await interpreter.on_cmd("chan 1 at f")
    await interpreter.on_cmd("record sub 1")
    await interpreter.on_cmd("midi bind cc 70 sub 1")
    assert interpreter.bindings == {70: 0}


def test_apply_cc_out_of_range_is_noop() -> None:
    engine = Engine(Mock(), universe_size=20)
    interpreter = Interpreter(engine)
    interpreter.bindings = {70: 5}  # no such submaster
    interpreter._apply_cc(70, 127)  # must not raise


@pytest.mark.asyncio
async def test_show_save_load_round_trip() -> None:
    # build state in a source console: a sub (fader up), a cue with fades, a bind
    src = Interpreter(Engine(Mock(), universe_size=20))
    await src.on_cmd("chan 1 at f")
    await src.on_cmd("chan 5 at half")
    await src.on_cmd("record sub 1")
    await src.on_cmd("sub 1 at 50")
    await src.on_cmd("chan 2 at f")
    await src.on_cmd("record cue 1 fade_in 2 fade_out 3 hold 4")
    await src.on_cmd("midi bind cc 70 sub 1")

    lines = src.serialise()

    # replay into a fresh console
    dst = Interpreter(Engine(Mock(), universe_size=20))
    for line in lines:
        if line.startswith("#"):
            continue
        await dst.on_cmd(line)

    assert dst.engine.subs == src.engine.subs
    assert dst.engine.cues == src.engine.cues
    assert dst.bindings == src.bindings


@pytest.mark.asyncio
async def test_load_commands_skips_blanks_and_tolerates_errors(tmp_path) -> None:  # type: ignore[no-untyped-def]
    engine = Engine(Mock(), universe_size=20)
    interpreter = Interpreter(engine)

    show = tmp_path / "show.txt"
    show.write_text(
        "# a comment\n"
        "\n"
        "chan 1 at f\n"
        "record sub 1\n"
        "this is not a valid command\n"
        "chan 2 at f\n"
        "record sub 2\n"
    )
    await interpreter.load_commands(str(show))

    # the valid commands either side of the broken line both applied
    assert len(engine.subs) == 2
