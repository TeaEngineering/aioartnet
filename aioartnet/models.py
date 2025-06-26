from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Tuple, Union

DMX_UNIVERSE_SIZE = 512

DatagramAddr = tuple[Union[str, Any], int]


class UnivActuator(Protocol):
    def _send_dmx(self, univ: "ArtNetUniverse") -> None: ...


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
    def __init__(self, portaddress: int, client: UnivActuator):
        if portaddress > 0x7FFF:
            raise ValueError("Invalid net:subnet:universe, as net>128")
        self.portaddress = portaddress
        self.publishers: list[ArtNetNode] = list()
        self.subscribers: list[ArtNetNode] = list()
        self.last_data = bytearray(DMX_UNIVERSE_SIZE)
        self._last_seq = 1
        self._last_publish: float = 0.0
        self.publisherseq: dict[Tuple[DatagramAddr, int], int] = {}
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

    # eq/hash based on portaddress only
    def __hash__(self) -> int:
        return hash(self.portaddress)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, self.__class__):
            return self.portaddress == other.portaddress
        else:
            return False


@dataclass
class ArtNetPort:
    node: Optional[ArtNetNode]
    is_input: bool
    media: int
    portaddr: int
    universe: ArtNetUniverse

    def __repr__(self) -> str:
        inout = {True: "Input", False: "Output"}[self.is_input]
        media = ["DMX", "MIDI", "Avab", "Colortran CMX", "ADB 62.5", "Art-Net", "DALI"][
            self.media
        ]
        return f"Port<{inout},{media},{self.universe}>"
