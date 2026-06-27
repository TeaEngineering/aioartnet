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
    midi.bind_cc((0, 70), seen.append)
    midi.poll()

    # held keys are tracked, last CC values recorded, listener fired
    assert midi.notes_on == {(0, 60): 99, (0, 64): 80}
    assert midi.cc_last == {(0, 70): 100, (0, 74): 20}
    assert seen == [100]

    # note-off and note-on-with-velocity-0 both release the key
    fake.msgs = [[NOTE_OFF, 60, 0], [NOTE_ON, 64, 0]]
    midi.poll()
    assert midi.notes_on == {}
    # cc_last persists every channel ever seen
    assert midi.cc_last == {(0, 70): 100, (0, 74): 20}


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
    assert interpreter.bindings == {(0, 70): ("sub", 0)}

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
    assert len(midi.cc_listeners[(0, 70)]) == 1
    assert interpreter.bindings == {(0, 70): ("sub", 1)}

    fake.feed([CONTROLLER_CHANGE, 70, 127])
    midi.poll()
    assert engine.subs[1].intensity == 1.0
    assert engine.subs[0].intensity == 0.0  # the old target is untouched


@pytest.mark.asyncio
async def test_midi_note_flashes_submaster() -> None:
    from aioartnet.console import NOTE_OFF, NOTE_ON, MidiCC

    engine = Engine(Mock(), universe_size=20)
    fake = _FakeMidiIn()
    midi = MidiCC(fake)
    it = Interpreter(engine, midi=midi)

    await it.on_cmd("chan 1 at f")
    await it.on_cmd("record sub 1")  # sub 1 fader starts at 0.0
    await it.on_cmd("midi bind note 36 flash sub 1")
    assert it.note_bindings == {(0, 36): ("flash", 0)}

    # press -> full while held
    fake.feed([NOTE_ON, 36, 100])
    midi.poll()
    assert engine.subs[0].intensity == 1.0

    # a repeated note-on (retrigger) must not corrupt the saved value
    fake.feed([NOTE_ON, 36, 110])
    midi.poll()
    assert engine.subs[0].intensity == 1.0

    # release -> restored to the pre-flash fader value
    fake.feed([NOTE_OFF, 36, 0])
    midi.poll()
    assert engine.subs[0].intensity == 0.0

    # round-trips through the show file, and unbinds
    assert "midi bind note 0:36 flash sub 1" in it.serialise()
    await it.on_cmd("midi bind note 36")
    assert it.note_bindings == {}


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
    assert interpreter.bindings == {(0, 70): ("sub", 0)}


def test_apply_cc_out_of_range_is_noop() -> None:
    engine = Engine(Mock(), universe_size=20)
    interpreter = Interpreter(engine)
    interpreter.bindings = {(0, 70): ("sub", 5)}  # no such submaster
    interpreter._apply_cc((0, 70), 127)  # must not raise


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


def _fixture_profiles():  # type: ignore[no-untyped-def]
    from aioartnet.console import load_profiles

    return load_profiles(
        {
            "fixtures": {
                "rgba_wash": ["dimmer", "red", "green", "blue", "amber"],
                "star_wash_bl": [
                    "pan",
                    "tilt",
                    "dimmer",
                    "red",
                    "green",
                    "blue",
                    "white",
                    {"gobo": {"open": 0, "stars": 50, "moon": 100}},
                ],
            }
        }
    )


def _edits(engine: Engine) -> dict:  # type: ignore[type-arg]
    return {ci.channel: ci.intensity for ci in engine.edits}


@pytest.mark.asyncio
async def test_fixture_patch_strides_by_footprint() -> None:
    engine = Engine(Mock(), universe_size=200)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch rgba_wash wash 1 thru 6 @ 1")  # footprint 5

    assert it.fixtures[("wash", 1)].base == 0  # DMX 1
    assert it.fixtures[("wash", 3)].base == 10  # DMX 11
    assert it.fixtures[("wash", 6)].base == 25  # DMX 26


@pytest.mark.asyncio
async def test_fixture_color_sets_rgb_only() -> None:
    engine = Engine(Mock(), universe_size=200)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch rgba_wash wash 1 @ 1")
    await it.on_cmd("fix wash 1 color #ff8800")

    e = _edits(engine)
    assert (e[1], e[2], e[3]) == (255, 136, 0)  # red, green, blue
    assert 4 not in e  # amber untouched
    assert 0 not in e  # dimmer untouched

    await it.on_cmd("fix wash 1 at full")
    assert _edits(engine)[0] == 255  # dimmer


@pytest.mark.asyncio
async def test_fixture_macro_and_literal() -> None:
    engine = Engine(Mock(), universe_size=200)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch star_wash_bl head 1 @ 1")  # gobo at offset 7 -> chan 7

    await it.on_cmd("fix head 1 gobo stars")
    assert _edits(engine)[7] == 50  # macro resolves to its raw byte

    await it.on_cmd("fix head 1 gobo 60")
    assert _edits(engine)[7] == 60  # bare number is a literal byte on raw channel


@pytest.mark.asyncio
async def test_fixture_group_and_mixed_selector() -> None:
    engine = Engine(Mock(), universe_size=200)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch rgba_wash wash 1 thru 2 @ 1")
    await it.on_cmd("patch star_wash_bl head 1 @ 20")
    await it.on_cmd("group rig = wash 1 thru 2 head 1")

    await it.on_cmd("fix rig color blue")
    e = _edits(engine)
    # wash 1 blue (chan 3) + head 1 blue (base 19 + offset 5 = 24)
    assert e[3] == 255
    assert e[24] == 255


@pytest.mark.asyncio
async def test_fixture_save_round_trip() -> None:
    profiles = _fixture_profiles()
    src = Interpreter(Engine(Mock(), universe_size=200), profiles=profiles)
    await src.on_cmd("patch rgba_wash wash 1 thru 6 @ 1")
    await src.on_cmd("patch star_wash_bl head 1 thru 4 @ 31")
    await src.on_cmd("group washes = wash 1 thru 6")

    dst = Interpreter(Engine(Mock(), universe_size=200), profiles=profiles)
    for line in src.serialise():
        if not line.startswith("#"):
            await dst.on_cmd(line)

    assert list(dst.fixtures.keys()) == list(src.fixtures.keys())
    assert dst.fixtures[("head", 2)].base == src.fixtures[("head", 2)].base
    assert dst.groups == src.groups


@pytest.mark.asyncio
async def test_fixture_default_holds_base_layer() -> None:
    from aioartnet.console import SRC_DEFAULT, SRC_LIVE, load_profiles

    profiles = load_profiles(
        {
            "fixtures": {
                # global_dimmer (offset 2) defaults to full
                "head": ["red", "green", {"global_dimmer": 255}],
            }
        }
    )
    out: dict[int, int] = {}
    engine = Engine(lambda d: out.update(enumerate(d)), universe_size=20)
    it = Interpreter(engine, profiles=profiles)
    await it.on_cmd("patch head h 1 thru 2 @ 1")  # footprint 3 -> base 0, 3

    await engine.poll(0)
    assert engine.base[2] == 255  # h1 global_dimmer
    assert engine.base[5] == 255  # h2 global_dimmer
    assert out[2] == 255  # held in the output beneath everything
    assert engine.last_source[2] == SRC_DEFAULT
    assert engine.last_source[0] == 0  # undriven (no default)

    # a live edit overrides the default and reads as a live channel
    await it.on_cmd("live on")
    await it.on_cmd("chan 3 at z")  # 1-based chan 3 == index 2
    assert out[2] == 0
    assert engine.last_source[2] == SRC_LIVE


def _captured(engine: Engine) -> bytearray:
    # the bytearray passed to the last handler call
    return engine.handler.call_args[0][0]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_fx_rgb_drives_group_with_provenance() -> None:
    from aioartnet.console import SRC_FX, SRC_LIVE

    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())
    # rgba_wash: dimmer,red,green,blue,amber -> red/green/blue at offsets 1,2,3
    await it.on_cmd("patch rgba_wash w 1 thru 2 @ 1")  # footprint 5 -> base 0,5
    await it.on_cmd("group ws = w 1 thru 2")
    await it.on_cmd("fx rgb group ws")
    await it.on_cmd("fx rgb style rainbow")
    await it.on_cmd("fx rgb spread 0")
    await it.on_cmd("fx rgb speed 0")
    await it.on_cmd("fx rgb intensity 100")
    await engine.poll(0.0)

    live = _captured(engine)
    assert (live[1], live[2], live[3]) == (255, 0, 0)  # rainbow phase 0 = red
    assert engine.last_source[1] == SRC_FX

    # intensity scales brightness
    await it.on_cmd("fx rgb intensity 50")
    await engine.poll(0.0)
    assert _captured(engine)[1] == 128  # round(255 * 0.5)

    # intensity 0 releases the channels (no longer SRC_FX)
    await it.on_cmd("fx rgb off")
    await engine.poll(0.0)
    assert engine.last_source[1] != SRC_FX

    # a live edit punches over a running effect
    await it.on_cmd("fx rgb intensity 100")
    await it.on_cmd("live on")
    await it.on_cmd("fix w 2 red 50")  # w2 base 5, red offset 1 -> chan 6
    await engine.poll(0.0)
    assert engine.last_source[6] == SRC_LIVE
    assert _captured(engine)[6] == 128
    assert engine.last_source[1] == SRC_FX  # w1 still effect-driven


@pytest.mark.asyncio
async def test_fx_rgb_spread_offsets_fixtures() -> None:
    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch rgba_wash w 1 thru 2 @ 1")
    await it.on_cmd("group ws = w 1 thru 2")
    await it.on_cmd("fx rgb group ws")
    await it.on_cmd("fx rgb speed 0")
    await it.on_cmd("fx rgb intensity 100")

    await it.on_cmd("fx rgb spread 0")
    await engine.poll(0.0)
    live = _captured(engine)
    assert (live[1], live[2], live[3]) == (live[6], live[7], live[8])  # unison

    await it.on_cmd("fx rgb spread 50")
    await engine.poll(0.0)
    live = _captured(engine)
    assert (live[1], live[2], live[3]) != (live[6], live[7], live[8])  # offset


@pytest.mark.asyncio
async def test_fx_pt_home_and_movement() -> None:
    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch star_wash_bl h 1 @ 1")  # pan@0, tilt@1
    await it.on_cmd("group heads = h 1")
    await it.on_cmd("fx pt group heads")
    await it.on_cmd("fx pt home 100 100")
    await it.on_cmd("fx pt mode circle")
    await it.on_cmd("fx pt size 100")
    await it.on_cmd("fx pt speed 0")
    await it.on_cmd("fx pt intensity 100")

    await engine.poll(0.0)  # theta 0 -> cos=1,sin=0 -> pan=home+amp, tilt=home
    live = _captured(engine)
    assert (live[0], live[1]) == (227, 100)

    await it.on_cmd("fx pt mode static")  # holds exactly home
    await engine.poll(0.0)
    live = _captured(engine)
    assert (live[0], live[1]) == (100, 100)


@pytest.mark.asyncio
async def test_fx_pt_home_capture() -> None:
    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch star_wash_bl h 1 @ 1")
    await it.on_cmd("group heads = h 1")
    await it.on_cmd("fx pt group heads")

    # point the head by hand, then capture the current output as home
    await it.on_cmd("live on")
    await it.on_cmd("fix h 1 pan 40 tilt 60")
    await engine.poll(0.0)
    await it.on_cmd("fx pt home")
    assert it.homes[("h", 1)] == (102, 153)  # parse_intensity(40)=102, (60)=153


@pytest.mark.asyncio
async def test_fx_cc_and_note_bindings() -> None:
    from aioartnet.console import CONTROLLER_CHANGE, NOTE_ON, MidiCC

    engine = Engine(Mock(), universe_size=60)
    fake = _FakeMidiIn()
    midi = MidiCC(fake)
    it = Interpreter(engine, midi=midi, profiles=_fixture_profiles())
    await it.on_cmd("patch rgba_wash w 1 @ 1")
    await it.on_cmd("group ws = w 1")
    await it.on_cmd("fx rgb group ws")

    # CC drives a continuous param
    await it.on_cmd("midi bind cc 74 fx rgb intensity")
    fake.feed([CONTROLLER_CHANGE, 74, 64])
    midi.poll()
    assert engine.effects["rgb"].params["intensity"] == 64 / 127.0

    # a CC cannot bind an enum param
    with pytest.raises(ValueError):
        await it.on_cmd("midi bind cc 75 fx rgb style")

    # a note latches an enum param and it persists after release
    await it.on_cmd("midi bind note 76 fx rgb style ocean")
    fake.feed([NOTE_ON, 76, 100])
    midi.poll()
    assert engine.effects["rgb"].params["style"] == "ocean"
    fake.feed([[0x80, 76, 0]][0])  # note off
    midi.poll()
    assert engine.effects["rgb"].params["style"] == "ocean"  # latched


@pytest.mark.asyncio
async def test_fx_save_round_trip() -> None:
    profiles = _fixture_profiles()
    src = Interpreter(Engine(Mock(), universe_size=60), profiles=profiles)
    await src.on_cmd("patch rgba_wash w 1 thru 2 @ 1")
    await src.on_cmd("patch star_wash_bl h 1 @ 20")
    await src.on_cmd("group ws = w 1 thru 2")
    await src.on_cmd("group heads = h 1")
    await src.on_cmd("fx rgb group ws")
    await src.on_cmd("fx rgb style ocean")
    await src.on_cmd("fx rgb speed 40")
    await src.on_cmd("fx pt group heads")
    await src.on_cmd("fx pt home 100 120")
    await src.on_cmd("midi bind cc 74 fx rgb intensity")
    await src.on_cmd("midi bind note 76 fx rgb style rainbow")

    dst = Interpreter(Engine(Mock(), universe_size=60), profiles=profiles)
    for line in src.serialise():
        if not line.startswith("#"):
            await dst.on_cmd(line)

    assert dst.engine.effects["rgb"].params == src.engine.effects["rgb"].params
    assert dst.engine.effects["rgb"].group == src.engine.effects["rgb"].group
    assert dst.engine.effects["pt"].group == src.engine.effects["pt"].group
    assert dst.homes == src.homes
    assert dst.bindings == src.bindings
    assert dst.note_bindings == src.note_bindings


@pytest.mark.asyncio
async def test_view_trim_serialises() -> None:
    from aioartnet.console import DmxGrid

    engine = Engine(Mock(), universe_size=64)
    it = Interpreter(engine, profiles=_fixture_profiles())
    grid = DmxGrid(lambda: bytes(64), lambda: bytes(64))
    grid.trim_source = it._patched_extent
    it.grid = grid

    # default view is not emitted
    assert not any(line.startswith("view") for line in it.serialise())

    await it.on_cmd("view pct trim")
    assert grid.mode == "pct" and grid.trim is True
    assert "view pct trim" in it.serialise()

    # replay onto a fresh grid restores the setting
    engine2 = Engine(Mock(), universe_size=64)
    it2 = Interpreter(engine2, profiles=_fixture_profiles())
    grid2 = DmxGrid(lambda: bytes(64), lambda: bytes(64))
    grid2.trim_source = it2._patched_extent
    it2.grid = grid2
    for line in it.serialise():
        if not line.startswith("#"):
            await it2.on_cmd(line)
    assert grid2.mode == "pct" and grid2.trim is True


@pytest.mark.asyncio
async def test_midi_channel_disambiguates_notes() -> None:
    from aioartnet.console import NOTE_ON, MidiCC, parse_chan_num

    assert parse_chan_num("9:36") == (9, 36)
    assert parse_chan_num("36") == (0, 36)  # bare -> channel 0

    engine = Engine(Mock(), universe_size=20)
    fake = _FakeMidiIn()
    midi = MidiCC(fake)
    it = Interpreter(engine, midi=midi, profiles=None)

    await it.on_cmd("chan 1 at f")
    await it.on_cmd("record sub 1")
    await it.on_cmd("chan 2 at f")
    await it.on_cmd("record sub 2")

    # same note number 36, different channels -> two distinct bindings
    await it.on_cmd("midi bind note 0:36 flash sub 1")  # keybed
    await it.on_cmd("midi bind note 9:36 flash sub 2")  # pad
    assert it.note_bindings == {(0, 36): ("flash", 0), (9, 36): ("flash", 1)}

    # a pad press (channel 9) flashes sub 2 only, keybed (sub 1) untouched
    fake.feed([NOTE_ON | 9, 36, 100])
    midi.poll()
    assert engine.subs[1].intensity == 1.0
    assert engine.subs[0].intensity == 0.0

    # a keybed press (channel 0) flashes sub 1 only
    fake.feed([NOTE_ON | 0, 36, 100])
    midi.poll()
    assert engine.subs[0].intensity == 1.0

    # both round-trip through the show file with their channels
    show = it.serialise()
    assert "midi bind note 0:36 flash sub 1" in show
    assert "midi bind note 9:36 flash sub 2" in show


@pytest.mark.asyncio
async def test_fx_rgb_over_mixed_profile_group() -> None:
    from aioartnet.console import SRC_FX

    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())
    # rgba_wash: red/green/blue at offsets 1,2,3 (base 0)
    await it.on_cmd("patch rgba_wash w 1 @ 1")
    # star_wash_bl: red/green/blue at offsets 3,4,5 (base 20 -> chan 19)
    await it.on_cmd("patch star_wash_bl h 1 @ 20")
    await it.on_cmd("group all = w 1 h 1")  # mixed-profile group

    await it.on_cmd("fx rgb group all")
    await it.on_cmd("fx rgb style rainbow")
    await it.on_cmd("fx rgb spread 0")
    await it.on_cmd("fx rgb speed 0")
    await it.on_cmd("fx rgb intensity 100")
    await engine.poll(0.0)

    live = _captured(engine)
    # rainbow phase 0 = red on BOTH fixtures' red/green/blue
    assert (live[1], live[2], live[3]) == (255, 0, 0)  # wash
    assert (live[22], live[23], live[24]) == (255, 0, 0)  # head (base 19 + 3,4,5)
    assert engine.last_source[1] == SRC_FX
    assert engine.last_source[22] == SRC_FX
    # the 4th emitters are left alone (wash amber @4, head white @25)
    assert live[4] == 0 and live[25] == 0


@pytest.mark.asyncio
async def test_group_listing(capsys) -> None:  # type: ignore[no-untyped-def]
    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())

    await it.on_cmd("group")
    assert "No groups" in capsys.readouterr().out

    await it.on_cmd("patch rgba_wash w 1 thru 3 @ 1")
    await it.on_cmd("patch star_wash_bl h 1 @ 20")
    await it.on_cmd("group all = w 1 thru 3 h 1")
    await it.on_cmd("group")
    out = capsys.readouterr().out
    assert "group all = w 1 thru 3 h 1" in out


@pytest.mark.asyncio
async def test_fixture_attr_listing(capsys) -> None:  # type: ignore[no-untyped-def]
    engine = Engine(Mock(), universe_size=120)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch star_wash_bl head 1 thru 4 @ 1")
    await it.on_cmd("patch rgba_wash wash 1 @ 60")
    await it.on_cmd("group heads = head 1 thru 4")

    # selector with no attributes lists what can be set, incl. macro options
    await it.on_cmd("fixture heads")
    out = capsys.readouterr().out
    assert "star_wash_bl" in out
    assert "gobo (open|stars|moon)" in out
    assert "color" in out  # rgb present -> shortcut shown

    # a mixed selection shows only the common attributes (intersection)
    await it.on_cmd("group mixed = head 1 wash 1")
    await it.on_cmd("fix mixed")
    out = capsys.readouterr().out
    assert "red" in out and "green" in out and "blue" in out
    assert "gobo" not in out  # head-only attr excluded from the common set
    assert "white" not in out and "amber" not in out


@pytest.mark.asyncio
async def test_fx_pt_16bit_fine_channels() -> None:
    from aioartnet.console import load_profiles

    # a head with pan_fine/tilt_fine: pan@0 pan_fine@1 tilt@2 tilt_fine@3
    profiles = load_profiles(
        {"fixtures": {"mh": ["pan", "pan_fine", "tilt", "tilt_fine", "dimmer"]}}
    )
    engine = Engine(Mock(), universe_size=20)
    it = Interpreter(engine, profiles=profiles)
    await it.on_cmd("patch mh m 1 @ 1")
    await it.on_cmd("group g = m 1")
    await it.on_cmd("fx pt group g")
    await it.on_cmd("fx pt home 100 100")
    await it.on_cmd("fx pt mode circle")
    await it.on_cmd("fx pt size 50")  # amp16 = 0.5*127*256 = 16256
    await it.on_cmd("fx pt speed 0")
    await it.on_cmd("fx pt intensity 100")
    await engine.poll(0.0)  # theta 0 -> dpan=1, dtilt=0

    live = _captured(engine)
    # pan: (100<<8) + 16256 = 41856 -> coarse 163, fine 128
    assert (live[0], live[1]) == (163, 128)
    # tilt: delta 0 -> 100<<8 -> coarse 100, fine 0
    assert (live[2], live[3]) == (100, 0)

    # static holds the coarse home exactly with fine zeroed
    await it.on_cmd("fx pt mode static")
    await engine.poll(0.0)
    live = _captured(engine)
    assert (live[0], live[1], live[2], live[3]) == (100, 0, 100, 0)


@pytest.mark.asyncio
async def test_fixture_inspect_detail(capsys) -> None:  # type: ignore[no-untyped-def]
    engine = Engine(Mock(), universe_size=120)
    it = Interpreter(engine, profiles=_fixture_profiles())
    # star_wash_bl footprint 8: pan0 tilt1 dimmer2 red3 green4 blue5 white6 gobo7
    await it.on_cmd("patch star_wash_bl head 1 thru 2 @ 1")  # head2 base 8
    await it.on_cmd("live on")
    await it.on_cmd("fix head 2 red 50")  # parse_intensity(50)=128
    await it.on_cmd("fix head 2 gobo stars")  # value 50
    await engine.poll(0.0)

    capsys.readouterr()  # clear
    await it.on_cmd("fixture head 2")
    out = capsys.readouterr().out

    assert "head 2 (star_wash_bl) @ 9" in out  # base 8 -> abs address 9
    lines = {ln.split()[0]: ln for ln in out.splitlines() if ln.startswith("  ")}
    # red: rel 4 (offset 3), abs 12 (base 8 + offset 3 = 11 -> +1), value 128
    assert "rel  4" in lines["red"] and "abs  12" in lines["red"]
    assert lines["red"].rstrip().endswith("= 128")
    # gobo enum shows the matching macro name for its current value
    assert "stars" in lines["gobo"]


@pytest.mark.asyncio
async def test_fixture_inspect_vs_group_summary(capsys) -> None:  # type: ignore[no-untyped-def]
    engine = Engine(Mock(), universe_size=120)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch star_wash_bl head 1 thru 2 @ 1")
    await it.on_cmd("group heads = head 1 thru 2")

    await it.on_cmd("fixture head 1")  # single -> detail (has abs/rel/value)
    assert "abs" in capsys.readouterr().out

    await it.on_cmd("fixture heads")  # multiple -> common attrs summary
    out = capsys.readouterr().out
    assert "settable attributes" in out and "abs" not in out


@pytest.mark.asyncio
async def test_look_bank_no_htp_bleed() -> None:
    from aioartnet.console import SRC_LOOK

    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())
    # star_wash_bl: red@3 green@4 blue@5 gobo@7
    await it.on_cmd("patch star_wash_bl head 1 @ 1")
    await it.on_cmd("group heads = head 1")
    await it.on_cmd("bank heads red_moon = fix heads gobo moon ; fix heads color red")
    await it.on_cmd("bank heads grn_star = fix heads gobo stars ; fix heads color green")

    await it.on_cmd("bank heads red_moon")
    await engine.poll(0.0)
    live = _captured(engine)
    assert live[7] == 100  # gobo moon
    assert (live[3], live[4], live[5]) == (255, 0, 0)
    assert engine.last_source[7] == SRC_LOOK

    # switching fully replaces: gobo=stars (NOT max), red cleared to 0
    await it.on_cmd("bank heads grn_star")
    await engine.poll(0.0)
    live = _captured(engine)
    assert live[7] == 50  # stars, not max(100, 50)
    assert (live[3], live[4], live[5]) == (0, 255, 0)
    assert it.banks["heads"].active == "grn_star"
    assert list(engine.looks.keys()) == ["heads"]  # one entry: mutex

    # deactivate clears the bank
    await it.on_cmd("bank heads off")
    await engine.poll(0.0)
    assert "heads" not in engine.looks
    assert engine.last_source[7] != SRC_LOOK


@pytest.mark.asyncio
async def test_look_bank_note_activation_and_layering() -> None:
    from aioartnet.console import NOTE_ON, SRC_LIVE, MidiCC

    engine = Engine(Mock(), universe_size=60)
    fake = _FakeMidiIn()
    midi = MidiCC(fake)
    it = Interpreter(engine, midi=midi, profiles=_fixture_profiles())
    await it.on_cmd("patch star_wash_bl head 1 @ 1")
    await it.on_cmd("group heads = head 1")
    await it.on_cmd("bank heads a = fix heads gobo moon")
    await it.on_cmd("bank heads b = fix heads gobo stars")
    await it.on_cmd("midi bind note 9:40 bank heads a")
    await it.on_cmd("midi bind note 9:41 bank heads b")

    # note activation is synchronous (no await needed in the dispatch path)
    fake.feed([NOTE_ON | 9, 41, 100])
    midi.poll()
    assert it.banks["heads"].active == "b"
    fake.feed([NOTE_ON | 9, 40, 100])
    midi.poll()
    assert it.banks["heads"].active == "a"
    # re-press stays active (radio, no toggle-off)
    fake.feed([NOTE_ON | 9, 40, 100])
    midi.poll()
    assert it.banks["heads"].active == "a"

    # a manual live edit still punches over the look (SRC_LIVE on top)
    await it.on_cmd("live on")
    await it.on_cmd("fix heads gobo open")  # gobo@7 -> 0
    await engine.poll(0.0)
    assert _captured(engine)[7] == 0
    assert engine.last_source[7] == SRC_LIVE


@pytest.mark.asyncio
async def test_look_bank_validation() -> None:
    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch star_wash_bl head 1 @ 1")
    await it.on_cmd("group heads = head 1")

    with pytest.raises(ValueError):
        await it.on_cmd("bank heads bad = record cue 1")  # not a fix/chan command
    with pytest.raises(ValueError):
        await it.on_cmd("bank heads empty = ")  # empty body
    with pytest.raises(ValueError):
        await it.on_cmd("bank heads off = fix heads gobo open")  # 'off' reserved
    assert "heads" not in it.banks  # nothing stored on failure

    # 'bank B off' still deactivates cleanly
    await it.on_cmd("bank heads a = fix heads gobo moon")
    await it.on_cmd("bank heads a")
    await it.on_cmd("bank heads off")
    assert it.banks["heads"].active is None


@pytest.mark.asyncio
async def test_look_bank_recompiles_on_repatch() -> None:
    engine = Engine(Mock(), universe_size=120)
    it = Interpreter(engine, profiles=_fixture_profiles())
    await it.on_cmd("patch star_wash_bl head 1 @ 1")  # base 0, gobo@7
    await it.on_cmd("group heads = head 1")
    await it.on_cmd("bank heads a = fix heads gobo moon")
    await it.on_cmd("bank heads a")
    assert {ci.channel for ci in it.banks["heads"].looks["a"].compiled} == {7}

    await it.on_cmd("patch star_wash_bl head 1 @ 40")  # re-patch to base 39
    assert {ci.channel for ci in it.banks["heads"].looks["a"].compiled} == {46}
    await engine.poll(0.0)
    assert _captured(engine)[46] == 100  # output tracks the new patch


@pytest.mark.asyncio
async def test_look_bank_round_trip() -> None:
    profiles = _fixture_profiles()
    src = Interpreter(Engine(Mock(), universe_size=60), profiles=profiles)
    await src.on_cmd("patch star_wash_bl head 1 @ 1")
    await src.on_cmd("group heads = head 1")
    await src.on_cmd("bank heads a = fix heads gobo moon ; fix heads color red")
    await src.on_cmd("bank heads b = fix heads gobo stars ; fix heads color green")
    await src.on_cmd("bank heads b")  # active
    await src.on_cmd("midi bind note 9:40 bank heads a")

    dst = Interpreter(Engine(Mock(), universe_size=60), profiles=profiles)
    for line in src.serialise():
        if not line.startswith("#"):
            await dst.on_cmd(line)

    assert dst.banks["heads"].active == "b"
    assert (
        dst.banks["heads"].looks["a"].commands
        == src.banks["heads"].looks["a"].commands
    )
    assert dst.note_bindings == src.note_bindings


@pytest.mark.asyncio
async def test_looks_view_render() -> None:
    from aioartnet.console import LooksView

    it = Interpreter(Engine(Mock(), universe_size=60), profiles=_fixture_profiles())
    assert "no banks" in "".join(t for _, t in LooksView(it).render())

    await it.on_cmd("patch star_wash_bl head 1 @ 1")
    await it.on_cmd("group heads = head 1")
    await it.on_cmd("bank heads a = fix heads gobo moon")
    await it.on_cmd("bank heads b = fix heads gobo stars")
    await it.on_cmd("bank heads b")

    frags = LooksView(it).render()
    text = "".join(t for _, t in frags)
    assert "a" in text and "b" in text
    active = [s for s, t in frags if t.strip() == "b"]
    assert any("bold" in s for s in active)  # active look highlighted


def test_midi_missing_device_not_required(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import aioartnet.console as console

    def raise_no_device(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("No MIDI input ports found.")

    monkeypatch.setattr(console, "open_midi_input", raise_no_device)
    cfg = {"midi": {"enabled": True, "device": 0, "cc_defaults": {"16": 0}}}

    # not required -> no crash, a no-device MidiCC with mappings held
    midi = console.setup_midi_from_config(cfg, None)
    assert isinstance(midi, console.MidiCC)
    assert isinstance(midi.midi_in, console._NoMidiInput)
    assert midi.cc_last == {(0, 16): 0}

    # a no-device input simply never yields a message
    midi.poll()  # must not raise
    assert midi.notes_on == {}


def test_midi_missing_device_required_aborts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import aioartnet.console as console

    def raise_no_device(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("No MIDI input ports found.")

    monkeypatch.setattr(console, "open_midi_input", raise_no_device)
    cfg = {"midi": {"enabled": True, "device": 0, "required": True}}
    with pytest.raises(SystemExit):
        console.setup_midi_from_config(cfg, None)


@pytest.mark.asyncio
async def test_fx_unit_inspection(capsys) -> None:  # type: ignore[no-untyped-def]
    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())

    # bare `fx rgb` before configuring shows params + enum choices
    await it.on_cmd("fx rgb")
    out = capsys.readouterr().out
    assert "intensity = 0" in out
    assert "rainbow" in out and "ocean" in out  # style alternatives

    # `fx pt` shows current values, mode choices, and per-fixture home
    await it.on_cmd("patch star_wash_bl head 1 thru 2 @ 1")
    await it.on_cmd("group heads = head 1 thru 2")
    await it.on_cmd("fx pt group heads")
    await it.on_cmd("fx pt home 100 120")
    await it.on_cmd("fx pt mode wave")
    await it.on_cmd("fx pt size 40")
    capsys.readouterr()  # clear
    await it.on_cmd("fx pt")
    out = capsys.readouterr().out
    assert "mode      = wave" in out
    assert "static|circle|wave|sway" in out
    assert "size      = 40" in out
    assert "head 1 = (100, 120)" in out and "head 2 = (100, 120)" in out

    with pytest.raises(ValueError):
        await it.on_cmd("fx bogus")


@pytest.mark.asyncio
async def test_sub_command_defined_and_faded() -> None:
    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())
    # star_wash_bl: dimmer@2
    await it.on_cmd("patch star_wash_bl head 1 @ 1")
    await it.on_cmd("group heads = head 1")

    await it.on_cmd("sub 1 = fix heads dimmer full")
    assert it.engine.subs[0].commands == ["fix heads dimmer full"]
    assert {c.channel for c in it.engine.subs[0].channels} == {2}

    # the fader scales the compiled levels (HTP sub-mix): 50% -> 128
    await it.on_cmd("sub 1 at 50")
    await engine.poll(0.0)
    assert _captured(engine)[2] == 128

    # recompiles when the fixtures move
    await it.on_cmd("patch star_wash_bl head 1 @ 40")  # base 39
    assert {c.channel for c in it.engine.subs[0].channels} == {41}


@pytest.mark.asyncio
async def test_sub_command_and_recorded_round_trip() -> None:
    profiles = _fixture_profiles()
    src = Interpreter(Engine(Mock(), universe_size=60), profiles=profiles)
    await src.on_cmd("patch star_wash_bl head 1 @ 1")
    await src.on_cmd("group heads = head 1")
    await src.on_cmd("sub 1 = fix heads dimmer full")  # command-defined
    await src.on_cmd("sub 1 at 80")
    await src.on_cmd("chan 10 at full")  # legacy recorded
    await src.on_cmd("record sub 2")

    lines = src.serialise()
    assert "sub 1 = fix heads dimmer full" in lines
    assert any(line.startswith("record sub 2") for line in lines)

    dst = Interpreter(Engine(Mock(), universe_size=60), profiles=profiles)
    for line in lines:
        if not line.startswith("#"):
            await dst.on_cmd(line)
    assert dst.engine.subs == src.engine.subs


@pytest.mark.asyncio
async def test_sub_listing_and_view(capsys) -> None:  # type: ignore[no-untyped-def]
    from aioartnet.console import SubsView

    engine = Engine(Mock(), universe_size=60)
    it = Interpreter(engine, profiles=_fixture_profiles())
    assert "no subs" in "".join(t for _, t in SubsView(engine).render())

    await it.on_cmd("patch star_wash_bl head 1 @ 1")
    await it.on_cmd("group heads = head 1")
    await it.on_cmd("sub 1 = fix heads dimmer full")
    await it.on_cmd("sub 1 at 80")
    await it.on_cmd("chan 10 at full")
    await it.on_cmd("record sub 2")  # recorded

    await it.on_cmd("sub")
    out = capsys.readouterr().out
    assert "sub 1 [80]: fix heads dimmer full" in out
    assert "sub 2 [00]:" in out and "channels (recorded)" in out

    frags = SubsView(engine).render()
    active = [s for s, t in frags if t.startswith("1:")]
    assert any("bold" in s for s in active)  # the 80% fader is highlighted


@pytest.mark.asyncio
async def test_list_command_removed() -> None:
    it = Interpreter(Engine(Mock(), universe_size=20))
    with pytest.raises(ValueError):
        await it.on_cmd("list")


@pytest.mark.asyncio
async def test_submaster_provenance_distinct_from_cue() -> None:
    from aioartnet.console import SRC_STORED, SRC_SUB

    engine = Engine(Mock(), universe_size=20)
    it = Interpreter(engine)
    await it.on_cmd("chan 1 at f")
    await it.on_cmd("record cue 1")
    await it.on_cmd("go")  # ch1 driven by a cue
    await it.on_cmd("chan 2 at f")
    await it.on_cmd("record sub 1")
    await it.on_cmd("sub 1 at full")  # ch2 driven by a submaster
    await engine.poll(0.0)

    assert engine.last_source[0] == SRC_STORED  # cue (black)
    assert engine.last_source[1] == SRC_SUB  # submaster (yellow)

    # fader down releases the sub provenance
    await it.on_cmd("sub 1 at z")
    await engine.poll(0.0)
    assert engine.last_source[1] != SRC_SUB
