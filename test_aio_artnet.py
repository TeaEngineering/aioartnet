import struct

from aio_artnet import ArtNetUniverse, ArtNetClient, ArtNetClientProtocol
from typing import Iterator, Tuple
import pytest


def test_universe():
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


def test_artnet_poll_reply():

    # play a short pcap recording of Art-Net polls then inspect the resulting data
    # fake client.connect being called by manually building the protocol
    client = ArtNetClient(interface="dummy")
    proto = ArtNetClientProtocol("10.10.10.255", client)

    for _, pkt in packet_reader("tests/artnet-nodes.pcap"):
        udp = pkt[42:]
        addr = pkt[26:30]  # TODO format addr?
        print(f"ip {addr} data {udp}")
        proto.datagram_received(udp, addr)

    assert len(client.nodes) == 2
    assert len(client.universes) == 4
    assert list(map(str, client.universes.values())) == [
        "0:0:0",
        "0:0:1",
        "0:0:2",
        "0:0:3",
    ]
    print(client.nodes)
    assert (
        str(client.nodes[3724650688])
        == "ArtNetNode<DMX Monitor for iPhone 1.0,192.168.1.222:6454>"
    )
    assert (
        str(client.nodes[3439438016])
        == "ArtNetNode<Q Light Controller Plus - ArtNet interface,192.168.1.205:6454>"
    )
