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
async def test_console_loop() -> None:
    set_dmx = Mock()
    engine = Engine(set_dmx, universe_size=20)
    interpreter = Interpreter(engine)
    cmds = "ch 1 at f,record cue 1,ch 2 at f,record cue 2,ch 3 at f,record cue 3,go,go,go,go"
    for cmd in cmds.split(","):
        await interpreter.on_cmd(cmd)
    assert engine.active_cue == 0
