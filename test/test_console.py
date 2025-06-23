from unittest.mock import Mock

import pytest

from aioartnet.console import Engine, Intepreter


@pytest.mark.asyncio
async def test_console() -> None:
    set_dmx = Mock()
    engine = Engine(set_dmx)
    interpreter = Intepreter(engine)

    exp = bytearray(512)
    await interpreter.on_cmd("live on")
    set_dmx.assert_called_with(exp)
    assert engine.cues == []

    await interpreter.on_cmd("chan 1 at 50")
    exp[0] = 128
    set_dmx.assert_called_with(exp)

    await interpreter.on_cmd("chan 10 thru 20 at 20")
    exp[9:19] = bytes([51] * 10)
    set_dmx.assert_called_with(exp)

    await interpreter.on_cmd("RECORD CUE 1")
    assert len(engine.cues) == 1
    set_dmx.assert_called_with(exp)

    # await interpreter.on_cmd("RECORD CUE 2 TIME 4")
    # await interpreter.on_cmd("RECORD CUE 3 TIME 2 DELAY 1")
    # await interpreter.on_cmd("CUE 3 LABEL \"Scene 1 blackout\"")
    # await interpreter.on_cmd("GO")
    # await interpreter.on_cmd("STATE")
    # await interpreter.on_cmd("LIST")
