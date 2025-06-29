import argparse
import asyncio
import html
import logging
from collections import deque
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import HTML  # For colored output
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.patch_stdout import patch_stdout

from . import ArtNetClient
from .events import ArtNetEvent, UniverseDMX


async def main(client: ArtNetClient) -> None:
    dmxstr = ["Waiting for DMX data"]
    history: deque[ArtNetEvent] = deque([], maxlen=10)
    await client.connect()

    def get_formatted_universe() -> HTML:
        return HTML(dmxstr[0])

    def get_event_history() -> HTML:
        return HTML("events: \n" + "\n".join(html.escape(str(p)) for p in history))

    def get_status_text() -> HTML:
        return HTML("<b><ansiblack>Press 'q' to quit. </ansiblack></b>")

    kb = KeyBindings()

    @kb.add("q")  # Bind the 'q' key to exit app
    def _(event: Any) -> None:
        event.app.exit()

    # Define the top UI window for displaying the DMX universe.
    dir_listing_window = Window(
        content=FormattedTextControl(get_formatted_universe),
        wrap_lines=True,
        style="bg:#262626 fg:#FFFFFF",  # Background: dark gray, Foreground: white
    )
    event_window = Window(
        content=FormattedTextControl(get_event_history),
        wrap_lines=True,
        height=10,
        style="bg:#262626 fg:#FFFFFF",  # Background: dark gray, Foreground: white
    )
    # Define the bottom UI window for displaying status messages.
    status_bar = Window(
        content=FormattedTextControl(get_status_text),
        height=1,
        style="bg:#444444 fg:#FFFFFF",
    )

    # Create the prompt_toolkit Application instance
    application: Application[Any] = Application(
        layout=Layout(
            HSplit(
                [
                    dir_listing_window,
                    event_window,
                    status_bar,
                ]
            )
        ),
        key_bindings=kb,
        full_screen=True,
    )

    async def handler_task(history: deque[ArtNetEvent], dmxstr: list[str]) -> None:
        async for event in client.events():
            match event:
                case UniverseDMX():
                    hs = event.data.hex()
                    dmxstr[0] = f"Universe {event.universe}\n" + " ".join(
                        [hs[i : i + 2] for i in range(0, len(hs), 2)]
                    )
                case _:
                    history.append(event)

            application.invalidate()

    task = asyncio.create_task(handler_task(history, dmxstr))

    await (
        application.run_async()
    )  # Run the prompt_toolkit application, blocking until exited
    task.cancel()
    await task


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="aioartnet viewer",
        description="View Art-Net nodes and universes",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-i", "--interface")
    parser.add_argument("-n", "--portName")
    parser.add_argument("universe", nargs="+", help="universes to view")

    args = parser.parse_args()

    level = {False: logging.INFO, True: logging.DEBUG}[args.verbose]
    logging.basicConfig(level=level)

    kwargs = {}
    if args.interface:
        kwargs["interface"] = args.interface
    if args.portName:
        kwargs["portName"] = args.portName
    client = ArtNetClient(**kwargs)

    for u in args.universe:
        client.set_port_config(u, is_output=True)

    with patch_stdout():
        asyncio.run(main(client))
