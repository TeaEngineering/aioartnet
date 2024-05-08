import asyncio
import socket
import struct
import ipaddress
import fcntl
from typing import Optional, Set
import re

from desk import (
    ControllerUniverseOutput,
    NetNode,
    Controller,
    UniverseKey,
    DMX_UNIVERSE_SIZE,
)


ARTNET_PORT = 6454
ARTNET_PREFIX = bytes("Art-Net".encode() + b"\000")


# socket-io fnctl flags
# https://elixir.bootlin.com/linux/v4.2/source/include/uapi/linux/sockios.h#L43
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
SIOCGIFBRDADDR = 0x8919
SIOCGIFFLAGS = 0x8913

# interfaces *with an ip* are preferred in this order
PREFERED_INTERFACES_ORDER = ["enp.*", "wlp.*"]


# helper to de-tangle some of the protocol endianness. Fields like IP address
# are stored as 4 consecutive bytes, but not in little-endian like the rest of the
# protocol. Better to read as a 32-bit int in struct.unpack and then byteswap it
def swap32(x: int) -> int:
    return int.from_bytes(
        x.to_bytes(4, byteorder="little"), byteorder="big", signed=False
    )


# The broadcast IP is used for locating nodes and managing subscriptions.
# Art-Net I and II protocols also used it for sending DMX data, and many
# implementations are backwards compatible.
# Art-Net II switched over to UDP unicast from the universe publisher to
# the subscriber(s).

# We enumerate all universes offered by all nodes and offer them to the controller,
# merged by the 15-bit universe identifier. The controller can make a call to
# subscribe, write, or broadcast each universe key, and we will manage the
# art-poll-reply flags to make this work.
#
# We might recieved unsolicited broadcasts of a universe from other controllers,
# (like QLC+) there's nothing we can do about this, but we drop them unless we
# are in subscribe mode.

# Each Node can publish one (artnet<3) or more (arcnet>3) sets of 4-ports.
# Each set fixes a single net and sub_net value, however the choice
# of universe nibble is determined per-port (net:sub_net:universe).
# TODO: implement multiple pages (ArtFor now I have will implement a singl e


class ArtNetNode(NetNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    def __repr__(self):
        return f"ArtNetNode<{self.name},{self.address}>"


class ArtNetUniverse:
    def __init__(self, portaddress: int):
        if portaddress > 0x7FFF:
            raise ValueError("Invalid net:subnet:universe, as net>128")
        self.portaddress = portaddress
        self.publishers: Set[ArtNetNode] = set()
        self.subscribers: Set[ArtNetNode] = set()
        self.last_data = bytearray(DMX_UNIVERSE_SIZE)

    def __repr__(self):
        net = self.portaddress >> 8
        sub_net = (self.portaddress >> 4) & 0x0F
        universe = self.portaddress & 0x0F
        # name  net:sub_net:universe
        # bits  8:15  4:8     0:4
        return f"{net}:{sub_net}:{universe}"


class ArtNetClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, broadcast_ip: str, client: "ArtNetClient"):
        self.client = client
        self.transport = None
        self.handlers = {0x2000: self.on_art_poll, 0x2100: self.on_art_poll_reply}
        self.broadcast_ip = broadcast_ip

    def connection_made(self, transport):
        self.transport = transport

        sock = transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        print("made:", self.transport)

    def datagram_received(self, data, addr):
        if data[0:8] == ARTNET_PREFIX:
            (opcode,) = struct.unpack("H", data[8:10])
            print(f"Received Art-net: op {opcode} from {addr}: {data[10:]}")
            h = self.handlers.get(opcode, None)
            if h:
                h(addr, data[10:])
        else:
            print(f"Received non Art-Net data {data} from {addr}")

    def on_art_poll(self, addr, data):
        ver, flags, priority = struct.unpack("HBB", data)
        print(f"Received Art-Net Poll: ver {ver} flags {flags} prio: {priority}")
        self.send_art_poll_reply(addr)

    def on_art_poll_reply(self, addr, data):
        (
            ip,
            port,
            fw,
            netsw,
            subsw,
            oemCode,
            ubeaVer,
            status,
            esta,
            portName,
            longName,
            report,
            numports,
        ) = struct.unpack("<IHHBBHBBH18s64s64sH", data[0:164])
        (
            ptype,
            ins,
            outs,
            swin,
            swout,
            acnprio,
            swmacro,
            swremote,
            style,
            mac,
            bindip,
            bindindex,
        ) = struct.unpack("4s4s4s4s4sBBB3xB6sIB", data[164:205])
        status2, goodout, status3, rdm, user, refresh, zfilter = struct.unpack(
            "B4sB6sHH8s", data[205:]
        )

        portName = portName.rstrip(b"\000").decode()
        longName = longName.rstrip(b"\000").decode()
        print(
            f"Received Art-Net PollReply: ip {ip} fw {fw} portName {portName} longName: {longName} portflags {ptype} bindindex {bindindex}"
        )

        ipa = ipaddress.IPv4Address(swap32(ip))

        nn = self.client.nodes.get(ip, None)
        if nn is None:
            print("protocol adding node")
            nn = ArtNetNode(address=f"{ipa}:{port}", name=longName)
            self.client.add_node(ip, nn)

        # do we know the universes?
        for _type, _in, _out, _swin, _swout in zip(ptype, ins, outs, swin, swout):
            print(f" port {_type} {_in} {_out} {_swin} {_swout} - {netsw} {subsw}")

            in_port_addr = (
                ((netsw & 0x7F) << 8) + ((subsw & 0x0F) << 4) + (_swin & 0x0F)
            )
            out_port_addr = (
                ((netsw & 0x7F) << 8) + ((subsw & 0x0F) << 4) + (_swout & 0x0F)
            )
            if _type & 0b10000000:
                outu = self.get_create_universe(out_port_addr)
                print(f"  is output port {outu}")
            if _type & 0b01000000:
                inu = self.get_create_universe(in_port_addr)
                print(f"  is input port {inu}")

    def get_create_universe(self, port_addr):
        if (u := self.client.universes.get(port_addr, None)) is None:
            u = ArtNetUniverse(port_addr)
            self.client.universes[port_addr] = u
        return u

    async def send_art_poll(self):
        message = ARTNET_PREFIX + struct.pack("<HBBBB", 0x2000, 0, 14, 6, 16)
        while True:
            await asyncio.sleep(3)
            print(f"sending poll to {self.broadcast_ip}")
            self.transport.sendto(message, addr=(self.broadcast_ip, ARTNET_PORT))

    def send_art_poll_reply(self, addr):
        # message = ARTNET_PREFIX + struct.pack('<HBBBB', 0x2000, 0, 14, 6, 16)
        print(f"sending poll reply to {addr}")
        # self.transport.sendto(message, addr=(self.broadcast_ip, ARTNET_PORT))

    def error_received(self, exc):
        print("Error received:", exc)

    def connection_lost(self, exc):
        print("Connection closed")


class ArtNetClient(ControllerUniverseOutput):
    def __init__(self, interface=None, net=0, subnet=0) -> None:
        self.nodes: dict[int, ArtNetNode] = {}
        self.controller: Optional[Controller] = None
        self.universes: dict[int, ArtNetUniverse] = {}
        self.net = 0
        self.subnet = 0

        if interface is None:
            interface = get_preferred_artnet_interface()
        self.interface = interface

    async def connect(self, controller: Controller) -> asyncio.Future:
        print("connect")
        loop = asyncio.get_running_loop()

        on_con_lost = loop.create_future()

        # lookup broadcast for provided interface
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            iface_bin = struct.pack("256s", bytes(self.interface, "utf-8"))
            packet_ip = fcntl.ioctl(s.fileno(), SIOCGIFADDR, iface_bin)[20:24]
            bcast = fcntl.ioctl(s.fileno(), SIOCGIFBRDADDR, iface_bin)[20:24]
            self.broadcast = socket.inet_ntoa(bcast)
            print(
                f"using interface {self.interface} with ip {socket.inet_ntoa(packet_ip)} broadcast ip {self.broadcast}"
            )

        # remote_addr=('192.168.1.255', ARTNET_PORT),
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: ArtNetClientProtocol(self.broadcast, self),
            local_addr=("0.0.0.0", ARTNET_PORT),
            family=socket.AF_INET,
            allow_broadcast=True,
        )

        asyncio.create_task(protocol.send_art_poll())

        self.transport = transport
        self.protocol = protocol
        self.controller = controller

        return on_con_lost

    async def set_dmx(self, universe: UniverseKey, data: bytes):
        pass

    def get_nodes(self) -> list[NetNode]:
        return list(self.nodes.values())

    def add_node(self, ip: int, node: ArtNetNode):
        print("client adding node")
        self.nodes[ip] = node
        if self.controller:
            self.controller.add_node(node)


def get_iface_ip(iface: str):
    """
    Get network interface IP using the network interface name
    :param iface: Interface name (like eth0, enp2s0, etc.)
    :return IP address in the form XX.XX.XX.XX
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            iface_bin = struct.pack("256s", bytes(iface, "utf-8"))
            packet_ip = fcntl.ioctl(s, SIOCGIFADDR, iface_bin)[20:24]
            netmask = fcntl.ioctl(s, SIOCGIFNETMASK, iface_bin)[20:24]
            bcast = fcntl.ioctl(s, SIOCGIFBRDADDR, iface_bin)[20:24]
        return map(socket.inet_ntoa, [packet_ip, netmask, bcast])
    except OSError:
        return None, None, None


def get_preferred_artnet_interface() -> str:
    preferred = []
    matchers = list(map(re.compile, PREFERED_INTERFACES_ORDER))
    print(matchers)
    for idx, name in socket.if_nameindex():

        packet, netmask, bcast = get_iface_ip(name)
        print(f"idx={idx} name={name} {packet} {netmask} {bcast}")
        # looks like an explicit class-A primary interface for Art-Net
        if packet is None:
            # no ip address, skip
            pass
        elif netmask == "255.0.0.0" and packet.startswith("2."):
            preferred.append((-1, name))
        else:
            for i, p in enumerate(matchers):
                if re.match(p, name):
                    print(f"found interface prefix {p} priority {i}")
                    preferred.append((i, name))
                    break
            else:
                preferred.append((10, name))

    preferred = sorted(preferred)
    print(f"preferred interfaces: {preferred}")
    interface = preferred[0][1]
    return interface


async def main():
    client = ArtNetClient()
    on_con_lost = await client.connect(None)
    # print(await client.get_dmx(universe=1))
    # print(await client.set_dmx(universe=1, data=b"\0\0\0\0"))

    try:
        await on_con_lost
    finally:
        pass


if __name__ == "__main__":
    asyncio.run(main())
