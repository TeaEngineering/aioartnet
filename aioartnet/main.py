import argparse
import asyncio
import logging

from . import (
    ArtNetClient,
)


async def main(client: ArtNetClient) -> None:
    await client.connect()

    async for event in client.events():
        print(event)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="aioartnet cli",
        description="View Art-Net nodes and universes",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-i", "--interface")
    parser.add_argument("-n", "--portName")
    parser.add_argument("-p", "--publish", action="store_true")
    args = parser.parse_args()

    level = {False: logging.INFO, True: logging.DEBUG}[args.verbose]
    logging.basicConfig(level=level)

    kwargs = {}
    if args.interface:
        kwargs["interface"] = args.interface
    if args.portName:
        kwargs["portName"] = args.portName
    client = ArtNetClient(**kwargs)
    if args.publish:
        u1 = client.set_port_config("0:0:0", is_input=True)
        u1.set_dmx(bytes(list(range(128)) * 4))
    asyncio.run(main(client))
    asyncio.get_event_loop().run_forever()
