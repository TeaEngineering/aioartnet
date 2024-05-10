import asyncio
import socket
import struct
import ipaddress
import fcntl
import logging
from typing import Optional, Set, Tuple
import re
from collections import defaultdict

from desk import (
    ControllerUniverseOutput,
    NetNode,
    Controller,
    UniverseKey,
    DMX_UNIVERSE_SIZE,
)

# Art-Net implementation for Python asyncio
# Any page references to 'spec' refer to
#   "Art-Net 4 Protocol Release V1.4 Document Revision 1.4di 29/7/2023"
#
# This implementation Copyright Tea Engineering Ltd. 2024
#

# Art-Net specific constants
ARTNET_PORT = 6454
ARTNET_PREFIX = bytes("Art-Net".encode() + b"\000")

# We need to interrogate network interfaces to check which are configured for IP and
# have a valid broadcast address. For unix-like systems this is fone with socket fnctl
# calls. This isn't portable and might need reconsidering later. Constants from
# https://elixir.bootlin.com/linux/v4.2/source/include/uapi/linux/sockios.h#L43
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
SIOCGIFBRDADDR = 0x8919
SIOCGIFFLAGS = 0x8913

# interfaces *with an ip* are preferred in this order
PREFERED_INTERFACES_ORDER = ["enp.*", "wlp.*"]

# register a logger so that our debug can be enabled if required
logger = logging.getLogger("aioartnet")


# helper to de-tangle some of the protocol endianness. Fields like IP address
# are stored as 4 consecutive bytes, but not in little-endian like the rest of the
# protocol. Better to read as a 32-bit int in struct.unpack and then byteswap it
def swap32(x: int) -> int:
    return int.from_bytes(
        x.to_bytes(4, byteorder="little"), byteorder="big", signed=False
    )


def swap16(x: int) -> int:
    return int.from_bytes(
        x.to_bytes(2, byteorder="little"), byteorder="big", signed=False
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
# TODO: implement multiple pages (ArtFor now I have will implement a single


class ArtNetNode:
    def __init__(
        self,
        longName: str = "name",
        portName: str = "",
        address: str = "",
        style: int = 0,
    ) -> None:
        self.portName = portName
        self.longName = longName
        self.address = address
        self.portBinds: defaultdict[int, list[ArtNetPort]] = defaultdict(list)
        self.ports = []
        self.style = style

    def __repr__(self):
        return f"ArtNetNode<{self.portName},{self.address}>"


class ArtNetUniverse:
    def __init__(self, portaddress: int):
        if portaddress > 0x7FFF:
            raise ValueError("Invalid net:subnet:universe, as net>128")
        self.portaddress = portaddress
        self.publishers: Set[ArtNetNode] = set()
        self.subscribers: Set[ArtNetNode] = set()
        self.last_data = bytearray(DMX_UNIVERSE_SIZE)
        self.last_seq = 0
        self.publisherseq: dict[Tuple[int, int], int] = {}

    def __repr__(self):
        net = self.portaddress >> 8
        sub_net = (self.portaddress >> 4) & 0x0F
        universe = self.portaddress & 0x0F
        # name  net:sub_net:universe
        # bits  8:15  4:8     0:4
        return f"{net}:{sub_net}:{universe}"


class ArtNetPort:
    def __init__(
        self,
        node: ArtNetNode,
        isinput: bool,
        media: int,
        portaddr: int,
        universe: ArtNetUniverse,
    ):
        self.node = node
        self.isinput = isinput
        self.media = 0
        self.portaddr = portaddr
        self.universe = universe

    def __repr__(self):
        inout = {True: "Input", False: "Output"}[self.isinput]
        media = ["DMX", "MIDI", "Avab", "Colortran CMX", "ADB 62.5", "Art-Net", "DALI"][
            self.media
        ]
        return f"Port<{inout},{media},{self.universe}>"


class ArtNetClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: "ArtNetClient"):
        self.client = client
        self.transport = None
        self.handlers = {
            0x2000: self.on_art_poll,
            0x2100: self.on_art_poll_reply,
            0x5000: self.on_art_dmx,
        }
        client.protocol = self

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def datagram_received(self, data: bytes, addr):
        if data[0:8] == ARTNET_PREFIX:
            (opcode,) = struct.unpack("H", data[8:10])
            h = self.handlers.get(opcode, None)
            if h:
                h(addr, data[10:])
            else:
                print(f"Received Art-Net: op {opcode} from {addr}: {data[10:]!r}")
                raise ValueError(f"missing art-net support for {hex(opcode)}")
        else:
            print(f"Received non Art-Net data from {addr}: {data!r}")

    def on_art_poll(self, addr, data: bytes) -> None:
        ver, flags, priority = struct.unpack("<HBB", data)
        ver = swap16(ver)
        logger.info(
            f"Received Art-Net Poll: ver {ver} flags {flags} prio: {priority} from {addr}"
        )
        self.send_art_poll_reply(addr)

    def on_art_poll_reply(self, addr, data: bytes) -> None:
        # everything up to the mac address field is mandatory, the rest must be
        # parsed only if it is sent (field at a time)
        ip, port, fw, netsw, subsw, oemCode = struct.unpack("<IHHBBH", data[0:12])
        ubeaVer, status, esta, portName = struct.unpack("<BBH18s", data[12:34])
        longName, report, numports = struct.unpack("<64s64sH", data[34:164])
        ptype, ins, outs, swin, swout = struct.unpack("<4s4s4s4s4s", data[164:184])
        acnprio, swmacro, swremote, style = struct.unpack("<BBB3xB", data[184:191])
        mac = struct.unpack("<6s", data[191:197])

        bindip = 0
        bindindex = 0
        status2 = 0
        goodout = bytes(4)
        status3 = 0
        rdm = bytes(6)
        user = 0
        refresh = 0
        if len(data) >= 202:
            bindip, bindindex = struct.unpack("<IB", data[197:202])
        if len(data) >= 203:
            status2 = struct.unpack("<B", data[202:203])
        if len(data) >= 207:
            goodout = struct.unpack("<4s", data[203:207])
        if len(data) >= 208:
            status3 = struct.unpack("<B", data[207:208])
        if len(data) >= 216:
            rdm = struct.unpack("<6s", data[210:216])
        if len(data) >= 218:
            user = struct.unpack("<H", data[216:218])
        if len(data) >= 220:
            refresh = struct.unpack("<H", data[218:220])

        # post process
        portName = portName.rstrip(b"\000").decode()
        longName = longName.rstrip(b"\000").decode()

        ipa = ipaddress.IPv4Address(swap32(ip))

        nn = self.client.nodes.get(ip, None)
        changed = False
        if nn is not None:
            changed |= nn.longName != longName
            changed |= nn.portName != portName
            changed |= nn.style != style
            logger.debug(f"change detection on {nn} => {changed}")

        if nn is None or changed:
            newnode = ArtNetNode(
                address=f"{ipa}:{port}",
                longName=longName,
                portName=portName,
                style=style,
            )
            if changed:
                logger.debug(f"change detected: from {nn} to {newnode}")

            # TODO: make client.node observable dict and remove this method
            self.client.add_node(ip, newnode)
            nn = newnode

        # iterate through the ports and create ports and universes
        portList = []
        for _type, _in, _out, _swin, _swout in zip(ptype, ins, outs, swin, swout):
            in_port_addr = (
                ((netsw & 0x7F) << 8) + ((subsw & 0x0F) << 4) + (_swin & 0x0F)
            )
            out_port_addr = (
                ((netsw & 0x7F) << 8) + ((subsw & 0x0F) << 4) + (_swout & 0x0F)
            )
            if _type & 0b10000000:
                outu = self.get_create_universe(out_port_addr)
                portList.append(
                    ArtNetPort(nn, False, _type & 0x1F, out_port_addr, outu)
                )
            if _type & 0b01000000:
                inu = self.get_create_universe(in_port_addr)
                portList.append(ArtNetPort(nn, True, _type & 0x1F, in_port_addr, inu))

        # track which 'pages' of port bindings we have seen
        old_ports = nn.portBinds[bindindex]
        for p in portList:
            if p not in old_ports:
                nn.ports.append(p)
                nn.portBinds[bindindex].append(p)
        for p in list(old_ports):
            if p not in portList:
                nn.ports.remove(p)
                mm.portBinds[bindindex].remove(p)

        logger.info(
            f"Received Art-Net PollReply from {ip} fw {fw} portName {portName} longName: {longName} bindindex {bindindex} ports:{portList}"
        )

    def on_art_dmx(self, addr, data: bytes) -> None:
        ver, seq, phys, sub, net, chlen = struct.unpack("<HBBBBH", data[0:8])
        ver = swap16(ver)
        chlen = swap16(chlen)
        portaddress = sub + (net << 8)
        print(
            f"Received Art-Net DMX: ver {ver} port_address {portaddress} seq {seq} channels {chlen} from {addr}"
        )
        # TODO: should we support overside/undersize universes? For now truncate
        # what is supplied beyond DMX_UNIVERSE_SIZE
        if chlen > DMX_UNIVERSE_SIZE:
            chlen = DMX_UNIVERSE_SIZE

        u = self.get_create_universe(portaddress)

        # If addr is the broadcast address, it could be from a device
        # that is not responding to Art-Net Poll messages, rather then a unicast
        # stream just to us.

        # It would seem that (ip_address,phys) is the 'key' for a sequence.
        # ie. the spec envisions that a single node with two input ports could output
        # them both on Art-Net to the same universe. The reciever would disambiguate
        # the senders by ip_address+phys (see p62)
        publisherkey = (addr[0], phys)

        # TODO: check seq field for packet re-ordering and loss detection
        # a seq of 0 is considered always 'in sequence' as the sender is not
        # supporting sequencing. Valid values wrap within [1..255]
        if seq > 0:
            # store the last value we've seen
            u.publisherseq[publisherkey] = seq

        # TODO: HTP/LTP merging with Merge Mode, see "Data Merging" spec p61
        # See ArtAddress AcCancelMerge flags spec p39
        # Only two sources are allowed to contribute to the values in the universe
        u.last_data[0:chlen] = data[8 : 8 + chlen]

    def get_create_universe(self, port_addr: int) -> ArtNetUniverse:
        if (u := self.client.universes.get(port_addr, None)) is None:
            u = ArtNetUniverse(port_addr)
            self.client.universes[port_addr] = u
        return u

    async def art_poll_task(self):
        while True:
            await asyncio.sleep(3)
            self._send_art_poll()

    def _send_art_poll(self):
        message = ARTNET_PREFIX + struct.pack("<HBBBB", 0x2000, 0, 14, 6, 16)
        logger.debug(f"sending poll to {self.client.broadcast_ip}")
        self.transport.sendto(message, addr=(self.client.broadcast_ip, ARTNET_PORT))

    def send_art_poll_reply(self, addr) -> None:
        bindindex = 1
        ip = int.from_bytes(
            socket.inet_aton(self.client.unicast_ip), byteorder="little", signed=False
        )
        bindip = ip
        oemCode = 0
        esta = 0x02AE
        ports = []
        status2 = 0x08  # 15-bit port-address supported
        status3 = 0
        rdm = 0
        user = 0
        refresh = 40

        # build dynamically from ports[]
        ptype = b"\0\0\0\0"
        ins = b"\0\0\0\0"
        outs = b"\0\0\0\0"
        swin = b"\0\0\0\0"
        swout = b"\0\0\0\0"
        goodout = b"\0\0\0\0"
        mac = b"\01\22\33\44\55\66"
        bindindex = 1

        data = ARTNET_PREFIX + struct.pack(
            "<HIHHBBHBBH18s64s64sH4s4s4s4s4sBBB3xB6sIBB4sB6xHH11x",
            0x2100,
            ip,
            ARTNET_PORT,
            1,
            self.client.net,
            self.client.subnet,
            oemCode,
            0,
            0,
            esta,
            self.client._portName.encode(),
            self.client._longName.encode(),
            b"",
            len(ports),
            ptype,
            ins,
            outs,
            swin,
            swout,
            0,
            0,
            0,
            self.client._style,
            mac,
            bindip,
            bindindex,
            status2,
            goodout,
            status3,
            user,
            refresh,
        )
        logger.debug(f"sending poll reply to {addr}")
        self.transport.sendto(data, addr=(self.client.broadcast_ip, ARTNET_PORT))

    def error_received(self, exc):
        logger.warn("Error received:", exc)

    def connection_lost(self, exc):
        logger.warn("Connection closed")


class ArtNetClient(ControllerUniverseOutput):
    def __init__(
        self, interface=None, net=0, subnet=0, passive=False, portName="aioartnet"
    ) -> None:
        self.nodes: dict[int, ArtNetNode] = {}
        self.controller: Optional[Controller] = None
        self.universes: dict[int, ArtNetUniverse] = {}
        self.net = 0
        self.subnet = 0
        self.ports = []
        self._portName = portName
        self._longName = f"{portName} (aioartnet)"
        # identify as an StController, see Table 4 'Style Codes' p23
        self._style = 1
        self.passive = passive

        if interface is None:
            interface = get_preferred_artnet_interface()
        self.interface = interface
        self.broadcast_ip = None
        self.unicast_ip = None
        self.protocol: Optional[ArtNetClientProtocol]

    async def connect(self, controller: Controller) -> asyncio.Future:
        loop = asyncio.get_running_loop()

        on_con_lost = loop.create_future()

        # lookup broadcast for provided interface
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            iface_bin = struct.pack("256s", bytes(self.interface, "utf-8"))
            packet_ip = fcntl.ioctl(s.fileno(), SIOCGIFADDR, iface_bin)[20:24]
            bcast = fcntl.ioctl(s.fileno(), SIOCGIFBRDADDR, iface_bin)[20:24]
            self.broadcast_ip = socket.inet_ntoa(bcast)
            self.unicast_ip = socket.inet_ntoa(packet_ip)
            logger.info(
                f"using interface {self.interface} with ip {self.unicast_ip} broadcast ip {self.broadcast_ip}"
            )

        # remote_addr=('192.168.1.255', ARTNET_PORT),
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: ArtNetClientProtocol(self),
            local_addr=("0.0.0.0", ARTNET_PORT),
            family=socket.AF_INET,
            allow_broadcast=True,
        )
        if not self.passive:
            asyncio.create_task(protocol.art_poll_task())

        self.transport = transport
        self.controller = controller

        return on_con_lost

    async def set_dmx(self, universe: UniverseKey, data: bytes):
        pass

    def get_nodes(self) -> list[NetNode]:
        return list(self.nodes.values())

    def add_node(self, ip: int, node: ArtNetNode):
        self.nodes[ip] = node
        if self.controller:
            self.controller.add_node(node)

    def set_port_config(self, universe: UniverseKey, is_input=True):
        universe_int = 0
        if isinstance(universe, str):
            # parse to int
            net, sub, univ = map(int, universe.split(":"))
            port_addr = ((net & 0x7F) << 8) + ((sub & 0x0F) << 4) + (univ & 0x0F)

    # if you need to change a lot of properties in bulk, set client.passive=True,
    # make the required changes then clear .passive
    @property
    def portName(self):
        return self._portName

    @portName.setter
    def portName(self, value: str) -> None:
        self._portName = value
        if not self.passive and self.protocol:
            self.protocol._send_art_poll()

    @property
    def longName(self):
        return self._longName

    @longName.setter
    def longName(self, value: str) -> None:
        self._longName = value
        if not self.passive and self.protocol:
            self.protocol._send_art_poll()

    @property
    def style(self):
        return self._style

    @longName.setter
    def style(self, value: int) -> None:
        self._style = value
        if not self.passive and self.protocol:
            self.protocol._send_art_poll()


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
    for idx, name in socket.if_nameindex():
        packet, netmask, bcast = get_iface_ip(name)
        logger.debug(f"interface idx={idx} name={name} {packet} {netmask} {bcast}")
        # looks like an explicit class-A primary interface for Art-Net
        if packet is None:
            # no ip address, skip
            pass
        elif netmask == "255.0.0.0" and packet.startswith("2."):
            preferred.append((-1, name))
        else:
            for i, p in enumerate(matchers):
                if re.match(p, name):
                    preferred.append((i, name))
                    break
            else:
                preferred.append((10, name))

    preferred = sorted(preferred)
    logger.info(f"preferred interfaces: {preferred}")
    interface = preferred[0][1]
    return interface


async def main():
    client = ArtNetClient()
    on_con_lost = await client.connect(None)
    # print(await client.get_dmx(universe=1))
    # print(await client.set_dmx(universe=1, data=b"\0\0\0\0"))

    while True:
        await asyncio.sleep(5)
        print("status:")
        for n, node in client.nodes.items():
            print(node)

        for u, univ in client.universes.items():
            print(univ)
            print(univ.publisherseq)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
    asyncio.get_event_loop().run_forever()
