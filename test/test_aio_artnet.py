import socket
import struct
from asyncio import BaseTransport
from collections import deque
from typing import Any, Iterator, Tuple

import pytest

from aioartnet import (
    DMX_UNIVERSE_SIZE,
    ArtNetClient,
    ArtNetUniverse,
)
from aioartnet.aio_artnet import ArtNetClientProtocol, DGAddr


def test_universe() -> None:
    ac = ArtNetClient()
    assert str(ArtNetUniverse(4, ac)) == "0:0:4"
    assert str(ArtNetUniverse(0x15, ac)) == "0:1:5"
    assert str(ArtNetUniverse(0x315, ac)) == "3:1:5"
    assert str(ArtNetUniverse(0x7FF, ac)) == "7:15:15"
    assert str(ArtNetUniverse(0xFFF, ac)) == "15:15:15"
    assert str(ArtNetUniverse(0x7FFF, ac)) == "127:15:15"
    with pytest.raises(ValueError):
        ArtNetUniverse(0x8FFF, ac)  # only 128 'nets'


def packet_reader(file: str) -> Iterator[Tuple[float, bytes]]:
    with open(file, "rb") as f:
        magic, verMaj, verMin, snaplen, netw = struct.unpack("<IHH8xII", f.read(24))
        print(f"pcap {file} magic {hex(magic)} ver {verMaj}.{verMin} link layer {netw}")

        # magic written as 0xa1b2c3d4 in native order
        # magic reads as 0xa1b2c3d4 => we are usec, good
        # magic reads as 0xd4c3b2a1 => we are usec, byte-swapped
        # magic reads as 0xa1b23c4d => nanos
        assert magic == 0xA1B2C3D4
        timediv = 1000000.0
        while True:
            hdr = f.read(16)
            if len(hdr) == 0:
                return
            tsec, tusec, filesz, wiresz = struct.unpack("<IIII", hdr)
            time = tsec + tusec / timediv
            # print(f" pkt {time} {filesz} {wiresz}")
            packet = f.read(filesz)
            yield time, packet


class MockTransport(BaseTransport):
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, DGAddr]] = []

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return None

    def sendto(self, data: bytes, addr: DGAddr) -> None:
        self.sent.append((data, addr))


def test_artnet_poll_reply() -> None:
    # play a short pcap recording of Art-Net polls & replies then inspect the resulting data
    # fake client.connect being called by manually building the protocol
    client = ArtNetClient(interface="dummy")
    client.broadcast_ip = "10.10.10.255"
    client.unicast_ip = "10.10.10.10"

    proto = ArtNetClientProtocol(client)
    transport = MockTransport()
    proto.connection_made(transport)

    for _, pkt in packet_reader("test/artnet-nodes.pcap"):
        udp = pkt[42:]
        ip = socket.inet_ntoa(pkt[26:30])
        (port,) = struct.unpack(">H", pkt[34:36])
        print(f"UDP ip {ip}:{port} data {udp!r}")
        # package up the sending address as a tuple like asyncio
        proto.datagram_received(udp, (ip, port))

    assert len(client.nodes) == 2
    # assert len(client.universes) == 4
    assert list(map(str, client.universes.values())) == [
        "0:0:0",
        "0:0:1",
        "0:0:2",
        "0:0:3",
        "0:0:8",
    ]
    # Note that 0:0:8 is being broadcasted to without the node (QLC+) listing the port
    # in its node output port configuration
    assert str(client.nodes[3724650688]) == "ArtNetNode<DMX Monitor,192.168.1.222:6454>"
    assert str(client.nodes[3439438016]) == "ArtNetNode<QLC+,192.168.1.205:6454>"

    # last publisher seq stored by (address,physicalport)
    assert client.universes[8].last_data[0] == 0
    assert client.universes[8].last_data[1] == 0x70
    assert client.universes[8].last_data[2] == 0x94
    assert client.universes[8].publisherseq == {(("192.168.1.205", 6454), 0): 20}

    assert client.universes[2].last_data[1] == 0
    assert client.universes[2].publisherseq == {(("192.168.1.205", 6454), 0): 85}

    # DMX Monitor for iPhone binds from page 1, identifies as a desk
    assert client.nodes[3724650688]._portBinds == {1: []}
    assert client.nodes[3724650688].style == 1

    # QLC binds from page 0, identifies as a node
    assert len(client.nodes[3439438016]._portBinds) == 1
    ports = client.nodes[3439438016]._portBinds[0]
    assert list(map(str, ports)) == [
        "Port<Output,DMX,0:0:0>",
        "Port<Output,DMX,0:0:1>",
        "Port<Output,DMX,0:0:2>",
        "Port<Output,DMX,0:0:3>",
    ]
    assert client.nodes[3439438016].style == 0

    # our node should have replied to the poll
    assert len(transport.sent) == 1
    pollreply, addr = transport.sent[0]
    assert addr == (client.broadcast_ip, 6454)
    assert len(pollreply) == 239

    # because we don't *see* our own poll reply so far in the test
    # (as it's in the MockTransport), our ArtNetNode
    # has not been created yet. Release it now and check
    proto.datagram_received(pollreply, addr)
    assert len(client.nodes) == 3
    nn = client.nodes[168430090]
    assert nn._portBinds == {1: []}
    assert str(nn) == "ArtNetNode<aioartnet,10.10.10.10:6454>"


# all connected protocols recieved each others messages
# note messages are dispatched inline, so the stack is re-entrent in ways
# that a real network is not.
#  ie. a send can trigger a rx that triggers a send that arrives in the
#    middle of the original send. normally event loops don't do this.
class BroadcastTransport(BaseTransport):
    def __init__(self, protocols: list[ArtNetClientProtocol] = []) -> None:
        self.protos = list(protocols)
        self.pending: deque[Tuple[bytes, Any]] = deque()
        for p in self.protos:
            p.connection_made(self)

    def connect_protocol(self, protocol: ArtNetClientProtocol) -> None:
        self.protos.append(protocol)

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return None

    def sendto(self, data: bytes, addr: DGAddr) -> None:
        self.pending.append((data, addr))

    def drain(self) -> None:
        while self.pending:
            msg = self.pending.popleft()
            for p in self.protos:
                p.datagram_received(*msg)


@pytest.mark.asyncio
async def test_artnet_back_to_back_nodes() -> None:
    # use two instances of our client linked by a mock transport to test
    # port and node detection

    clA = ArtNetClient(interface="dummy", portName="alpha")
    clA.broadcast_ip = "10.10.10.255"
    clA.unicast_ip = "10.10.10.10"

    clB = ArtNetClient(interface="dummy", portName="bravo")
    clB.broadcast_ip = "10.10.10.255"
    clB.unicast_ip = "10.10.10.2"

    protoA = ArtNetClientProtocol(clA)
    protoB = ArtNetClientProtocol(clB)
    transport = BroadcastTransport([protoA, protoB])

    # send, then flush the poll/reply packets
    protoA._send_art_poll()
    transport.drain()

    assert len(clA.nodes) == 2
    assert len(clB.nodes) == 2
    assert (
        str(list(clA.nodes.values()))
        == "[ArtNetNode<alpha,10.10.10.10:6454>, ArtNetNode<bravo,10.10.10.2:6454>]"
    )

    # when a client has a property modified, it automatically sends an unsolicited PollReply
    clB.portName = "charlie"
    assert len(transport.pending) == 1
    transport.drain()

    assert (
        str(list(clA.nodes.values()))
        == "[ArtNetNode<alpha,10.10.10.10:6454>, ArtNetNode<charlie,10.10.10.2:6454>]"
    )
    assert (
        str(list(clB.nodes.values()))
        == "[ArtNetNode<alpha,10.10.10.10:6454>, ArtNetNode<charlie,10.10.10.2:6454>]"
    )


@pytest.mark.asyncio
async def test_ports() -> None:
    # use one instance of client with a mock loopback transport to test
    # port and node detection

    clA = ArtNetClient(interface="dummy", portName="alpha")
    clA.broadcast_ip = "10.10.10.255"
    clA.unicast_ip = "10.10.10.10"
    clA.set_port_config("1:0:7", is_input=True)

    protoA = ArtNetClientProtocol(clA)
    transport = BroadcastTransport([protoA])

    # send, then flush the poll/reply packets
    protoA._send_art_poll()
    transport.drain()

    assert len(clA.nodes) == 1
    assert len(clA.ports) == 1
    assert str(clA._portBinds) == "{1: [Port<Input,DMX,1:0:7>]}"

    # check the *recieved* view of the same packets match
    assert str(list(clA.nodes.values())[0].ports) == "[Port<Input,DMX,1:0:7>]"
    assert list(clA.universes.keys()) == [263]
    assert str(clA.universes[263].publishers) == "[ArtNetNode<alpha,10.10.10.10:6454>]"
    assert clA.universes[263].subscribers == []

    # disable existing, add an output port
    clA.set_port_config("1:0:7")
    clA.set_port_config("0:1:8", is_output=True)

    transport.drain()
    assert str(clA._portBinds) == "{1: [Port<Output,DMX,0:1:8>]}"
    assert str(list(clA.nodes.values())[0].ports) == "[Port<Output,DMX,0:1:8>]"

    assert clA.universes[263].publishers == []
    assert clA.universes[263].subscribers == []
    print(clA.universes)
    assert clA.universes[24].publishers == []
    assert str(clA.universes[24].subscribers) == "[ArtNetNode<alpha,10.10.10.10:6454>]"

    # two ports active at once
    clA.set_port_config("0:1:9", is_input=True)
    transport.drain()
    assert clA.universes[24].publishers == []
    assert str(clA.universes[24].subscribers) == "[ArtNetNode<alpha,10.10.10.10:6454>]"
    assert str(clA.universes[25].publishers) == "[ArtNetNode<alpha,10.10.10.10:6454>]"
    assert str(clA.universes[25].subscribers) == "[]"


@pytest.mark.asyncio
async def test_dmx_tx_rx() -> None:
    # use two instances of our client linked by a mock transport to test
    # port and node detection

    clA = ArtNetClient(interface="dummy", portName="alpha")
    clA.broadcast_ip = "10.10.10.255"
    clA.unicast_ip = "10.10.10.10"

    clB = ArtNetClient(interface="dummy", portName="bravo")
    clB.broadcast_ip = "10.10.10.255"
    clB.unicast_ip = "10.10.10.2"

    protoA = ArtNetClientProtocol(clA)
    protoB = ArtNetClientProtocol(clB)
    transport = BroadcastTransport([protoA, protoB])

    utx = clA.set_port_config("1:0:7", is_input=True)
    urx = clB.set_port_config("1:0:7", is_output=True)

    assert urx.get_dmx() == bytes(DMX_UNIVERSE_SIZE)

    # send, then flush the poll/reply packets
    protoA._send_art_poll()
    transport.drain()

    test_pattern = bytes(list(range(128)) * 4)
    clA.set_dmx(utx, test_pattern)
    assert len(transport.pending) == 1
    transport.drain()

    assert urx.get_dmx() == test_pattern
