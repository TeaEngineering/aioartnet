import asyncio
import logging

from . import ArtNetClient


async def main() -> None:
    client = ArtNetClient()
    await client.connect()
    u = client.set_port_config("0:0:1", isinput=True)
    u.last_data[0:100] = range(100)

    u2 = client.set_port_config("0:0:5", isoutput=True)

    while True:
        await asyncio.sleep(5)
        print("status:")
        for n, node in client.nodes.items():
            print(f"{node!r: <60} {node.ports}")

        for univ in client.universes.values():
            print(f" {univ} pubs:{univ.publishers} subs:{univ.subscribers}")
            print(univ.publisherseq)

        print(u2.last_data[0:20])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
    asyncio.get_event_loop().run_forever()
