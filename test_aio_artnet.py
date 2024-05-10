import struct

from aio_artnet import ArtNetUniverse, ArtNetClient, ArtNetClientProtocol
from typing import Iterator, Tuple, Any
import pytest
import socket


def test_universe() -> None:
    assert str(ArtNetUniverse(4)) == "0:0:4"
    assert str(ArtNetUniverse(0x15)) == "0:1:5"
    assert str(ArtNetUniverse(0x315)) == "3:1:5"
    assert str(ArtNetUniverse(0x7FF)) == "7:15:15"
    assert str(ArtNetUniverse(0xFFF)) == "15:15:15"
    assert str(ArtNetUniverse(0x7FFF)) == "127:15:15"
    with pytest.raises(ValueError):
        ArtNetUniverse(0x8FFF)  # only 128 'nets'


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


class MockTransport:
    def __init__(self):
        self.sent = []

    def get_extra_info(self, key: str) -> Any:
        return None

    def sendto(self, data, addr=None):
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

    for _, pkt in packet_reader("tests/artnet-nodes.pcap"):
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
    assert (
        str(client.nodes[3724650688])
        == "ArtNetNode<DMX Monitor for iPhone 1.0,192.168.1.222:6454>"
    )
    assert (
        str(client.nodes[3439438016])
        == "ArtNetNode<Q Light Controller Plus - ArtNet interface,192.168.1.205:6454>"
    )

    # last publisher seq stored by (address,physicalport)
    assert client.universes[8].last_data[0] == 0
    assert client.universes[8].last_data[1] == 0x70
    assert client.universes[8].last_data[2] == 0x94
    assert client.universes[8].publisherseq == {("192.168.1.205", 0): 20}

    assert client.universes[2].last_data[1] == 0
    assert client.universes[2].publisherseq == {("192.168.1.205", 0): 85}

    # DMX Monitor for iPhone binds from page 1, identifies as a desk
    assert client.nodes[3724650688].portBinds == {1: []}
    assert client.nodes[3724650688].style == 1

    # QLC binds from page 0, identifies as a node
    assert len(client.nodes[3439438016].portBinds) == 1
    ports = client.nodes[3439438016].portBinds[0]
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
    assert client.nodes[168430090].portBinds == {1: []}
