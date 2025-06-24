import asyncio
import ipaddress
import logging
import re
import socket
import struct
import time
from collections import defaultdict
from typing import Any, Optional, Tuple, Union, cast

from .network import AF_PACKET, getifaddrs

# Art-Net implementation for Python asyncio
# Any page references to 'spec' refer to
#   "Art-Net 4 Protocol Release V1.4 Document Revision 1.4di 29/7/2023"
#
# This implementation Copyright Tea Engineering Ltd. 2024
#

# Art-Net specific constants
ARTNET_PORT = 6454
ARTNET_PREFIX = bytes("Art-Net".encode() + b"\000")

DMX_UNIVERSE_SIZE = 512

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

# We enumerate all universes offered by all nodes and offer them
# merged by the 15-bit universe identifier. Controlling code can make a call to
# set_port_config to subscribe, write, or broadcast each universe, and we will
# manage the art-poll-reply flags to make this work.
#
# We might recieved unsolicited broadcasts of a universe from other controllers,
# (like QLC+) there's nothing we can do about this, but we drop them unless we
# are in subscribe mode.

# Each Node can publish one (artnet<3) or more (arcnet>3) sets of 4-ports.
# Each set fixes a single net and sub_net value, however the choice
# of universe nibble is determined per-port (net:sub_net:universe).

DGAddr = tuple[Union[str, Any], int]


class ArtNetNode:
    def __init__(
        self,
        longName: str,
        portName: str,
        style: int,
        ip: str,
        udpport: int,
    ) -> None:
        self.portName = portName
        self.longName = longName
        self._portBinds: defaultdict[int, list[ArtNetPort]] = defaultdict(list)
        self.ports: list[ArtNetPort] = []
        self.style: int = style
        self.udpport = udpport
        self.ip = ip
        self.last_reply: float = 0.0

    def __repr__(self) -> str:
        return f"ArtNetNode<{self.portName},{self.ip}:{self.udpport}>"


class ArtNetUniverse:
    def __init__(self, portaddress: int, client: "ArtNetClient"):
        if portaddress > 0x7FFF:
            raise ValueError("Invalid net:subnet:universe, as net>128")
        self.portaddress = portaddress
        self.publishers: list[ArtNetNode] = list()
        self.subscribers: list[ArtNetNode] = list()
        self.last_data = bytearray(DMX_UNIVERSE_SIZE)
        self._last_seq = 1
        self._last_publish: float = 0.0
        self.publisherseq: dict[Tuple[DGAddr, int], int] = {}
        self._client = client

    def split(self) -> Tuple[int, int, int]:
        # name  net:sub_net:universe
        # bits  8:15  4:8     0:4
        net = self.portaddress >> 8
        sub_net = (self.portaddress >> 4) & 0x0F
        universe = self.portaddress & 0x0F
        return net, sub_net, universe

    def __repr__(self) -> str:
        net, sub_net, universe = self.split()
        return f"{net}:{sub_net}:{universe}"

    def set_dmx(self, data: bytes) -> None:
        assert len(data) == DMX_UNIVERSE_SIZE
        self.last_data[:] = data[:]
        self._client._send_dmx(self)

    def get_dmx(self) -> bytes:
        return self.last_data


class ArtNetPort:
    def __init__(
        self,
        node: Union[ArtNetNode, "ArtNetClient"],
        is_input: bool,
        media: int,
        portaddr: int,
        universe: ArtNetUniverse,
    ):
        self.node = node
        self.is_input = is_input
        self.media = 0
        self.portaddr = portaddr
        self.universe = universe

    def __repr__(self) -> str:
        inout = {True: "Input", False: "Output"}[self.is_input]
        media = ["DMX", "MIDI", "Avab", "Colortran CMX", "ADB 62.5", "Art-Net", "DALI"][
            self.media
        ]
        return f"Port<{inout},{media},{self.universe}>"


class ArtNetClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: "ArtNetClient"):
        self.client = client
        self.transport: Optional[asyncio.DatagramTransport] = None
        self._last_poll = 0.0
        self.handlers = {
            0x2000: self.on_art_poll,
            0x2100: self.on_art_poll_reply,
            0x5000: self.on_art_dmx,
        }
        client.protocol = self
        self.node_report_counter = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)
        sock = transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def datagram_received(self, data: bytes, addr: DGAddr) -> None:
        if data[0:8] == ARTNET_PREFIX:
            (opcode,) = struct.unpack("H", data[8:10])
            h = self.handlers.get(opcode, None)
            if h:
                h(addr, data[10:])
            else:
                logger.debug(
                    f"Received unsupported Art-Net: op {hex(opcode)} from {addr}: {data[10:]!r}"
                )
        else:
            logger.debug(f"Received non Art-Net data from {addr}: {data!r}")

    def on_art_poll(self, addr: DGAddr, data: bytes) -> None:
        ver, flags, priority = struct.unpack("<HBB", data)
        ver = swap16(ver)
        logger.debug(
            f"Received Art-Net Poll: ver {ver} flags {flags} prio: {priority} from {addr}"
        )
        self.send_art_poll_reply()

    def on_art_poll_reply(self, addr: DGAddr, data: bytes) -> None:
        # everything up to the mac address field is mandatory, the rest must be
        # parsed only if it is sent (field at a time)
        ip, port, fw, netsw, subsw, oemCode = struct.unpack("<IHHBBH", data[0:12])
        ubeaVer, status, esta, portName = struct.unpack("<BBH18s", data[12:34])
        longName, report, numports = struct.unpack("<64s64sH", data[34:164])
        ptype, ins, outs, swin, swout = struct.unpack("<4s4s4s4s4s", data[164:184])
        acnprio, swmacro, swremote, style = struct.unpack("<BBB3xB", data[184:191])
        # mac = struct.unpack("<6s", data[191:197])

        bindip = 0
        bindindex = 0
        status2 = 0
        goodout = bytes(4)
        status3 = 0
        # rdm = bytes(6)
        user = 0
        refresh = 0
        if len(data) >= 202:
            bindip, bindindex = struct.unpack("<IB", data[197:202])
        if len(data) >= 203:
            (status2,) = struct.unpack("<B", data[202:203])
        if len(data) >= 207:
            (goodout,) = struct.unpack("<4s", data[203:207])
        if len(data) >= 208:
            (status3,) = struct.unpack("<B", data[207:208])
        if len(data) >= 216:
            (rdm,) = struct.unpack("<6s", data[210:216])
        if len(data) >= 218:
            (user,) = struct.unpack("<H", data[216:218])
        if len(data) >= 220:
            (refresh,) = struct.unpack("<H", data[218:220])

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

        # FIXME: what if a node changes ip address?

        if nn is None or changed:
            newnode = ArtNetNode(
                ip=f"{ipa}",
                udpport=port,
                longName=longName,
                portName=portName,
                style=style,
            )
            if changed:
                logger.debug(f"change detected: from {nn} to {newnode}")

            # TODO: make client.node observable dict and remove this method
            self.client.add_node(ip, newnode)
            nn = newnode

        nn.last_reply = time.time()

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
                outu = self.client._get_create_universe(out_port_addr)
                portList.append(
                    ArtNetPort(nn, False, _type & 0x1F, out_port_addr, outu)
                )
            if _type & 0b01000000:
                inu = self.client._get_create_universe(in_port_addr)
                portList.append(ArtNetPort(nn, True, _type & 0x1F, in_port_addr, inu))

        # track which 'pages' of port bindings we have seen
        old_ports = nn._portBinds[bindindex]
        for p in portList:
            if p not in old_ports:
                nn.ports.append(p)
                nn._portBinds[bindindex].append(p)
                {True: p.universe.publishers, False: p.universe.subscribers}[
                    p.is_input
                ].append(nn)

        for p in list(old_ports):
            if p not in portList:
                nn.ports.remove(p)
                nn._portBinds[bindindex].remove(p)
                {True: p.universe.publishers, False: p.universe.subscribers}[
                    p.is_input
                ].remove(nn)
        logger.debug(
            f"Received Art-Net PollReply from {ip} fw {fw} portName {portName} longName: {longName} bindindex {bindindex} ports:{portList}"
        )

    def on_art_dmx(self, addr: DGAddr, data: bytes) -> None:
        ver, seq, phys, sub, net, chlen = struct.unpack("<HBBBBH", data[0:8])
        ver = swap16(ver)
        chlen = swap16(chlen)
        portaddress = sub + (net << 8)

        # check enabled flag as a ~40Hz frequency message
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"Received Art-Net DMX: ver {ver} port_address {portaddress} seq {seq} channels {chlen} from {addr}"
            )
        # TODO: should we support overside/undersize universes? For now truncate
        # what is supplied beyond DMX_UNIVERSE_SIZE
        if chlen > DMX_UNIVERSE_SIZE:
            chlen = DMX_UNIVERSE_SIZE

        u = self.client._get_create_universe(portaddress)

        # If addr is the broadcast address, it could be from a device
        # that is not responding to Art-Net Poll messages, rather then a unicast
        # stream just to us.

        # It would seem that (ip_address,phys) is the 'key' for a sequence.
        # ie. the spec envisions that a single node with two input ports could output
        # them both on Art-Net to the same universe. The reciever would disambiguate
        # the senders by ip_address+phys (see p62)
        publisherkey = (addr, phys)

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

    async def art_poll_task(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            t = time.time()

            for u in self.client._publishing:
                if t > u._last_publish + 1.0:
                    self._send_art_dmx(u)

            if t > self._last_poll + 2.0:
                self._send_art_poll()

    def _send_art_poll(self) -> None:
        self._last_poll = time.time()
        self.node_report_counter = (self.node_report_counter + 1) % 10000
        message = ARTNET_PREFIX + struct.pack("<HBBBB", 0x2000, 0, 14, 2, 16)
        logger.debug(f"sending poll to {self.client.broadcast_ip}")
        if self.transport:
            self.transport.sendto(message, addr=(self.client.broadcast_ip, ARTNET_PORT))

    def _send_art_dmx(self, u: ArtNetUniverse) -> None:
        u._last_publish = time.time()
        u._last_seq = (u._last_seq + 1) % 255
        logger.debug(f"send_art_dmx {u} to {u.subscribers}")
        for s in u.subscribers:
            self._send_art_dmx_subscriber(u, s, u._last_seq)

    def send_art_poll_reply(self) -> None:
        for bi, p in self.client._portBinds.items():
            self._send_art_poll_reply_bindindex(bi, p)

    def _send_art_poll_reply_bindindex(
        self, bindindex: int, ports: list[ArtNetPort]
    ) -> None:
        if not self.client.unicast_ip:
            return
        ip = int.from_bytes(
            socket.inet_aton(self.client.unicast_ip), byteorder="little", signed=False
        )
        bindip = ip
        oemCode = 0x2CD3
        esta = 0x02AE
        status2 = 0x08  # 15-bit port-address supported
        status3 = 0
        user = 0
        refresh = 0

        # build dynamically from ports[]
        ptype = bytearray(4)
        ins = bytearray(4)
        outs = bytearray(4)
        swin = bytearray(4)
        swout = bytearray(4)
        goodout = bytearray(4)
        # defaults used when we have zero ports!
        net = self.client.net
        subnet = self.client.subnet
        mac = self.client.mac

        for i, p in enumerate(ports):
            ptype[i] = p.media | (1 << (6 if p.is_input else 7))
            net, subnet, universe = p.universe.split()
            if p.is_input:
                swin[i] = universe
            else:
                swout[i] = universe

        nodereport = f"#0001 [{self.node_report_counter:04d}] Debug OK".encode()

        data = ARTNET_PREFIX + struct.pack(
            "<HIHHBBHBBH18s64s64sBB4s4s4s4s4sBBB3xB6sIBB4sB6xHH11x",
            0x2100,
            ip,
            ARTNET_PORT,
            1,
            net,
            subnet,
            oemCode,
            0,
            0,
            esta,
            self.client._portName.encode(),
            self.client._longName.encode(),
            nodereport,
            0,
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
        addr = (self.client.broadcast_ip, ARTNET_PORT)
        logger.debug(f"sending poll reply to {addr}")
        if self.transport:
            self.transport.sendto(data, addr=addr)

    def _send_art_dmx_subscriber(
        self, universe: ArtNetUniverse, node: ArtNetNode, seq: int
    ) -> None:
        subuni = universe.portaddress & 0xFF
        net = universe.portaddress >> 8
        message = ARTNET_PREFIX + struct.pack(
            "<HBBBBBBH",
            0x5000,
            0,
            14,
            1 + seq,
            0,
            subuni,
            net,
            swap16(DMX_UNIVERSE_SIZE),
        )
        message = message + universe.last_data

        logger.debug(
            f"sending dmx for {universe} to {node} at {node.ip}:{node.udpport}"
        )
        if self.transport:
            self.transport.sendto(message, addr=(node.ip, node.udpport))

    def error_received(self, exc: Exception) -> None:
        logger.warn("Error received:", exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if self.client._task:
            logger.info("Connection closed, cancelling periodic task")
            self.client._task.cancel()


UniverseKey = Union[int, str, ArtNetUniverse]


class ArtNetClient:
    def __init__(
        self,
        interface: Optional[str] = None,
        net: int = 0,
        subnet: int = 0,
        passive: bool = False,
        portName: str = "aioartnet",
    ) -> None:
        self.nodes: dict[int, ArtNetNode] = {}
        self.universes: dict[int, ArtNetUniverse] = {}
        self.net = 0
        self.subnet = 0
        self.ports: list[ArtNetPort] = []
        self._portBinds: dict[int, list[ArtNetPort]] = {1: []}
        self._portName = portName
        self._longName = f"{portName} (aioartnet)"
        # identify as an StController, see Table 4 'Style Codes' p23
        self._style = 1
        self.passive = passive
        self.broadcast_ip: Optional[str] = None
        self.unicast_ip: Optional[str] = None
        self.listen_ip: str = "0.0.0.0"
        self.mac: bytes = b"\01\22\33\44\55\66"

        self.protocol: Optional[ArtNetClientProtocol] = None
        self._publishing: list[ArtNetUniverse] = []
        self.interface: Optional[str] = interface
        self._task: Optional[asyncio.Task[None]] = None

    async def connect(self) -> asyncio.DatagramTransport:
        loop = asyncio.get_running_loop()

        if self.broadcast_ip is None or self.unicast_ip is None:
            if self.interface is None:
                self.interface = get_preferred_artnet_interface()

            ips = get_iface_ip(self.interface)
            if not ips:
                raise Exception(
                    f"Unable to determine IPs for interface '{self.interface}'"
                )
            self.unicast_ip = ips["addr"]
            self.broadcast_ip = ips["broadaddr"]
            self.mac = bytes.fromhex(ips["mac"])

        logger.info(
            f"using interface {self.interface} with ip {self.unicast_ip} broadcast ip {self.broadcast_ip}, listening on {self.listen_ip} our mac {self.mac.hex()}"
        )

        # create_datagram_endpoint only allows us to listen on a single address. Ideally we would listen for packets
        # sent to both our unicast and the broadcast address. As a workaround we can listen on 0.0.0.0 which will get
        # us both (however perhaps also those from other interfaces).
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: ArtNetClientProtocol(self),
            local_addr=(self.listen_ip, ARTNET_PORT),
            family=socket.AF_INET,
            allow_broadcast=True,
        )
        if not self.passive:
            self._task = asyncio.create_task(
                protocol.art_poll_task(), name="Art-Net timer"
            )

        return transport

    def set_dmx(self, universe: UniverseKey, data: bytes) -> None:
        assert len(data) == DMX_UNIVERSE_SIZE
        port_addr = self._parse_universe(universe)

        # where to keep our write buffer?
        u = self._get_create_universe(port_addr)
        u.set_dmx(data)

    def _send_dmx(self, universe: ArtNetUniverse) -> None:
        if universe not in self._publishing:
            raise ValueError(f"No input port configured for {universe}")

        if self.protocol:
            self.protocol._send_art_dmx(universe)

    def get_nodes(self) -> list[ArtNetNode]:
        return list(self.nodes.values())

    def add_node(self, ip: int, node: ArtNetNode) -> None:
        self.nodes[ip] = node

    def _parse_universe(self, universe: UniverseKey) -> int:
        if isinstance(universe, str):
            # parse to int
            net, sub, univ = map(int, universe.split(":"))
            return ((net & 0x7F) << 8) + ((sub & 0x0F) << 4) + (univ & 0x0F)
        elif isinstance(universe, int):
            if universe > 0x7FFF:
                raise ValueError(f"invalid universe: {universe} exceeds 0x7fff")
            return universe
        elif isinstance(universe, ArtNetUniverse):
            return universe.portaddress
        else:
            raise ValueError(f"invalid universe: {universe}")

    def set_port_config(
        self,
        universe: UniverseKey,
        is_input: bool = False,
        is_output: bool = False,
    ) -> ArtNetUniverse:
        port_addr = self._parse_universe(universe)

        u = self._get_create_universe(port_addr)

        # port objects within client.ports all have node=None, this is the template
        # for the publisher. client.nodes[ownip].ports[] *should* contain the same
        # information once we process our own replies.
        port = None
        for i, p in enumerate(self.ports):
            if p.universe == u:
                logger.debug(f"set_port_config: has existing port {p}")
                port = p
                break
        if port:
            self.ports.remove(port)
            logger.info(f"removed own port {port}")

        if is_input or is_output:
            port = ArtNetPort(
                node=self, is_input=is_input, media=0, portaddr=port_addr, universe=u
            )
            self.ports.append(port)
            logger.info(f"configured own port {port}")

        # TODO: optimise the layour of self.ports to self._portBinds
        # up to four ports with a common (net,sub-net) can be listed on the same page
        # ie. sharing the same bindIndex. For now, we will put one on each
        if self.ports:
            self._portBinds = dict([(i + 1, [p]) for i, p in enumerate(self.ports)])
        else:
            self._portBinds = {1: []}

        # used for the timer-based DMX repeating
        if u in self._publishing:
            self._publishing.remove(u)
        if is_input:
            self._publishing.append(u)

        if not self.passive and self.protocol:
            self.protocol.send_art_poll_reply()

        return u

    def _get_create_universe(self, port_addr: int) -> ArtNetUniverse:
        if (u := self.universes.get(port_addr, None)) is None:
            u = ArtNetUniverse(port_addr, self)
            self.universes[port_addr] = u
        return u

    # MUTABLE PROPERTIES
    # if you need to change a lot of properties in bulk, set client.passive=True,
    # make the required changes, then clear .passive
    @property
    def portName(self) -> str:
        return self._portName

    @portName.setter
    def portName(self, value: str) -> None:
        self._portName = value
        if not self.passive and self.protocol:
            self.protocol.send_art_poll_reply()

    @property
    def longName(self) -> str:
        return self._longName

    @longName.setter
    def longName(self, value: str) -> None:
        self._longName = value
        if not self.passive and self.protocol:
            self.protocol.send_art_poll_reply()

    @property
    def style(self) -> int:
        return self._style

    @style.setter
    def style(self, value: int) -> None:
        self._style = value
        if not self.passive and self.protocol:
            self.protocol.send_art_poll_reply()


def get_iface_ip(iface: str) -> Optional[dict[str, Any]]:
    """
    Get network interface IP using the network interface name
    :param iface: Interface name (like eth0, enp2s0, etc.)
    :return IP address in the form XX.XX.XX.XX
    """
    na = {}
    for netaddr in getifaddrs(family=socket.AF_INET, ifname=iface):
        # return first address if multiple bound to NIC
        na = dict(**netaddr)
    for netaddr in getifaddrs(family=AF_PACKET, ifname=iface):
        # return first address if multiple bound to NIC
        na["mac"] = netaddr["addr"]
    return na


def get_preferred_artnet_interface() -> str:
    preferred = []
    patterns = list(map(re.compile, PREFERED_INTERFACES_ORDER))

    for netaddr in getifaddrs(family=socket.AF_INET):
        name = str(netaddr["name"])
        packet = netaddr["addr"]
        netmask = netaddr["netmask"]
        bcast = netaddr["broadaddr"]
        logger.debug(f"interface name={name} {packet} {netmask} {bcast}")
        # looks like an explicit class-A primary interface for Art-Net
        if netmask == "255.0.0.0" and packet.startswith("2."):
            preferred.append((-1, name))
        else:
            for i, pattern in enumerate(patterns):
                if re.match(pattern, name):
                    preferred.append((i, name))
                    break
            else:
                preferred.append((10, name))

    preferred = sorted(preferred)
    logger.info(f"preferred interfaces: {preferred}")
    interface = preferred[0][1]
    return interface
